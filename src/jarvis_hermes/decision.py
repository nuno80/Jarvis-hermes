"""System 1 locale pre-turno: contratto I1, runtime Ollama, politica in codice.

Tracciabilita: issue #31 (JARVIS-31), D13, sezione 7, appendice I, J10, AT07/AT13/AT14/AT15.
ADR 0005 (hook pre-turno), ADR 0007 (modello qwen3.5:4b).

Il System 1 e un decisore senza tool: non vede token di approvazione ne segreti
(AT13) e non concede permessi. Output vincolato via JSON schema Ollama
(``format``); confidenza derivata dai logprob delle etichette, mai da un numero
dichiarato dal modello. Output non valido, NaN, fuori range o oltre budget di
latenza -> escalation al System 2 (AT14).
"""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import math
import os
import platform
import shutil
import subprocess
import time
import urllib.request
from typing import Any, Callable

# Modello System 1 di default (ADR 0007). Override via env OLLAMA_MODEL.
DEFAULT_MODEL = "qwen3.5:4b"
DEFAULT_TIMEOUT_S = 30.0
SCHEMA_VERSION = "1.0"
SYSTEM1_VERSION = "system1-v1"
# Statistiche host per la risposta diretta pre-turno (ADR 0005): solo disco
# e diagnostica locale, nessun vault/segreto, mai permessi (AT13).

# Budget di latenza del fast path (appendice I4 / AT14): oltre -> escalation.
# Spec: default proposto 300 ms; alzato a 500 ms perche qwen3.5:4b live su
# RTX 5070 misura ~1.3s a caldo (ADR 0007: total p50 333ms solo modello).
LATENCY_BUDGET_MS = 500.0

# Domande V1 (appendice I1).
QUESTION_KEYS = [
    "intent", "handler", "skill_hint",
    "needs_clarification", "external_effect", "private_data",
    "risk", "complexity",
]

INTENTS = [
    "agenda_create", "agenda_read", "ambiguous", "brainstorm",
    "code_architecture", "code_writing", "dangerous_ambiguous", "debug_help",
    "device_status", "disk_usage", "email_read", "email_reply",
    "explain_concept", "general_question", "git_push", "gui_action",
    "memory_correction", "memory_search", "preference_update",
    "project_workflow", "repo_status", "run_command", "summarize",
    "travel_compare", "travel_search", "voice_transcribe",
    "web_form_submit", "web_search", "writing",
]

HANDLERS = [
    "agenda_create", "agenda_read", "ask_clarification", "device_status",
    "disk_usage", "email_read", "email_reply", "gemini_reasoning",
    "git_push", "gui_action", "memory_correction", "memory_search",
    "preference_update", "project_workflow", "repo_status", "run_command",
    "travel_compare", "travel_search", "voice_transcribe",
    "web_form_submit", "web_search",
]

SKILLS = [
    "admin", "calendar", "desktop_automation", "dev_tools", "email",
    "memory", "none", "reasoning", "system_status", "travel", "voice",
    "web_browsing",
]

# Lo schema vincola i tipi; i vocabolari chiusi restano validati in codice
# (AT14: il modello non si auto-valida, una decisione non valida si scala).
DECISION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "intent": {"type": "string"},
        "handler": {"type": "string"},
        "skill_hint": {"type": "string"},
        "skill_hint_2": {"type": "string"},
        "skill_hint_3": {"type": "string"},
        "needs_clarification": {"type": "boolean"},
        "external_effect": {"type": "boolean"},
        "private_data": {"type": "boolean"},
        "risk": {"type": "integer"},
        "complexity": {"type": "integer"},
    },
    "required": [
        "intent", "handler", "skill_hint", "skill_hint_2", "skill_hint_3",
        "needs_clarification", "external_effect", "private_data",
        "risk", "complexity",
    ],
}

