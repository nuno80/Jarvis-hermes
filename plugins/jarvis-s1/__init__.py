"""Plugin Hermes per il System 1 Jarvis (issue #31 / JARVIS-31, ADR 0005).

Installazione (host reale, NON committata qui): copiare questa directory in
``~/.hermes/plugins/jarvis-s1/``. Nessun secondo poller: gli hook girano dentro
il ``GatewayRunner`` esistente; nessun segreto o token di approvazione transita
nel System 1 (AT13); nessun permesso concesso (policy MCP invariata).

Hook registrati:
- ``pre_gateway_dispatch``: risposta diretta per handler sicuri ad alta
  confidenza (skip = zero chiamate al modello principale, AT15); altrimenti
  dispatch normale (None/allow).
- ``pre_llm_call``: iniezione ``Suggested skill to inspect first: ...`` in
  coda al messaggio utente (prefix cache preservata, ADR 0005 §5).
- ``llm_request``: passthrough del modello (override solo se un giorno la
  policy lo prevede; oggi sempre None).
"""
from __future__ import annotations

import logging
import os
import sys
from typing import Any

logger = logging.getLogger(__name__)

# Percorso del checkout jarvis-hermes SOLO via env, mai hardcodato: il plugin
# gira sull'host Hermes reale, non nel repo di sviluppo. Legge anche
# ~/.hermes/.env via get_env_value (os.environ da solo non basta nel gateway).
def _jarvis_src() -> str | None:
    hit = os.environ.get("JARVIS_HERMES_SRC")
    if hit:
        return hit
    try:
        from hermes_cli.config import get_env_value
        return get_env_value("JARVIS_HERMES_SRC")
    except Exception:
        return None


_src = _jarvis_src()
if _src and _src not in sys.path:
    sys.path.insert(0, _src)

try:
    from jarvis_hermes.decision import pre_turn_dispatch
    from jarvis_hermes.voice import handle_voice_transcript
except Exception:  # Hermes non deve rompersi se Jarvis non e installato
    pre_turn_dispatch = None  # type: ignore[assignment]
    handle_voice_transcript = None  # type: ignore[assignment]


def _is_voice_event(event: Any) -> bool:
    """True per vocali/upload audio (Hermes STT li arricchisce gia in testo)."""
    mtype = str(getattr(event, "message_type", "") or "").lower()
    if "voice" in mtype or "audio" in mtype:
        return True
    urls = getattr(event, "media_urls", None) or []
    types = getattr(event, "media_types", None) or []
    return any("audio" in str(t).lower() or "ogg" in str(u).lower()
               for t, u in zip(types, urls)) or any(
        str(u).lower().endswith((".ogg", ".oga", ".mp3", ".m4a", ".wav"))
        for u in urls)


def _delivery_target(gateway: Any, event: Any) -> tuple[Any, Any]:
    source = getattr(event, "source", None)
    try:
        adapter = gateway.adapters[source.platform] if source is not None else None
    except Exception:
        adapter = None
    chat_id = getattr(source, "chat_id", None) if source is not None else None
    return adapter, chat_id


def _send_text(adapter: Any, chat_id: Any, text: str) -> bool:
    """Invio sync-safe da hook: coroutine -> task, valore -> invio diretto."""
    import asyncio
    try:
        send = adapter.send(chat_id, text)
    except Exception:
        return False
    if not asyncio.iscoroutine(send):
        return True
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        loop = None
    try:
        if loop is not None:
            loop.create_task(send)
        else:
            asyncio.run(send)
    except Exception:
        return False
    return True


def _text_of(event: Any) -> str:
    return str(getattr(event, "text", "") or "")


def pre_gateway_dispatch_hook(event: Any, gateway: Any,
                              session_store: Any, **kwargs: Any) -> Any:
    """Talker vocale + risposta diretta sicura, altrimenti None (= dispatch normale).

    Vocali (issue #18): lo STT Hermes gira DOPO l'hook (misurato live
    2026-10-04: text_len=0 all'hook), quindi due stadi: (1) ricevuta
    immediata "vocale ricevuto, lo trascrivo" entro 3 s senza esiti
    verificati, dispatch normale che prosegue; (2) hook post-STT lato
    Hermes (follow-up B) per ack/risposta sul testo trascritto. Se il testo
    e gia presente (caption), ack Talker diretto; answered -> skip.
    """
    if pre_turn_dispatch is None:
        return None
    if handle_voice_transcript is not None and _is_voice_event(event):
        return _voice_ack(event, gateway)
    try:
        result = pre_turn_dispatch(_text_of(event))
    except Exception:
        return None
    logger.info(
        "jarvis-s1 pre_gateway_dispatch path=%s conf=%s latency_ms=%s reply=%s",
        result.get("path"), result.get("confidence"),
        result.get("latency_ms"), bool(result.get("reply")))
    if result.get("reply") is None:
        return None
    adapter, chat_id = _delivery_target(gateway, event)
    if adapter is None or chat_id is None:
        return None
    if not _send_text(adapter, chat_id, result["reply"]):
        return None
    return {"action": "skip", "reason": "jarvis_s1_direct"}


