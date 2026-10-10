"""Persistent reminders store, DST/timezone conversion, consultation, and reconciliation."""
from __future__ import annotations

import json
import os
from hashlib import sha256
from datetime import datetime, timezone
from pathlib import Path
import sqlite3
import time
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError


class ReminderError(Exception):
    def __init__(self, code: str, message: str, retryable: bool = False):
        self.code = code
        self.message = message
        self.retryable = retryable
        super().__init__(message)


def normalize_due_datetime(due_at_input: str | datetime, timezone_name: str) -> tuple[datetime, str]:
    """Parse and normalize a reminder schedule to absolute UTC datetime and local ISO string."""
    try:
        tz = ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, Exception) as exc:
        raise ReminderError("INVALID_TIMEZONE", f"Timezone {timezone_name!r} non valida: {exc}")

    if isinstance(due_at_input, datetime):
        dt = due_at_input
    else:
        # Tenta parsing ISO 8601
        cleaned = due_at_input.strip()
        try:
            dt = datetime.fromisoformat(cleaned)
        except ValueError as exc:
            raise ReminderError("INVALID_DATETIME", f"Formato data/ora non valido {cleaned!r}: {exc}")

    if dt.tzinfo is None:
        # Interpretato nel fuso orario locale richiesto
        dt_local = dt.replace(tzinfo=tz)
        dt_utc = dt_local.astimezone(timezone.utc)
    else:
        dt_utc = dt.astimezone(timezone.utc)
        dt_local = dt.astimezone(tz)

    return dt_utc, dt_local.isoformat()