PROMPT_TEMPLATE = (
    "Classifica la richiesta utente. Rispondi SOLO con JSON valido.\n"
    f"intent: uno di {','.join(INTENTS)}\n"
    f"handler: uno di {','.join(HANDLERS)}\n"
    "skill_hint,skill_hint_2,skill_hint_3: tre skill ordinate per rilevanza, "
    f"una di {','.join(SKILLS)}\n"
    "needs_clarification,external_effect,private_data: booleani. "
    "risk,complexity: interi 1-5.\n"
    "Regole: azione esterna o distruttiva -> external_effect=true, risk>=4. "
    "Ambiguo -> needs_clarification=true.\n"
    "Richiesta: {text}"
)


@dataclass
class Question:
    key: str
    type: str  # choice | score | binary
    options: list[str] | None = None


@dataclass
class DecisionRequest:
    state: dict[str, Any]
    questions: list[Question] = field(default_factory=list)
    schema_version: str = SCHEMA_VERSION
    request_id: str = "decision"
    job_id: str = "interactive"


@dataclass
class DecisionResult:
    answers: dict[str, Any]
    confidences: dict[str, float]
    model_id: str
    latency_ms: float
    tokens: int
    path_chosen: str  # system_1 | system_2 (escalation)
    escalation_reason: str | None = None
    valid: bool = True


def build_v1_questions() -> list[Question]:
    """Domande V1 dell'appendice I1."""
    return [
        Question(key="intent", type="choice", options=list(INTENTS)),
        Question(key="handler", type="choice", options=list(HANDLERS)),
        Question(key="skill_hint", type="choice", options=list(SKILLS)),
        Question(key="needs_clarification", type="binary"),
        Question(key="external_effect", type="binary"),
        Question(key="private_data", type="binary"),
        Question(key="risk", type="score", options=["1", "2", "3", "4", "5"]),
        Question(key="complexity", type="score", options=["1", "2", "3", "4", "5"]),
    ]


def build_v1_request(text: str, job_id: str = "interactive",
                     request_id: str = "decision") -> DecisionRequest:
    """DecisionRequest V1 da testo utente: state compatto, solo testo + budget."""
    return DecisionRequest(
        state={"text": text, "budget_tokens": 512},
        questions=build_v1_questions(),
        request_id=request_id,
        job_id=job_id,
    )


class DecisionError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


SAFE_DIRECT_HANDLERS = ("disk_usage", "device_status", "repo_status")


def _disk_free_gb(path: str) -> float | None:
    """Spazio libero in GiB su ``path``; None se non leggibile (fail-closed)."""
    try:
        return shutil.disk_usage(path).free / (1024.0 ** 3)
    except OSError:
        return None


def diagnose_local(disk_path: str = "/") -> dict[str, Any]:
    """Diagnostica sola lettura dell'host corrente, riusata da CLI e hook."""
    system = platform.system()
    release = platform.release().lower()
    is_wsl = system == "Linux" and "microsoft" in release
    free_gb = _disk_free_gb(disk_path)
    return {
        "schema_version": SCHEMA_VERSION,
        "host": {
            "system": system,
            "python": platform.python_version(),
            "is_wsl": is_wsl,
        },
        "disk": {"path": disk_path, "free_gb": free_gb},
    }


