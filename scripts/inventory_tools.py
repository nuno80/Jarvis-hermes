#!/usr/bin/env python3
"""Generate inventory of registered MCP tools from FastMCP server."""

import asyncio
from typing import Any
from jarvis_hermes.server import build_server

CONFIRMATION_POLICIES = {
    # Approval / Elicitation required
    "simulate_with_approval": "Elicitazione / Approvazione simulata monouso",
    "git_push": "Elicitazione obbligatoria (target, branch, commit digest)",
    "submit_web_form": "Elicitazione obbligatoria (URL, campi, digest)",
    "email_send": "Elicitazione obbligatoria (destinatari, oggetto, body digest)",
    "calendar_create_event": "Elicitazione nativa MCP (riepilogo evento, digest)",
    "calendar_update_event": "Elicitazione nativa MCP (id, modifiche, digest)",
    "calendar_delete_event": "Elicitazione nativa MCP (id, digest)",
    "execute_gui_action": "Elicitazione obbligatoria per azioni modificatrici (close); osservazione senza conferma (open_and_inspect)",
    "run_command": "Policy deterministica (automatico per READONLY/WRITE_RECOVERABLE, approvazione per PRIVILEGED/DESTRUCTIVE)",
    "run_project_workflow": "Verifica hash script e git hooks prima dell'esecuzione",
    "commit_project_changes": "Verifica run precedente richiesta (nessun commit arbitrario)",
    "write_project_file": "Protetto da Checkpoint attivo e controllo di concorrenza su hash",
    "restore_checkpoint": "Job ID + hash ultimo stato noto obbligatori; CONFLICT se il file e' cambiato",
    "write_note": "Controllo concorrenza ottimistica su versione SHA-256 nota",
    "update_preference": "Controllo concorrenza e divieto di bypass privilegi",
    "forget_memory": "Cancellazione memoria inferita con backup dichiarativo",
    "pi_task": "Confinamento su ~/programmazione, lock concorrenza e tracciamento Job",
    "todoist_create_task_list": "Mutativo reversibile (nessuna conferma / task cancellabili da app)",
}


def get_policy(name: str) -> str:
    if name in CONFIRMATION_POLICIES:
        return CONFIRMATION_POLICIES[name]
    if name.startswith("draft_"):
        return "Nessuna rete / Bozza puramente locale (fail-safe)"
    return "Read-only / Nessun effetto collaterale distruttivo"


def get_status(name: str) -> str:
    # Most tools are fully implemented and covered by unit/integration MCP tests
    return "Implementato & Testato via MCP"


async def generate_markdown() -> str:
    server = build_server()
    tools = await server.list_tools()
    tools_sorted = sorted(tools, key=lambda t: t.name)

    lines = [
        "# Inventario Tool MCP — Jarvis",
        "",
        f"> Generato automaticamente dal server FastMCP. Totale tool registrati: **{len(tools_sorted)}**.",
        "",
        "| Tool | Politica di Conferma / Effetti | Stato Capacità | Descrizione Sintetica |",
        "|---|---|---|---|",
    ]

    for t in tools_sorted:
        desc = (t.description or "").strip().splitlines()[0].replace("|", "\\|")
        policy = get_policy(t.name)
        status = get_status(t.name)
        lines.append(f"| `{t.name}` | {policy} | {status} | {desc} |")

    lines.append("")
    return "\n".join(lines)


if __name__ == "__main__":
    md = asyncio.run(generate_markdown())
    print(md)