class ReminderStore:
    def __init__(self, database_path: Path):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.database_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS reminders (
                    reminder_id TEXT PRIMARY KEY,
                    actor_id INTEGER NOT NULL,
                    text TEXT NOT NULL,
                    due_at_utc TEXT NOT NULL,
                    due_timestamp REAL NOT NULL,
                    timezone TEXT NOT NULL,
                    scheduled_local TEXT NOT NULL,
                    status TEXT NOT NULL,
                    delivery_status TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    warnings TEXT,
                    delivery_metadata_json TEXT
                )
            """)
            conn.commit()

    def create(
        self,
        actor_id: int,
        text: str,
        due_at: str | datetime,
        timezone_str: str = "Europe/Rome",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if not actor_id or actor_id <= 0:
            raise ReminderError("INVALID_REQUEST", "actor_id deve essere un intero positivo.")
        cleaned_text = (text or "").strip()
        if not cleaned_text:
            raise ReminderError("INVALID_REQUEST", "Il testo del reminder non può essere vuoto.")

        dt_utc, local_iso = normalize_due_datetime(due_at, timezone_str)
        now = time.time()
        due_ts = dt_utc.timestamp()

        rem_id = f"rem-{sha256(f'{actor_id}:{cleaned_text}:{due_ts}:{now}'.encode()).hexdigest()[:16]}"

        with self._connection() as conn:
            conn.execute(
                """
                INSERT INTO reminders (
                    reminder_id, actor_id, text, due_at_utc, due_timestamp,
                    timezone, scheduled_local, status, delivery_status,
                    created_at, updated_at, warnings, delivery_metadata_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, 'scheduled', 'pending', ?, ?, '', ?)
                """,
                (
                    rem_id,
                    actor_id,
                    cleaned_text,
                    dt_utc.isoformat(),
                    due_ts,
                    timezone_str,
                    local_iso,
                    now,
                    now,
                    json.dumps(metadata or {}),
                ),
            )
            conn.commit()

        return self.get(rem_id)

    def get(self, reminder_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            if not row:
                raise ReminderError("NOT_FOUND", f"Reminder {reminder_id!r} non trovato.")
            return self._row_to_dict(row)

    def list_reminders(
        self,
        actor_id: int | None = None,
        status: str | None = None,
        limit: int = 50,
    ) -> list[dict[str, Any]]:
        query = "SELECT * FROM reminders WHERE 1=1"
        params: list[Any] = []
        if actor_id:
            query += " AND actor_id = ?"
            params.append(actor_id)
        if status:
            query += " AND status = ?"
            params.append(status)
        query += " ORDER BY due_timestamp ASC LIMIT ?"
        params.append(limit)

        with self._connection() as conn:
            rows = conn.execute(query, params).fetchall()
            return [self._row_to_dict(r) for r in rows]

    def cancel(self, reminder_id: str, actor_id: int | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute("SELECT * FROM reminders WHERE reminder_id = ?", (reminder_id,)).fetchone()
            if not row:
                raise ReminderError("NOT_FOUND", f"Reminder {reminder_id!r} non trovato.")
            if actor_id and row["actor_id"] != actor_id:
                raise ReminderError("PERMISSION_DENIED", "Non autorizzato ad annullare questo reminder.")
            if row["status"] in ("cancelled", "fired", "missed_during_downtime"):
                raise ReminderError("INVALID_STATE", f"Il reminder è già nello stato terminale {row['status']!r}.")

            now = time.time()
            conn.execute(
                "UPDATE reminders SET status = 'cancelled', updated_at = ? WHERE reminder_id = ?",
                (now, reminder_id),
            )
            conn.commit()

        return self.get(reminder_id)

    def reconcile_on_startup(self, now_utc: datetime | None = None) -> list[dict[str, Any]]:
        """Identify reminders scheduled for a time that passed while the node was offline."""
        now_dt = now_utc or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        reconciled = []

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'scheduled' AND due_timestamp <= ?",
                (now_ts,),
            ).fetchall()
            now_epoch = time.time()
            for r in rows:
                rid = r["reminder_id"]
                warn = (r["warnings"] or "") + "\nScaduto a nodo spento; contrassegnato come missed_during_downtime."
                conn.execute(
                    """
                    UPDATE reminders SET
                        status = 'missed_during_downtime',
                        updated_at = ?,
                        warnings = ?
                    WHERE reminder_id = ?
                    """,
                    (now_epoch, warn.strip(), rid),
                )
                reconciled.append(rid)
            conn.commit()

        return [self.get(rid) for rid in reconciled]

    def dispatch_due(
        self,
        now_utc: datetime | None = None,
        sender_func: Callable[[int, str], dict[str, Any]] | None = None,
    ) -> list[dict[str, Any]]:
        """Find scheduled reminders that are now due, transition them, and invoke sender_func."""
        now_dt = now_utc or datetime.now(timezone.utc)
        now_ts = now_dt.timestamp()
        dispatched = []

        with self._connection() as conn:
            rows = conn.execute(
                "SELECT * FROM reminders WHERE status = 'scheduled' AND due_timestamp <= ? ORDER BY due_timestamp ASC",
                (now_ts,),
            ).fetchall()

        for r in rows:
            rid = r["reminder_id"]
            actor_id = r["actor_id"]
            text = r["text"]
            delivery_status = "delivered"
            warning_msg = None
            metadata = {}

            if sender_func:
                try:
                    res = sender_func(actor_id, text)
                    metadata = res or {}
                except (TimeoutError, ConnectionError) as exc:
                    delivery_status = "delivery_uncertain"
                    warning_msg = f"Invio Telegram incerto o timeout: {exc}"
                except Exception as exc:
                    delivery_status = "delivery_failed"
                    warning_msg = f"Invio Telegram fallito: {exc}"

            now_epoch = time.time()
            with self._connection() as conn:
                existing_warn = r["warnings"] or ""
                final_warn = (existing_warn + "\n" + (warning_msg or "")).strip()
                conn.execute(
                    """
                    UPDATE reminders SET
                        status = 'fired',
                        delivery_status = ?,
                        updated_at = ?,
                        warnings = ?,
                        delivery_metadata_json = ?
                    WHERE reminder_id = ?
                    """,
                    (delivery_status, now_epoch, final_warn, json.dumps(metadata), rid),
                )
                conn.commit()
            dispatched.append(self.get(rid))

        return dispatched

    def _row_to_dict(self, row: sqlite3.Row) -> dict[str, Any]:
        return {
            "reminder_id": row["reminder_id"],
            "actor_id": row["actor_id"],
            "text": row["text"],
            "due_at_utc": row["due_at_utc"],
            "due_timestamp": row["due_timestamp"],
            "timezone": row["timezone"],
            "scheduled_local": row["scheduled_local"],
            "status": row["status"],
            "delivery_status": row["delivery_status"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
            "warnings": row["warnings"] or "",
            "metadata": json.loads(row["delivery_metadata_json"]) if row["delivery_metadata_json"] else {},
        }


# Module level wrappers for direct server usage
def create_reminder(
    actor_id: int,
    text: str,
    due_at: str,
    timezone_str: str = "Europe/Rome",
    database_path: Path | None = None,
) -> dict[str, Any]:
    store = ReminderStore(_default_db_path(database_path))
    return store.create(actor_id=actor_id, text=text, due_at=due_at, timezone_str=timezone_str)


def list_reminders(
    actor_id: int | None = None,
    status: str | None = None,
    limit: int = 50,
    database_path: Path | None = None,
) -> list[dict[str, Any]]:
    store = ReminderStore(_default_db_path(database_path))
    return store.list_reminders(actor_id=actor_id, status=status, limit=limit)


def cancel_reminder(
    reminder_id: str,
    actor_id: int | None = None,
    database_path: Path | None = None,
) -> dict[str, Any]:
    store = ReminderStore(_default_db_path(database_path))
    return store.cancel(reminder_id=reminder_id, actor_id=actor_id)


def reconcile_reminders_on_startup(database_path: Path | None = None) -> list[dict[str, Any]]:
    store = ReminderStore(_default_db_path(database_path))
    return store.reconcile_on_startup()


def dispatch_due_reminders(
    sender_func: Callable[[int, str], dict[str, Any]] | None = None,
    database_path: Path | None = None,
) -> list[dict[str, Any]]:
    store = ReminderStore(_default_db_path(database_path))
    return store.dispatch_due(sender_func=sender_func)


def _default_db_path(path: Path | None) -> Path:
    if path:
        return path
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    state_dir = state_home / "jarvis-hermes"
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    return state_dir / "reminders.sqlite3"