def direct_reply(result: DecisionResult) -> str | None:
    """Risposta diretta per handler sicuri ad alta confidenza; None altrimenti.

    Solo i tre handler di sola lettura con risposta locale (niente System 2,
    niente permessi). Qualsiasi fallimento -> None = escalation normale.
    """
    if not result.valid or result.path_chosen != "system_1":
        return None
    handler = result.answers.get("handler")
    if handler not in SAFE_DIRECT_HANDLERS:
        return None
    if result.confidences.get("overall", 0.0) < 0.70:
        return None
    if handler == "disk_usage":
        free_gb = _disk_free_gb("/")
        if free_gb is None:
            return None
        return f"Spazio libero: {free_gb:.1f} GiB."
    diag = diagnose_local()
    if handler == "device_status":
        host = diag["host"]
        plat = "WSL" if host["is_wsl"] else host["system"]
        return f"Macchina {plat} attiva (Python {host['python']})."
    if handler == "repo_status":
        proc = subprocess.run(
            ["git", "status", "--short", "--branch"],
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            return None
        out = proc.stdout.strip()
        return "Repo pulito." if not out else f"Stato repo:\n{out[:1000]}"
    return None


def load_thresholds(path: str | None = None) -> dict[str, float]:
    """Soglie per classe di azione da config (issue #38); fallback ai default."""
    defaults = {"readonly": 0.70, "protected": 0.95,
                "reasoning": 0.60, "clarify": 0.70}
    p = path or os.environ.get("JARVIS_ROUTING_THRESHOLDS", "config/routing_thresholds.json")
    try:
        with open(p, encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError):
        return defaults
    out = dict(defaults)
    for key in defaults:
        val = data.get(key) if isinstance(data, dict) else None
        if isinstance(val, bool) or not isinstance(val, (int, float)):
            continue
        if 0.0 <= float(val) <= 1.0:
            out[key] = float(val)
    return out


def action_class(result: DecisionResult) -> str:
    """Classe di azione per le soglie: protected > clarify > reasoning > readonly."""
    if not result.valid:
        return "clarify"
    answers = result.answers
    if answers.get("external_effect") or int(answers.get("risk", 1)) >= 4:
        return "protected"
    if answers.get("needs_clarification"):
        return "clarify"
    if int(answers.get("complexity", 1)) >= 3:
        return "reasoning"
    return "readonly"


def apply_policy(result: DecisionResult,
                 thresholds: dict[str, float] | None = None) -> DecisionResult:
    """Politica in codice (sezione 7): soglie per classe, fail-closed AT14.

    Regole deterministiche, mai decise dal modello: ambiguo -> chiarimento;
    effetto esterno/rischio alto -> System 2; complessita alta -> System 2;
    confidenza sotto soglia -> System 2. System 1 non concede permessi (AT13).
    """
    if not result.valid:
        return result
    thresholds = thresholds or load_thresholds()
    answers = result.answers
    conf = result.confidences.get("overall", float("nan"))
    if not isinstance(conf, (int, float)) or isinstance(conf, bool) \
            or not math.isfinite(conf) or not 0.0 <= conf <= 1.0:
        result.path_chosen = "system_2"
        result.escalation_reason = "invalid_confidence"
        result.valid = False
        return result
    if answers.get("needs_clarification"):
        result.path_chosen = "system_2"
        result.escalation_reason = "needs_clarification"
        return result
    if answers.get("external_effect") or int(answers.get("risk", 1)) >= 4:
        result.path_chosen = "system_2"
        result.escalation_reason = "protected_action"
        return result
    if int(answers.get("complexity", 1)) >= 4:
        result.path_chosen = "system_2"
        result.escalation_reason = "high_complexity"
        return result
    cls = action_class(result)
    if conf < thresholds.get(cls, 0.70):
        result.path_chosen = "system_2"
        result.escalation_reason = "low_confidence"
        return result
    result.path_chosen = "system_1"
    result.escalation_reason = None
    return result


def skill_suggestion(result: DecisionResult) -> str | None:
    """Riga 'Suggested skill to inspect first: X' per pre_llm_call (prefix cache safe)."""
    if not result.valid or result.path_chosen != "system_1":
        return None
    skill = result.answers.get("skill_hint")
    if skill in (None, "none") or skill not in SKILLS:
        return None
    return f"Suggested skill to inspect first: {skill}"


def _mean_logprob_for_span(tokens: list[dict[str, Any]], span_start: int,
                           span_end: int) -> float:
    """Media dei logprob sui token che coprono [span_start, span_end) del testo."""
    if span_end <= span_start:
        return float("nan")
    pos = 0
    vals: list[float] = []
    for tok in tokens:
        raw = tok.get("bytes")
        try:
            text = bytes(raw).decode("utf-8", errors="replace") if raw else tok.get("token", "")
        except Exception:
            text = tok.get("token", "")
        tlen = len(text)
        if pos + tlen > span_start and pos < span_end:
            lp = tok.get("logprob")
            if isinstance(lp, bool) or not isinstance(lp, (int, float)):
                return float("nan")
            vals.append(float(lp))
        pos += tlen
    if not vals:
        return float("nan")
    return sum(vals) / len(vals)


def label_confidence(response_text: str, logprob_tokens: list[dict[str, Any]],
                     label: str) -> float:
    """Confidenza di un'etichetta choice: exp(media logprob) sui suoi token.

    Fallisce chiusa (NaN) se l'etichetta non e nel testo o i logprob mancano.
    """
    if not label or not isinstance(response_text, str):
        return float("nan")
    start = response_text.find(f'"{label}"')
    if start < 0:
        return float("nan")
    mean_lp = _mean_logprob_for_span(logprob_tokens, start + 1, start + 1 + len(label))
    if not math.isfinite(mean_lp):
        return float("nan")
    return math.exp(mean_lp)


def _validate_answers(raw: Any) -> dict[str, Any] | None:
    """Valida i tipi + i vocabolari chiusi. Ritorna None se non valido (AT14)."""
    if not isinstance(raw, dict):
        return None
    try:
        intent = raw["intent"]
        handler = raw["handler"]
        skills = [raw["skill_hint"], raw["skill_hint_2"], raw["skill_hint_3"]]
        needs_cl = raw["needs_clarification"]
        ext = raw["external_effect"]
        priv = raw["private_data"]
        risk = raw["risk"]
        complexity = raw["complexity"]
    except KeyError:
        return None
    if intent not in INTENTS or handler not in HANDLERS:
        return None
    if any(s not in SKILLS for s in skills):
        return None
    if not all(type(v) is bool for v in (needs_cl, ext, priv)):
        return None
    if (type(risk) is not int or type(complexity) is not int
            or not 1 <= risk <= 5 or not 1 <= complexity <= 5):
        return None
    return {
        "intent": intent,
        "handler": handler,
        "skill_hint": skills[0],
        "skill_hints_top3": skills,
        "needs_clarification": needs_cl,
        "external_effect": ext,
        "private_data": priv,
        "risk": risk,
        "complexity": complexity,
    }


def ollama_call(prompt: str, fmt: dict[str, Any], model: str,
                endpoint: str, timeout_s: float,
                logprobs: bool = True) -> tuple[str, list[dict[str, Any]], int, int]:
    """Una chiamata Ollama con output vincolato. Ritorna (testo, logprobs, prompt_tok, eval_tok)."""
    payload: dict[str, Any] = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": False,
        "keep_alive": "10m",
        "format": fmt,
        "options": {"temperature": 0},
    }
    if logprobs:
        # ponytail: logprobs puri (senza top_logprobs) costano ~0ms extra; top_k solo se servira distribuzioni complete
        payload["logprobs"] = True
    req = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout_s) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return (
        data.get("response", ""),
        data.get("logprobs") or [],
        int(data.get("prompt_eval_count") or 0),
        int(data.get("eval_count") or 0),
    )


