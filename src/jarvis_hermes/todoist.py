"""Todoist client and task list integration (Issue #47 / ADR 0016).

Permette la creazione di liste (progetti) e task su Todoist.
Fail-fast senza credenziali: solleva PROVIDER_NOT_CONFIGURED.
"""

from __future__ import annotations

import json
import os
import socket
import urllib.error
import urllib.request
from typing import Any


class TodoistError(Exception):
    """Eccezione tipizzata per errori del provider Todoist."""

    def __init__(self, code: str, message: str, retryable: bool = False):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable


class TodoistClient:
    """Client minimale per le API REST v2 di Todoist."""

    def __init__(
        self,
        api_token: str | None = None,
        base_url: str = "https://api.todoist.com/rest/v2",
        timeout_seconds: float = 10.0,
    ):
        self.api_token = (api_token if api_token is not None else os.environ.get("TODOIST_API_TOKEN", "")).strip()
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds

    def _ensure_token(self) -> str:
        if not self.api_token:
            raise TodoistError(
                "PROVIDER_NOT_CONFIGURED",
                "Nessun token Todoist configurato. Imposta la variabile d'ambiente "
                "TODOIST_API_TOKEN con un API token valido (da Impostazioni > Integrazioni > "
                "Sviluppatori su Todoist). Non verranno create liste fittizie.",
                retryable=False,
            )
        return self.api_token

    def _request(self, method: str, endpoint: str, payload: dict[str, Any] | None = None) -> Any:
        token = self._ensure_token()
        url = f"{self.base_url}/{endpoint.lstrip('/')}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        headers = {
            "Authorization": f"Bearer {token}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "Jarvis-Hermes/1.0",
        }
        req = urllib.request.Request(url, data=data, headers=headers, method=method)

        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                status = resp.status
                body = resp.read().decode("utf-8")
                if not body.strip():
                    return {}
                return json.loads(body)
        except urllib.error.HTTPError as exc:
            if exc.code == 401:
                raise TodoistError(
                    "AUTHENTICATION_FAILED",
                    "Autenticazione Todoist fallita: token non valido o scaduto.",
                    retryable=False,
                ) from exc
            if exc.code == 429:
                raise TodoistError(
                    "RATE_LIMIT_EXCEEDED",
                    "Rate limit Todoist superato: riprovare più tardi.",
                    retryable=True,
                ) from exc
            if 400 <= exc.code < 500:
                raise TodoistError(
                    "INVALID_PARAMETER",
                    f"Richiesta Todoist non valida ({exc.code}): {exc.reason}",
                    retryable=False,
                ) from exc
            raise TodoistError(
                "PROVIDER_ERROR",
                f"Errore server Todoist ({exc.code}): {exc.reason}",
                retryable=True,
            ) from exc
        except (socket.timeout, TimeoutError) as exc:
            raise TodoistError(
                "PROVIDER_ERROR",
                "Timeout durante la connessione alle API di Todoist.",
                retryable=True,
            ) from exc
        except urllib.error.URLError as exc:
            raise TodoistError(
                "PROVIDER_ERROR",
                f"Errore di rete contattando Todoist: {exc.reason}",
                retryable=True,
            ) from exc
        except json.JSONDecodeError as exc:
            raise TodoistError(
                "PROVIDER_ERROR",
                "Risposta malformata ricevuta dalle API di Todoist.",
                retryable=False,
            ) from exc

    def create_task_list(
        self,
        name: str,
        tasks: list[str] | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        """Crea una lista (progetto) o task associati su Todoist.

        Se `name` è specificato e diverso da "inbox", crea un nuovo progetto.
        Se `project_id` è specificato, aggiunge i task al progetto indicato.
        Se `tasks` è una lista di stringhe, crea ciascun task nel progetto di destinazione.
        """
        # Fail-fast se il token manca, ancor prima della validazione
        self._ensure_token()

        clean_name = name.strip() if isinstance(name, str) else ""
        clean_tasks = (
            [t.strip() for t in tasks if isinstance(t, str) and t.strip()]
            if tasks
            else []
        )

        if not clean_name and not clean_tasks and not project_id:
            raise TodoistError(
                "INVALID_PARAMETER",
                "Specificare almeno il nome della lista o uno o più task.",
                retryable=False,
            )

        # 1. Risolvi o crea il progetto di destinazione
        target_project_id: str | None = None
        project_data: dict[str, Any]

        if project_id:
            target_project_id = project_id
            project_data = {"id": project_id, "name": clean_name or project_id}
        elif clean_name.lower() in ("inbox", "inbox default"):
            target_project_id = None
            project_data = {"id": "inbox", "name": "Inbox"}
        elif clean_name:
            res = self._request("POST", "/projects", payload={"name": clean_name})
            target_project_id = str(res.get("id"))
            project_data = {
                "id": target_project_id,
                "name": res.get("name", clean_name),
                "url": res.get("url"),
            }
        else:
            target_project_id = None
            project_data = {"id": "inbox", "name": "Inbox"}

        # 2. Crea i task nel progetto
        created_tasks: list[dict[str, Any]] = []
        for task_content in clean_tasks:
            task_payload: dict[str, Any] = {"content": task_content}
            if target_project_id and target_project_id != "inbox":
                task_payload["project_id"] = target_project_id
            task_res = self._request("POST", "/tasks", payload=task_payload)
            created_tasks.append(
                {
                    "id": str(task_res.get("id")),
                    "content": task_res.get("content", task_content),
                    "url": task_res.get("url"),
                }
            )

        return {
            "project": project_data,
            "tasks": created_tasks,
            "tasks_created": len(created_tasks),
            "url": project_data.get("url"),
        }
