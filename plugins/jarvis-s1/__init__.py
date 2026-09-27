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
except Exception:  # Hermes non deve rompersi se Jarvis non e installato
    pre_turn_dispatch = None  # type: ignore[assignment]


def _text_of(event: Any) -> str:
    return str(getattr(event, "text", "") or "")


def pre_gateway_dispatch_hook(event: Any, gateway: Any,
                              session_store: Any, **kwargs: Any) -> Any:
    """Risposta diretta sicura, altrimenti None (= dispatch normale)."""
    if pre_turn_dispatch is None:
        return None
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
    try:
        source = getattr(event, "source", None)
        adapter = gateway.adapters[source.platform] if source is not None else None
        chat_id = getattr(source, "chat_id", None) if source is not None else None
        if adapter is None or chat_id is None:
            return None
        import asyncio
        send = adapter.send(chat_id, result["reply"])
        if asyncio.iscoroutine(send):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                loop = None
            if loop is not None:
                loop.create_task(send)
            else:
                asyncio.run(send)
    except Exception:
        return None
    return {"action": "skip", "reason": "jarvis_s1_direct"}


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