def decide(request: DecisionRequest,
           model_call: Callable[..., tuple[str, list[dict[str, Any]], int, int]] | None = None,
           model_id: str = DEFAULT_MODEL,
           endpoint: str = "http://localhost:11434/api/generate",
           timeout_s: float = DEFAULT_TIMEOUT_S,
           latency_budget_ms: float = LATENCY_BUDGET_MS) -> DecisionResult:
    """Esegue le domande V1 in una sola chiamata; fail-closed verso system_2 (AT14).

    ``model_call`` e il seam di test (prompt, format) -> (testo, logprobs, prompt_tok, eval_tok).
    Lo state non deve mai contenere token di approvazione o segreti (AT13):
    qui viaggia solo il testo utente.
    """
    start = time.perf_counter()
    text = request.state.get("text", "")
    if not isinstance(text, str) or not text.strip():
        return DecisionResult({}, {}, model_id, 0.0, 0, "system_2",
                              escalation_reason="empty_state", valid=False)
    call = model_call or ollama_call
    try:
        response_text, lp_tokens, prompt_tok, eval_tok = call(
            PROMPT_TEMPLATE.format(text=text.strip()), DECISION_SCHEMA,
            model_id, endpoint, timeout_s,
        )
        raw = json.loads(response_text)
    except Exception:
        latency_ms = (time.perf_counter() - start) * 1000.0
        return DecisionResult({}, {}, model_id, latency_ms, 0, "system_2",
                              escalation_reason="invalid_output", valid=False)

    latency_ms = (time.perf_counter() - start) * 1000.0
    if latency_ms > latency_budget_ms:
        return DecisionResult({}, {}, model_id, latency_ms, prompt_tok + eval_tok,
                              "system_2", escalation_reason="latency_budget", valid=False)

    answers = _validate_answers(raw)
    if answers is None:
        return DecisionResult({}, {}, model_id, latency_ms, prompt_tok + eval_tok,
                              "system_2", escalation_reason="invalid_output", valid=False)

    confidences: dict[str, float] = {}
    for key in ("intent", "handler", "skill_hint"):
        confidences[key] = label_confidence(response_text, lp_tokens, str(answers[key]))
    # ponytail: binary/score senza distribuzione token -> confidenza = media delle choice, documentata come euristica
    choice_confs = [c for c in confidences.values() if math.isfinite(c)]
    proxy = sum(choice_confs) / len(choice_confs) if choice_confs else float("nan")
    for key in ("skill_hint_2", "skill_hint_3", "needs_clarification",
                "external_effect", "private_data", "risk", "complexity"):
        confidences[key] = proxy
    confidences["overall"] = proxy

    if not math.isfinite(proxy) or not 0.0 <= proxy <= 1.0:
        return DecisionResult({}, {}, model_id, latency_ms, prompt_tok + eval_tok,
                              "system_2", escalation_reason="invalid_confidence", valid=False)

    return DecisionResult(
        answers=answers,
        confidences={k: round(float(v), 4) for k, v in confidences.items()},
        model_id=model_id,
        latency_ms=round(latency_ms, 2),
        tokens=prompt_tok + eval_tok,
        path_chosen="system_1",
        escalation_reason=None,
        valid=True,
    )


