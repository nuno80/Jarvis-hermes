"""Flusso vocale Telegram: trascrizione Hermes -> ack Talker -> job Reasoner.

Tracciabilita: issue #18 (JARVIS-18), J11, D03, storie 2 e 11, sezione 7
(Talker-Reasoner).

Confini (ADR 0010): lo STT e di Hermes (cache Telegram -> faster-whisper
locale); Jarvis non tocca mai gli audio bytes, riceve solo la trascrizione,
la tratta come dato non fidato (AT05) e non ne desume alcun consenso (D07:
nessun token di approvazione creato o usato qui). Retention audio esplicita:
solo cache Hermes, mai storage Jarvis (``AUDIO_RETENTION_POLICY``).
"""

from __future__ import annotations

import time
from typing import Any, Callable

# ponytail: un modulo, dict-in/dict-out, riusa BudgetTracker/JobStore/pre_turn_dispatch.

# Telegram rende i clip lunghi (~5 min+) come 0:00 senza durata esplicita;
# oltre questa soglia si chiede di reinviare (fail-closed, niente troncature silenti).
VOICE_MAX_DURATION_SECONDS = 300
VOICE_TRANSCRIPT_EXCERPT_CHARS = 120
# Budget Talker della sezione 7: il riscontro parte entro 3 s, senza esiti non verificati.
ACK_LATENCY_BUDGET_MS = 3000.0

AUDIO_RETENTION_POLICY = (
    "audio_bytes_stored_by_jarvis=false; "
    "audio lives in the Hermes Telegram cache only; "
    "only the transcript is kept in the job record"
)


class VoiceError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


# Euristica dichiarata (ponytail: enforcement reale resta nel gate di approvazione):
# trascrizione che nomina effetti esterni + nessun fast reply -> chiarimento prima dell'effetto.
_PROTECTED_HINTS = (
    "cancella", "elimina", "push", "paga", "compra", "acquista",
    "invia ", "spedisc", "spegni", "riavvia", "formatta",
)

_PLAN_IT = {
    "disk_usage": "controllo lo stato del disco",
    "device_status": "controllo lo stato della macchina",
    "repo_status": "controllo lo stato del repo",
    "web_search": "cerco sul web",
    "travel_search": "cerco le offerte di viaggio",
}

_AMBIGUOUS_INTENTS = ("ambiguous", "dangerous_ambiguous")


DEFAULT_GROQ_STT_MODEL = "whisper-large-v3"
DEFAULT_LOCAL_STT_MODEL = "hermes_local"
DEFAULT_LOCAL_STT_PROVIDER = "hermes_stt"


def resolve_stt_backend(
    configured_provider: str | None = None,
    configured_model: str | None = None,
    env: dict[str, str] | None = None,
) -> tuple[str, str]:
    """Risolve provider e modello STT: Groq primario se GROQ_API_KEY presente, fallback local.

    Policy:
    - Se specificati esplicitamente parametri diversi dai default, vengono rispettati.
    - Altrimenti, se GROQ_API_KEY e' presente nell'environment, usa Groq + whisper-large-v3.
    - Se GROQ_API_KEY e' assente o vuota, fallback sul backend locale hermes_local.
    """
    import os
    environ = env if env is not None else os.environ

    if configured_provider and configured_provider not in ("hermes_stt", "auto"):
        return configured_provider, configured_model or (
            DEFAULT_GROQ_STT_MODEL if configured_provider == "groq" else DEFAULT_LOCAL_STT_MODEL
        )

    groq_key = environ.get("GROQ_API_KEY", "").strip()
    if groq_key:
        return "groq", configured_model or DEFAULT_GROQ_STT_MODEL

    return DEFAULT_LOCAL_STT_PROVIDER, configured_model or DEFAULT_LOCAL_STT_MODEL


def transcript_excerpt(text: str, limit: int = VOICE_TRANSCRIPT_EXCERPT_CHARS) -> str:
    clean = " ".join((text or "").split())
    return clean if len(clean) <= limit else clean[:limit] + "…"


def _plan_for(dispatch: dict[str, Any]) -> str:
    handler = str(dispatch.get("handler") or "")
    return _PLAN_IT.get(handler, _PLAN_IT.get(str(dispatch.get("intent") or ""), "elaboro la tua richiesta"))


def build_ack(transcript: str, dispatch: dict[str, Any], job_id: str) -> str:
    """Riscontro Talker: cosa sta per succedere, mai esiti non verificati."""
    excerpt = transcript_excerpt(transcript)
    plan = _plan_for(dispatch)
    n = len(" ".join(transcript.split()))
    return (
        f"🎙️ Ho trascritto il tuo vocale ({n} caratteri): «{excerpt}». "
        f"{plan[0].upper() + plan[1:]} e ti aggiorno qui appena pronto (job {job_id})."
    )


def build_clarification(transcript: str) -> str:
    excerpt = transcript_excerpt(transcript)
    return (
        f"🎙️ Ho trascritto: «{excerpt}». Prima di fare qualsiasi cosa: "
        "puoi precisare obiettivo e parametri (cosa, dove, quando)? Non ho eseguito nulla."
    )


