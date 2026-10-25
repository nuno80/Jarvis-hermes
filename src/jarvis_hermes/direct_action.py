"""Slice list_create: eligibility, validazione argomenti LLM, rewrite request, esecuzione MCP.

Issue #64 / ADR 0017 (corretto): niente parsing regex del linguaggio naturale.
Il router deterministico decide SOLO l'instradamento; gli argomenti
``{name, tasks[]}`` li produce il System 2/LLM; qui si valida in modo
deterministico, si forza la scelta del tool via ``llm_request`` e si esegue
una sola volta il tool allowlisted con verifica dell'esito.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Callable

logger = logging.getLogger(__name__)

TODOIST_CREATE_TASK_LIST_TOOL = "mcp__jarvis__todoist_create_task_list"
TODOIST_CREATE_TASK_LIST_SHORT = "todoist_create_task_list"
LIST_CREATE_NAME = "Spesa"
MAX_TASKS_PER_CALL = 50
MAX_TASK_CHARS = 200

_LIST_CREATE_TOOL_SUFFIXES = (
    TODOIST_CREATE_TASK_LIST_TOOL,
    f"mcp__jarvis__{TODOIST_CREATE_TASK_LIST_SHORT}",
    TODOIST_CREATE_TASK_LIST_SHORT,
)


def is_list_create_eligible(dispatch: dict[str, Any] | None) -> bool:
    """Gate di routing: true solo per list_create deterministico conf 1.0."""
    if not isinstance(dispatch, dict):
        return False
    confidence = dispatch.get("confidence")
    return (
        dispatch.get("path") == "system_1"
        and dispatch.get("target") == "deterministic"
        and dispatch.get("intent") == "list_create"
        and dispatch.get("handler") in (None, "list_create")
        and not isinstance(confidence, bool)
        and confidence == 1.0
        and dispatch.get("reply") is None
        and dispatch.get("escalation_reason") is None
    )


def validate_list_create_arguments(candidate: Any) -> dict[str, Any] | None:
    """Valida argomenti prodotti dall'LLM; ritorna ``{"name","tasks"}`` puliti o None.

    Nessuna euristica linguistica: solo forma, limiti e policy dello slice
    (nome ``Spesa``, 1..50 task non vuoti, niente duplicati case-insensitive).
    Qualsiasi dubbio -> None = fallback al Reasoner prima di ogni effetto.
    """
    if not isinstance(candidate, dict) or set(candidate) != {"name", "tasks"}:
        return None
    name = candidate.get("name")
    tasks = candidate.get("tasks")
    if not isinstance(name, str) or name.strip() != LIST_CREATE_NAME:
        return None
    if not isinstance(tasks, list) or not 1 <= len(tasks) <= MAX_TASKS_PER_CALL:
        return None
    cleaned: list[str] = []
    for task in tasks:
        if not isinstance(task, str):
            return None
        item = " ".join(task.split())
        if not item or len(item) > MAX_TASK_CHARS:
            return None
        cleaned.append(item)
    lowered = [item.casefold() for item in cleaned]
    if len(set(lowered)) != len(lowered):
        return None
    return {"name": LIST_CREATE_NAME, "tasks": cleaned}


def build_list_create_action(arguments: dict[str, Any]) -> dict[str, Any]:
    """Argomenti validati -> azione MCP eseguibile (solleva ValueError se invalidi)."""
    validated = validate_list_create_arguments(arguments)
    if validated is None:
        raise ValueError("Invalid list_create arguments")
    return {"tool": TODOIST_CREATE_TASK_LIST_TOOL, "arguments": validated}


def _tool_entry_name(tool: Any) -> str:
    if not isinstance(tool, dict):
        return ""
    fn = tool.get("function")
    if isinstance(fn, dict) and isinstance(fn.get("name"), str):
        return fn["name"]
    name = tool.get("name")
    return name if isinstance(name, str) else ""


def constrain_llm_request_for_list_create(
    request: Any, dispatch: dict[str, Any] | None
) -> dict[str, Any] | None:
    """Rewrite pura per il middleware ``llm_request``: un solo tool + scelta forzata.

    Ritorna la nuova request se lo slice e' eleggibile e il tool e' presente,
    altrimenti None (= nessun rewrite, il turno prosegue normale). Non esegue
    I/O, non chiama LLM: resta nel budget fast del middleware.
    """
    if not is_list_create_eligible(dispatch):
        return None
    if not isinstance(request, dict):
        return None
    tools = request.get("tools")
    if not isinstance(tools, list) or not tools:
        return None
    filtered = [t for t in tools if _tool_entry_name(t) in _LIST_CREATE_TOOL_SUFFIXES]
    if not filtered:
        return None
    rewritten = dict(request)
    rewritten["tools"] = filtered
    # "required" = il modello deve chiamare un tool; essendocene uno solo,
    # deve chiamare proprio todoist_create_task_list. Forma portabile tra
    # provider (la traduzione OpenAI->Anthropic e' a carico dell'host).
    rewritten["tool_choice"] = "required"
    return rewritten


def _mcp_envelope(raw: str | dict[str, Any]) -> dict[str, Any]:
    payload = json.loads(raw) if isinstance(raw, str) else raw
    if not isinstance(payload, dict):
        raise ValueError("MCP response is not an object")
    if "ok" in payload:
        return payload
    envelope = payload.get("structuredContent", payload.get("result"))
    if isinstance(envelope, str):
        envelope = json.loads(envelope)
    if not isinstance(envelope, dict):
        raise ValueError("MCP envelope missing")
    return envelope


def _verified_data(envelope: dict[str, Any], expected_tasks: int) -> dict[str, Any]:
    if envelope.get("ok") is not True:
        error = envelope.get("error")
        code = error.get("code") if isinstance(error, dict) else None
        raise ValueError(code or "MCP_ERROR")
    data = envelope.get("data")
    if not isinstance(data, dict) or not isinstance(data.get("project"), dict):
        raise ValueError("INVALID_MCP_ENVELOPE")
    tasks = data.get("tasks")
    if (
        not str(data["project"].get("id") or "").strip()
        or not isinstance(tasks, list)
        or data.get("tasks_created") != expected_tasks
        or len(tasks) != expected_tasks
        or any(not isinstance(task, dict) or not str(task.get("id") or "").strip()
               or not str(task.get("content") or "").strip() for task in tasks)
    ):
        raise ValueError("INCOMPLETE_MCP_EFFECT")
    return data


def _record_terminal(job_store: Any, job_id: str, status: str, result: dict[str, Any],
                     effects_count: int | None = None) -> None:
    try:
        job_store.update_status(
            job_id, status, step=f"direct_action_{status}",
            effects_count=effects_count, result=result)
    except Exception as exc:
        # La write esterna e' gia' avvenuta: mai rilanciare nel turno e rischiare un replay.
        logger.error("direct action job update failed job=%s: %s", job_id, exc)


def execute_direct_action(
    action: dict[str, Any], *, job_id: str, job_store: Any,
    dispatch_fn: Callable[[str, dict[str, Any]], str | dict[str, Any]],
) -> dict[str, Any]:
    """Esegue una sola volta il solo tool allowlisted e chiude il job con l'esito osservato."""
    arguments = action.get("arguments")
    tasks = arguments.get("tasks") if isinstance(arguments, dict) else None
    if (
        action.get("tool") != TODOIST_CREATE_TASK_LIST_TOOL
        or validate_list_create_arguments(arguments) is None
        or not isinstance(tasks, list)
    ):
        raise ValueError("Invalid direct action")

    job_store.update_status(job_id, "running", step="direct_action_started")
    started = time.perf_counter()
    call_arguments = {**arguments, "request_id": job_id}
    try:
        envelope = _mcp_envelope(dispatch_fn(action["tool"], call_arguments))
        data = _verified_data(envelope, len(tasks))
    except Exception as exc:
        latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
        code = str(exc)[:80] or "OUTCOME_UNKNOWN"
        result = {"tool": action["tool"], "request_id": job_id,
                  "error": {"code": code}, "latency_ms": latency_ms}
        _record_terminal(job_store, job_id, "outcome_unknown", result)
        return {
            "status": "outcome_unknown", "reply": (
                "⚠️ Todoist non ha confermato l'esito completo della lista «Spesa». "
                "Non ho ripetuto l'operazione per evitare duplicati; controlla Todoist "
                f"prima di riprovare (job {job_id})."),
            "error": {"code": code}, "latency_ms": latency_ms,
        }

    latency_ms = round((time.perf_counter() - started) * 1000.0, 2)
    result = {"tool": action["tool"], "request_id": job_id,
              "data": data, "latency_ms": latency_ms}
    effects_count = 1 + data["tasks_created"]
    _record_terminal(job_store, job_id, "succeeded", result, effects_count)
    contents = [task["content"] for task in data["tasks"]]
    return {
        "status": "succeeded",
        "reply": f"✅ Lista «Spesa» creata su Todoist con {len(contents)} task: "
                 + ", ".join(contents) + ".",
        "data": data, "error": None, "latency_ms": latency_ms,
    }