def pre_turn_dispatch(text: str,
                     job_id: str = "interactive",
                     model_call: Callable[..., tuple[str, list[dict[str, Any]], int, int]] | None = None,
                     thresholds: dict[str, float] | None = None,
                     telemetry_store: Any = None,
                     budget_tracker: Any = None,
                     **decide_kwargs: Any) -> dict[str, Any]:
    """Un turno System 1 completo: decide -> policy -> risposta/skill hook-ready.

    Funzione pura dict-in/dict-out per gli hook Hermes (ADR 0005):
    ``pre_gateway_dispatch`` usa ``reply`` (None = nessun bypass, dispatch
    normale); ``pre_llm_call`` usa ``skill_context`` (None = niente da
    iniettare); ``llm_request`` puo leggere ``model``. Il System 1 non vede
    token di approvazione ne segreti (AT13) e non concede permessi.
    Consumo e latenza vanno in budget (#36) e telemetria (#39).
    """
    # ponytail: regole deterministiche note (sezione 7.1) prima del modello: gratis, zero latenza
    from .router import FAST_PATH_RULES
    clean = text.strip() if isinstance(text, str) else ""
    for _pattern, intent, handler in FAST_PATH_RULES:
        if _pattern.search(clean):
            skill = {"disk_usage": "system_status", "device_status": "system_status"}.get(handler, "dev_tools")
            result = DecisionResult(
                answers={"intent": intent, "handler": handler, "skill_hint": skill},
                confidences={"overall": 1.0}, model_id="deterministic_rule",
                latency_ms=0.0, tokens=0, path_chosen="system_1")
            reply = direct_reply(result)
            questions = [q.key for q in build_v1_questions()]
            if budget_tracker is not None:
                try:
                    budget_tracker.record_usage(
                        job_id=job_id, provider="system_1_local",
                        model_id="deterministic_rule", prompt_tokens=max(1, len(text) // 4),
                        completion_tokens=0, cost_usd=0.0, is_estimated=True)
                except Exception:
                    pass
            if telemetry_store is not None:
                try:
                    from .projects import redact_secrets
                    telemetry_store.record_decision(
                        request=redact_secrets(text), domande=questions,
                        valori=result.answers, confidenze=result.confidences,
                        percorso=result.path_chosen, modello="deterministic_rule",
                        latenza_system_1_ms=0.0, latenza_first_msg_ms=0.0,
                        tokens=0, costo=0.0, esito="success")
                except Exception:
                    pass
            return {
                "path": "system_1",
                "target": "deterministic",
                "intent": intent,
                "handler": handler,
                "confidence": 1.0,
                "used_fast_path": reply is not None,
                "classifier": "deterministic_rule",
                "fallback_applied": False,
                "answers": {"intent": intent, "handler": handler, "skill_hint": skill},
                "confidences": {"overall": 1.0},
                "details": {"escalation_reason": None},
                "model": "deterministic_rule",
                "latency_ms": 0.0,
                "tokens": 0,
                "escalation_reason": None,
                "reply": reply,
                "skill_context": None,
                "model_override": None,
            }
    result = apply_policy(decide(build_v1_request(text, job_id=job_id),
                                 model_call=model_call, **decide_kwargs),
                          thresholds)
    questions = [q.key for q in build_v1_questions()]
    if budget_tracker is not None:
        try:
            budget_tracker.record_usage(
                job_id=job_id, provider="system_1_local",
                model_id=result.model_id, prompt_tokens=max(1, len(text) // 4),
                completion_tokens=result.tokens, cost_usd=0.0, is_estimated=True)
        except Exception:
            pass
    if telemetry_store is not None:
        try:
            from .projects import redact_secrets
            telemetry_store.record_decision(
                request=redact_secrets(text), domande=questions,
                valori=result.answers, confidenze=result.confidences,
                percorso=result.path_chosen, modello=result.model_id,
                latenza_system_1_ms=result.latency_ms,
                latenza_first_msg_ms=result.latency_ms, tokens=result.tokens,
                costo=0.0,
                esito="success" if result.valid else (result.escalation_reason or "escalated"))
        except Exception:
            pass
    reply = direct_reply(result)
    skill = skill_suggestion(result)
    # ponytail: chiavi legacy del router per compatibilita diagnostica (test_mcp)
    target = ("clarification" if result.escalation_reason == "needs_clarification"
              else "deterministic" if reply is not None
              else "reasoning_llm" if result.path_chosen == "system_2"
              else "system_1")
    return {
        "path": result.path_chosen,
        "target": target,
        "intent": result.answers.get("intent"),
        "handler": result.answers.get("handler"),
        "confidence": result.confidences.get("overall"),
        "used_fast_path": reply is not None,
        "classifier": f"system1_local:{result.model_id}",
        "fallback_applied": result.path_chosen == "system_2",
        "answers": result.answers,
        "confidences": result.confidences,
        "details": {"escalation_reason": result.escalation_reason,
                      "skill_hints_top3": result.answers.get("skill_hints_top3")},
        "model": result.model_id,
        "latency_ms": result.latency_ms,
        "tokens": result.tokens,
        "escalation_reason": result.escalation_reason,
        "reply": reply,
        "skill_context": skill if (skill and reply is None) else None,
        "model_override": None,
    }
