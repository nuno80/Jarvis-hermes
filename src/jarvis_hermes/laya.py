"""Backend Laya (multilingual) per il System 1: spike valutativa issue #41.

Tracciabilita: issue #41 (spike, non cambia default/soglie/policy), D13,
sezione 7, appendice I, AT07/AT13/AT14. Riusa il seam ``decide(model_call=...)``
della #31; qwen3.5:4b resta default (ADR-0007).

Laya non genera testo ne logprob: espone ``POST /v1/systemone`` (laya-serve,
wire protocol Jev-compatibile) con risposte ``choice``/``score``/``noul`` e
``answer_confidence`` (max-p calibrato). Il gating va su ``answer_confidence``,
MAI su ``confidence`` (entropia normalizzata, scala diversa, non calibrata).

Mapping (b) dell'intesa: intent flat (29 INTENTS V1); 3 binary come choice a
2 opzioni (workaround bug noul #156 upstream); risk/complexity come score
1-5; handler/skill_hint da tabella codice derivata dal dataset (deterministica,
non occupa head budget). State = sola query raw, niente plumbing I1.
"""
from __future__ import annotations

import json
import math
import os
import urllib.request
from typing import Any, Callable

from .decision import HANDLERS, INTENTS, SKILLS

LAYA_MODEL = "multilingual"
DEFAULT_ENDPOINT = "http://localhost:8000/v1/systemone"
DEFAULT_TIMEOUT_S = 30.0

# Tabella intent -> (handler, skill_hint) derivata da tests/data/routing_it.jsonl
# (ogni intent del dataset mappa a un unico handler/skill). Deterministica:
# aritmetica e confronti nel codice, mai chiesti al modello (appendice I1).
INTENT_TABLE: dict[str, tuple[str, str]] = {
    "agenda_create": ("agenda_create", "calendar"),
    "agenda_read": ("agenda_read", "calendar"),
    "ambiguous": ("ask_clarification", "none"),
    "brainstorm": ("gemini_reasoning", "reasoning"),
    "code_architecture": ("gemini_reasoning", "reasoning"),
    "code_writing": ("gemini_reasoning", "reasoning"),
    "dangerous_ambiguous": ("ask_clarification", "none"),
    "debug_help": ("gemini_reasoning", "reasoning"),
    "device_status": ("device_status", "system_status"),
    "disk_usage": ("disk_usage", "system_status"),
    "email_read": ("email_read", "email"),
    "email_reply": ("email_reply", "email"),
    "explain_concept": ("gemini_reasoning", "reasoning"),
    "general_question": ("gemini_reasoning", "reasoning"),
    "git_push": ("git_push", "dev_tools"),
    "gui_action": ("gui_action", "desktop_automation"),
    "memory_correction": ("memory_correction", "memory"),
    "memory_search": ("memory_search", "memory"),
    "preference_update": ("preference_update", "memory"),
    "project_workflow": ("project_workflow", "dev_tools"),
    "repo_status": ("repo_status", "dev_tools"),
    "run_command": ("run_command", "admin"),
    "summarize": ("gemini_reasoning", "reasoning"),
    "travel_compare": ("travel_compare", "travel"),
    "travel_search": ("travel_search", "travel"),
    "voice_transcribe": ("voice_transcribe", "voice"),
    "web_form_submit": ("web_form_submit", "web_browsing"),
    "web_search": ("web_search", "web_browsing"),
    "writing": ("gemini_reasoning", "reasoning"),
}

# Skill secondarie per top3: skill primaria + fallback neutri (vocabolario SKILLS).
_FALLBACK_SKILLS = ("reasoning", "none")

BINARY_QUESTIONS = ("needs_clarification", "external_effect", "private_data")
BINARY_LABELS = {
    "needs_clarification": ("chiaro", "ambiguo"),
    "external_effect": ("nessun effetto esterno", "effetto esterno"),
    "private_data": ("non privato", "privato"),
}
RISK_LEVELS = ["rischio 1 minimo", "rischio 2 basso", "rischio 3 medio",
               "rischio 4 alto", "rischio 5 critico"]
COMPLEXITY_LEVELS = ["complessita 1 banale", "complessita 2 semplice",
                     "complessita 3 media", "complessita 4 alta",
                     "complessita 5 molto alta"]


class LayaError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


def build_questions() -> dict[str, Any]:
    """Domande Laya per lo smoke: intent flat + 3 binary anti-noul + 2 score."""
    questions: dict[str, Any] = {
        "intent": {
            "type": "choice",
            "instructions": "Quale intento descrive la richiesta utente?",
            "criteria": {intent: intent for intent in INTENTS},
        },
    }
    for key in BINARY_QUESTIONS:
        no, yes = BINARY_LABELS[key]
        # ponytail: choice a 2 opzioni invece di noul (bug #156 upstream: noul segue le label)
        questions[key] = {
            "type": "choice",
            "instructions": f"La richiesta utente: {yes}?",
            "criteria": {"no": no, "yes": yes},
        }
    questions["risk"] = {
        "type": "score",
        "instructions": "Livello di rischio della richiesta (1 minimo, 5 critico).",
        "criteria": RISK_LEVELS,
    }
    questions["complexity"] = {
        "type": "score",
        "instructions": "Complessita della richiesta (1 banale, 5 molto alta).",
        "criteria": COMPLEXITY_LEVELS,
    }
    return questions