def _needs_clarification(text: str, dispatch: dict[str, Any]) -> bool:
    if dispatch.get("escalation_reason") == "needs_clarification":
        return True
    if dispatch.get("target") == "clarification":
        return True
    if dispatch.get("intent") in _AMBIGUOUS_INTENTS:
        return True
    lowered = text.lower()
    return any(hint in lowered for hint in _PROTECTED_HINTS)


def handle_voice_transcript(
    transcript: str,
    *,
    actor_id: int,
    job_id: str = "voice-job",
    duration_seconds: int | None = None,
    stt_provider: str | None = None,
    stt_model: str | None = None,
    budget_tracker: Any,
    job_store: Any,
    telemetry_store: Any = None,
    dispatch_fn: Callable[[str], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Pipeline vocale pura: valida -> registra consumo STT -> System 1 -> ack/job/chiarimento.

    Ritorna sempre ``status`` in
    ``answered | working | needs_clarification | needs_resend`` con ``reply``/``ack``,
    ``job_id``, ``ack_latency_ms`` e ``stt_usage``. Alza ``VoiceError`` solo per
    durata eccessiva (fail-closed); il budget esaurito alza ``LLMError`` dal tracker.
    Non crea mai approvazioni: il consenso resta nel gate esistente (D07).
    """
    from .decision import pre_turn_dispatch
    from .llm import STTClient

    resolved_provider, resolved_model = resolve_stt_backend(stt_provider, stt_model)

    start = time.perf_counter()
    text = " ".join((transcript or "").split())

    if duration_seconds is not None and duration_seconds > VOICE_MAX_DURATION_SECONDS:
        if budget_tracker is not None:
            STTClient(budget_tracker, model_id=resolved_model).record_transcription(
                job_id, duration_seconds, provider=resolved_provider)
        raise VoiceError(
            "VOICE_TOO_LONG",
            f"Vocale troppo lungo ({duration_seconds}s > {VOICE_MAX_DURATION_SECONDS}s): "
            "reinvia un vocale più breve o scrivi la richiesta.")

    stt_usage: dict[str, Any] = {}
    if budget_tracker is not None:
        rec = STTClient(budget_tracker, model_id=resolved_model).record_transcription(
            job_id, duration_seconds or 0, provider=resolved_provider)
        stt_usage = {"provider": rec.provider, "model_id": rec.model_id,
                     "total_tokens": rec.total_tokens, "cost_usd": rec.cost_usd,
                     "is_estimated": rec.is_estimated}

    def elapsed_ms() -> float:
        return round((time.perf_counter() - start) * 1000.0, 2)

    if not text:
        return {"status": "needs_resend",
                "reply": "🎙️ Non ho capito l'audio (trascrizione vuota). Reinvia il vocale o scrivi la richiesta.",
                "job_id": None, "ack_latency_ms": elapsed_ms(), "stt_usage": stt_usage}

    job = job_store.create_job(
        actor_id, "voice_message", text[:200],
        metadata={"transcript_source": "voice_stt_untrusted",
                  "stt_provider": resolved_provider, "stt_model": resolved_model,
                  "duration_seconds": duration_seconds,
                  "audio_retention": AUDIO_RETENTION_POLICY,
                  "transcript_chars": len(text)})
    created_id = job["job_id"]

    dispatch = dispatch_fn or (lambda t: pre_turn_dispatch(
        t, job_id=job_id, budget_tracker=budget_tracker, telemetry_store=telemetry_store))
    try:
        result = dispatch(text)
        if not isinstance(result, dict):
            raise ValueError("dispatch non-dict")
    except Exception:
        result = {"path": "system_2", "target": "reasoning_llm",
                  "escalation_reason": "dispatch_failed",
                  "intent": None, "handler": None, "confidence": None, "reply": None}
    ack_latency_ms = elapsed_ms()

    if result.get("reply"):
        reply = f"🎙️ «{transcript_excerpt(text)}»\n{result['reply']}"
        job_store.update_status(created_id, "succeeded", step="voice_answered",
                                result={"reply": reply, "path": result.get("path")})
        return {"status": "answered", "reply": reply, "job_id": created_id,
                "ack_latency_ms": ack_latency_ms, "stt_usage": stt_usage,
                "dispatch": {"path": result.get("path"), "target": result.get("target"),
                             "intent": result.get("intent"), "confidence": result.get("confidence"),
                             "escalation_reason": result.get("escalation_reason")}}

    if _needs_clarification(text, result):
        reply = build_clarification(text)
        job_store.update_status(created_id, "running", step="clarification_requested")
        return {"status": "needs_clarification", "reply": reply, "job_id": created_id,
                "ack_latency_ms": ack_latency_ms, "stt_usage": stt_usage,
                "dispatch": {"path": result.get("path"), "target": result.get("target"),
                             "intent": result.get("intent"), "confidence": result.get("confidence"),
                             "escalation_reason": result.get("escalation_reason")}}

    ack = build_ack(text, result, created_id)
    job_store.update_status(created_id, "running", step="voice_ack_sent")
    return {"status": "working", "ack": ack, "job_id": created_id,
            "ack_latency_ms": ack_latency_ms, "stt_usage": stt_usage,
            "dispatch": {"path": result.get("path"), "target": result.get("target"),
                         "intent": result.get("intent"), "confidence": result.get("confidence"),
                         "escalation_reason": result.get("escalation_reason")}}