def _voice_actor_id(event: Any) -> int:
    """Solo source.user_id: niente ripieghi su allowlist (attribuzione arbitraria)."""
    source = getattr(event, "source", None)
    raw = getattr(source, "user_id", None) if source is not None else None
    try:
        actor = int(str(raw))
    except (TypeError, ValueError):
        return 0
    return actor if actor > 0 else 0


def _voice_duration_seconds(event: Any) -> int | None:
    """Durata dal raw Telegram (voice/audio.duration); MessageEvent non ha il campo."""
    raw = getattr(event, "raw_message", None)
    for attr in ("voice", "audio"):
        clip = getattr(raw, attr, None) if raw is not None else None
        try:
            seconds = int(getattr(clip, "duration", None)) if clip is not None else None
        except (TypeError, ValueError):
            continue
        if seconds is not None:
            return seconds
    return None


def _voice_ack(event: Any, gateway: Any) -> Any:
    """Ack Talker per un vocale: ricevuta immediata + ack/risposta/chiarimento."""
    duration = _voice_duration_seconds(event)
    logger.info(
        "jarvis voice probe mtype=%s text_len=%s duration=%s",
        getattr(event, "message_type", None), len(_text_of(event)),
        duration)
    adapter, chat_id = _delivery_target(gateway, event)
    if adapter is None or chat_id is None:
        return None
    try:
        from jarvis_hermes.voice import (
            AUDIO_RETENTION_POLICY, VOICE_MAX_DURATION_SECONDS,
        )
    except Exception:
        AUDIO_RETENTION_POLICY = "audio in cache Telegram Hermes, mai salvato da Jarvis"
        VOICE_MAX_DURATION_SECONDS = 300
    if duration is not None and duration > VOICE_MAX_DURATION_SECONDS:
        _send_text(adapter, chat_id,
                   "🎙️ Vocale troppo lungo: reinvia un vocale più breve o scrivi la richiesta.")
        return {"action": "rewrite",
                "text": f"[vocale troppo lungo, utente gia avvisato; retention: {AUDIO_RETENTION_POLICY}]",
                "reason": "jarvis_voice_too_long"}
    text = _text_of(event).strip()
    if not text:
        # Stadio 1 (live, misurato 2026-10-04): lo STT Hermes gira dopo
        # l'hook, testo vuoto. Ricevuta immediata senza esiti verificati;
        # None = il dispatch normale prosegue (STT + Reasoner). Mai skip qui.
        if duration is not None:
            receipt = (f"🎙️ Vocale ricevuto ({duration} s): lo trascrivo "
                       "e ti aggiorno qui.")
        else:
            receipt = "🎙️ Vocale ricevuto: lo trascrivo e ti aggiorno qui."
        _send_text(adapter, chat_id, receipt)
        return None
    try:
        handle = handle_voice_transcript
        if handle is None:
            return None
        out = handle(
            text,
            actor_id=_voice_actor_id(event),
            duration_seconds=duration,
            budget_tracker=_voice_budget(),
            job_store=_voice_jobs(),
        )
    except Exception as exc:
        logger.warning("jarvis voice ack failed: %s", exc)
        return None
    message = out.get("reply") or out.get("ack")
    if message and not _send_text(adapter, chat_id, message):
        return None
    status = out.get("status")
    if status == "answered":
        # Risposta fast gia inviata in chat: nessun lavoro residuo, skip.
        return {"action": "skip", "reason": "jarvis_voice_answered"}
    # working / needs_clarification: il Reasoner gira sul testo trascritto.
    return {"action": "rewrite", "text": text, "reason": f"jarvis_voice_{status or 'ack'}"}


def _voice_state_dir() -> Any:
    from pathlib import Path
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    state_dir = state_home / "jarvis-hermes"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state_dir


def _voice_budget() -> Any:
    from jarvis_hermes.llm import BudgetTracker
    state_dir = _voice_state_dir()
    return BudgetTracker(
        state_dir / "llm_budget.json",
        job_limit_usd=float(os.environ.get("JARVIS_JOB_BUDGET_USD", "0.50")),
        daily_limit_usd=float(os.environ.get("JARVIS_DAILY_BUDGET_USD", "5.00")))


def _voice_jobs() -> Any:
    from jarvis_hermes.jobs import JobStore
    return JobStore(_voice_state_dir() / "jobs.sqlite3")


def pre_llm_call_hook(user_message: Any, **kwargs: Any) -> Any:
    """Suggerimento skill in coda al messaggio utente; None se niente da dire."""
    if pre_turn_dispatch is None:
        return None
    try:
        result = pre_turn_dispatch(str(user_message or ""))
    except Exception:
        return None
    context = result.get("skill_context")
    return {"context": context} if context else None


def llm_request_middleware(request: Any, **kwargs: Any) -> Any:
    """Passthrough: oggi la policy non prevede override del modello."""
    if pre_turn_dispatch is None or not isinstance(request, dict):
        return {"request": request}
    try:
        override = pre_turn_dispatch(
            str((request.get("messages") or [{}])[-1].get("content", "")),
        ).get("model_override")
    except Exception:
        override = None
    if override:
        request = dict(request)
        request["model"] = override
    return {"request": request}


def register(ctx: Any) -> None:
    ctx.register_hook("pre_gateway_dispatch", pre_gateway_dispatch_hook)
    ctx.register_hook("pre_llm_call", pre_llm_call_hook)
    ctx.register_middleware("llm_request", llm_request_middleware)