class LayaClient:
    """Client stdlib-only per laya-serve (POST /v1/systemone, model=multilingual)."""

    def __init__(self, endpoint: str | None = None,
                 api_key: str | None = None,
                 timeout_s: float = DEFAULT_TIMEOUT_S):
        self.endpoint = endpoint or os.environ.get("LAYA_ENDPOINT_URL", DEFAULT_ENDPOINT)
        self.api_key = api_key or os.environ.get("LAYA_API_KEY")
        self.timeout_s = timeout_s

    def system_one(self, state: Any, questions: dict[str, Any],
                   model: str = LAYA_MODEL) -> dict[str, Any]:
        """Una chiamata systemOne; ritorna il dict {answers, usage, ...} o solleva LayaError."""
        payload = {"state": state, "questions": questions, "model": model}
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        req = urllib.request.Request(
            self.endpoint, data=json.dumps(payload).encode("utf-8"),
            headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_s) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except Exception as exc:
            raise LayaError("LAYA_UNAVAILABLE", f"laya-serve non raggiungibile: {exc}") from None
        if not isinstance(data, dict) or not isinstance(data.get("answers"), dict):
            raise LayaError("LAYA_BAD_RESPONSE", "risposta laya-serve non valida")
        return data


def _valid_conf(value: Any) -> float:
    """answer_confidence valida o NaN (fail-closed AT14: mai troncare, mai riparare)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return float("nan")
    conf = float(value)
    return conf if 0.0 <= conf <= 1.0 else float("nan")


def parse_answers(payload: dict[str, Any]) -> tuple[dict[str, Any], dict[str, float]]:
    """Traduci answers Laya -> (raw V1 per decide, confidence_override).

    Solleva LayaError se intent fuori vocabolario o confidence non valide:
    decide() scala a system_2 con invalid_output/invalid_confidence (AT14).
    """
    answers = payload.get("answers", {})
    intent_ans = answers.get("intent", {})
    intent = intent_ans.get("choice") if isinstance(intent_ans, dict) else None
    if intent not in INTENT_TABLE:
        raise LayaError("LAYA_BAD_INTENT", f"intent fuori vocabolario: {intent!r}")
    handler, skill = INTENT_TABLE[intent]

    def binary(key: str) -> bool:
        ans = answers.get(key, {})
        probs = ans.get("probabilities", {}) if isinstance(ans, dict) else {}
        return probs.get("yes", 0.0) > probs.get("no", 0.0)

    def score(key: str, levels: int = 5) -> int:
        ans = answers.get(key, {})
        raw = ans.get("score") if isinstance(ans, dict) else None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise LayaError("LAYA_BAD_SCORE", f"score non valido per {key}: {raw!r}")
        return min(levels, max(1, int(round(float(raw))) + 1))

    skill2 = next((s for s in _FALLBACK_SKILLS if s != skill), "none")
    raw = {
        "intent": intent,
        "handler": handler,
        "skill_hint": skill,
        "skill_hint_2": skill2,
        "skill_hint_3": "none" if skill2 != "none" else "reasoning",
        "needs_clarification": binary("needs_clarification"),
        "external_effect": binary("external_effect"),
        "private_data": binary("private_data"),
        "risk": score("risk"),
        "complexity": score("complexity"),
    }
    intent_conf = _valid_conf(intent_ans.get("answer_confidence"))
    override = {"intent": intent_conf, "handler": intent_conf, "skill_hint": intent_conf}
    if not all(math.isfinite(v) for v in override.values()):
        raise LayaError("LAYA_BAD_CONFIDENCE", "answer_confidence mancante o fuori [0,1]")
    if handler not in HANDLERS or skill not in SKILLS:
        raise LayaError("LAYA_BAD_TABLE", "tabella intent->handler/skill incoerente")
    return raw, override


def laya_model_call(client: LayaClient | None = None
                    ) -> Callable[..., tuple[str, list[dict[str, Any]], int, int]]:
    """Factory del seam decide(): (prompt, format) -> (testo JSON V1, [], prompt_tok, eval_tok).

    Il prompt contiene gia la query raw (PROMPT_TEMPLATE); la factory la riusa
    come state, senza logprob (confidence via confidence_override in decide).
    """
    client = client or LayaClient()

    def call(prompt: str, fmt: dict[str, Any], model: str,
             endpoint: str, timeout: float) -> tuple[str, list[dict[str, Any]], int, int]:
        marker = "Richiesta: "
        text = prompt.rsplit(marker, 1)[-1].strip() if marker in prompt else prompt.strip()
        payload = client.system_one({"text": text}, build_questions())
        raw, _override = parse_answers(payload)
        usage = payload.get("usage", {}) if isinstance(payload.get("usage"), dict) else {}
        tokens = int(usage.get("input_tokens") or 0)
        return json.dumps(raw, ensure_ascii=False), [], tokens, 0

    return call


def decide_with_laya(text: str, job_id: str = "interactive",
                     client: LayaClient | None = None,
                     **decide_kwargs: Any):
    """Un turno System 1 via Laya: decide() con override di confidence calibrata."""
    from .decision import build_v1_request, decide
    client = client or LayaClient()
    payload = client.system_one({"text": text}, build_questions())
    raw, override = parse_answers(payload)
    call = lambda prompt, fmt, model, endpoint, timeout: (  # noqa: E731
        json.dumps(raw, ensure_ascii=False), [], 0, 0)
    return decide(build_v1_request(text, job_id=job_id), model_call=call,
                  model_id=f"laya:{LAYA_MODEL}",
                  confidence_override=override, **decide_kwargs)
