"""Persistent SQLite store for routing decisions and latency telemetry.

Traceability:
- Issue 38 / JARVIS-39
- Specification: Section 7, Appendix I3, B3, Section 10
- Table: routing_decisions
  (request, domande, valori, confidenze, percorso, modello, latenza_s1, latenza_first_msg,
   tokens, costo, esito, correzione_utente, timestamp)
- Features:
  * Secret redaction on logged requests and questions/values.
  * Configurable retention policy (cleanup older than retention_days).
  * Metrics calculation (p50/p95 latencies, escalation rate, path distribution, user corrections).
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import math
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

from .projects import redact_secrets


@dataclass
class RoutingTelemetryRecord:
    id: int
    request: str
    questions_json: str
    values_json: str
    confidences_json: str
    path_chosen: str
    model_id: str
    latency_system_1_ms: float
    latency_first_message_ms: float
    tokens: int
    cost_usd: float
    outcome: str
    user_correction: str | None
    created_at_utc: str


class RoutingDecisionStore:
    def __init__(self, db_path: Path | str, retention_days: int = 30):
        self.db_path = Path(db_path)
        self.retention_days = int(retention_days)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.db_path.parent.chmod(0o700)
        except OSError:
            pass
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS routing_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    request TEXT NOT NULL,
                    domande TEXT NOT NULL,
                    valori TEXT NOT NULL,
                    confidenze TEXT NOT NULL,
                    percorso TEXT NOT NULL,
                    modello TEXT NOT NULL,
                    latenza_system_1_ms REAL NOT NULL,
                    latenza_first_msg_ms REAL NOT NULL,
                    tokens INTEGER NOT NULL,
                    costo REAL NOT NULL,
                    esito TEXT NOT NULL,
                    correzione_utente TEXT,
                    created_at_utc TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_percorso ON routing_decisions(percorso)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_routing_created_at ON routing_decisions(created_at_utc)")
            try:
                self.db_path.chmod(0o600)
            except OSError:
                pass

    def record_decision(
        self,
        request: str,
        domande: list[str] | dict[str, Any] | None = None,
        valori: dict[str, Any] | list[Any] | None = None,
        confidenze: dict[str, float] | float | None = None,
        percorso: str = "deterministico",
        modello: str = "none",
        latenza_system_1_ms: float = 0.0,
        latenza_first_msg_ms: float = 0.0,
        tokens: int = 0,
        costo: float = 0.0,
        esito: str = "success",
        correzione_utente: str | None = None,
    ) -> int:
        """Record a routing decision turn, redacting all sensitive text fields."""
        redacted_request = redact_secrets(request)
        redacted_domande = redact_secrets(json.dumps(domande if domande is not None else []))
        redacted_valori = redact_secrets(json.dumps(valori if valori is not None else {}))
        conf_str = json.dumps(confidenze if confidenze is not None else {})
        redacted_correction = redact_secrets(correzione_utente) if correzione_utente else None

        now_utc = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute("""
                INSERT INTO routing_decisions (
                    request, domande, valori, confidenze, percorso, modello,
                    latenza_system_1_ms, latenza_first_msg_ms, tokens, costo,
                    esito, correzione_utente, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                redacted_request,
                redacted_domande,
                redacted_valori,
                conf_str,
                str(percorso),
                str(modello),
                float(latenza_system_1_ms),
                float(latenza_first_msg_ms),
                int(tokens),
                float(costo),
                str(esito),
                redacted_correction,
                now_utc,
            ))
            row_id = cursor.lastrowid
        return row_id

    def record_correction(self, decision_id: int, correzione_utente: str) -> bool:
        """Record user feedback / correction on a previous routing decision."""
        redacted = redact_secrets(correzione_utente)
        with self._get_connection() as conn:
            cursor = conn.execute(
                "UPDATE routing_decisions SET correzione_utente = ? WHERE id = ?",
                (redacted, decision_id)
            )
            return cursor.rowcount > 0

    def cleanup_old_records(self, retention_days: int | None = None) -> int:
        """Purge records older than retention period."""
        days = retention_days if retention_days is not None else self.retention_days
        if days <= 0:
            return 0
        cutoff = datetime.now(timezone.utc).timestamp() - (days * 86400)
        cutoff_iso = datetime.fromtimestamp(cutoff, tz=timezone.utc).isoformat()
        with self._get_connection() as conn:
            cursor = conn.execute(
                "DELETE FROM routing_decisions WHERE created_at_utc < ?",
                (cutoff_iso,)
            )
            return cursor.rowcount

    def get_decisions(self, limit: int = 100, offset: int = 0) -> list[dict[str, Any]]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM routing_decisions ORDER BY id DESC LIMIT ? OFFSET ?",
                (limit, offset)
            ).fetchall()
            return [dict(r) for r in rows]

    def generate_report(self) -> dict[str, Any]:
        """Generate metrics summary: p50/p95 latency, escalation rate, path distribution, corrections."""
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM routing_decisions ORDER BY id ASC").fetchall()

        total = len(rows)
        if total == 0:
            return {
                "total_decisions": 0,
                "path_distribution": {},
                "escalation_rate": 0.0,
                "latency_system_1_p50_ms": 0.0,
                "latency_system_1_p95_ms": 0.0,
                "latency_first_msg_p50_ms": 0.0,
                "latency_first_msg_p95_ms": 0.0,
                "total_tokens": 0,
                "total_cost_usd": 0.0,
                "corrections_count": 0,
                "corrections": [],
            }

        path_counts: dict[str, int] = {}
        s1_latencies: list[float] = []
        first_msg_latencies: list[float] = []
        total_tokens = 0
        total_cost = 0.0
        escalations = 0
        corrections = []

        for r in rows:
            p = r["percorso"]
            path_counts[p] = path_counts.get(p, 0) + 1
            s1_latencies.append(float(r["latenza_system_1_ms"]))
            first_msg_latencies.append(float(r["latenza_first_msg_ms"]))
            total_tokens += int(r["tokens"])
            total_cost += float(r["costo"])

            # Escalations include explicit escalation to System 2 or clarification when S1 could not handle directly
            if p in ("system_2", "reasoning_llm", "chiarimento", "clarification"):
                escalations += 1

            if r["correzione_utente"]:
                corrections.append({
                    "id": r["id"],
                    "request": r["request"],
                    "path_chosen": r["percorso"],
                    "correzione_utente": r["correzione_utente"],
                    "created_at_utc": r["created_at_utc"],
                })

        s1_latencies.sort()
        first_msg_latencies.sort()

        def percentile(data: list[float], pct: float) -> float:
            if not data:
                return 0.0
            idx = int(math.ceil((pct / 100.0) * len(data))) - 1
            idx = max(0, min(idx, len(data) - 1))
            return round(data[idx], 2)

        return {
            "total_decisions": total,
            "path_distribution": {k: {"count": v, "percentage": round((v / total) * 100, 1)} for k, v in path_counts.items()},
            "escalation_rate": round(escalations / total, 3),
            "latency_system_1_p50_ms": percentile(s1_latencies, 50),
            "latency_system_1_p95_ms": percentile(s1_latencies, 95),
            "latency_first_msg_p50_ms": percentile(first_msg_latencies, 50),
            "latency_first_msg_p95_ms": percentile(first_msg_latencies, 95),
            "total_tokens": total_tokens,
            "total_cost_usd": round(total_cost, 6),
            "corrections_count": len(corrections),
            "corrections": corrections,
        }
