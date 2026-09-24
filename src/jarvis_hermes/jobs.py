"""Persistent job lifecycle management, cancellation, and restart reconciliation."""
import json
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import time
from typing import Any

TERMINAL_STATES = {'succeeded', 'failed', 'cancelled', 'interrupted', 'outcome_unknown'}


class JobError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class JobStore:
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
            conn.execute('''
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    actor_id INTEGER NOT NULL,
                    target TEXT NOT NULL,
                    description TEXT NOT NULL,
                    status TEXT NOT NULL,
                    last_step TEXT,
                    effects_count INTEGER NOT NULL DEFAULT 0,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL,
                    metadata_json TEXT,
                    result_json TEXT,
                    warnings TEXT
                )
            ''')
            conn.commit()

    def create_job(self, actor_id: int, target: str, description: str, metadata: dict[str, Any] | None = None) -> dict[str, Any]:
        now = time.time()
        job_id = f"job-{sha256(f'{actor_id}:{target}:{now}'.encode()).hexdigest()[:16]}"
        with self._connection() as conn:
            conn.execute('''
                INSERT INTO jobs (
                    job_id, actor_id, target, description, status,
                    last_step, effects_count, created_at, updated_at, metadata_json
                ) VALUES (?, ?, ?, ?, 'received', NULL, 0, ?, ?, ?)
            ''', (
                job_id, actor_id, target, description, now, now, json.dumps(metadata or {})
            ))
            conn.commit()
        return self.get_job(job_id)

    def get_job(self, job_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute('SELECT * FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            if not row:
                raise JobError('NOT_FOUND', f'Job {job_id!r} not found.')
            
            created_at = row['created_at']
            updated_at = row['updated_at']
            elapsed = time.time() - created_at

            return {
                'job_id': row['job_id'],
                'actor_id': row['actor_id'],
                'target': row['target'],
                'description': row['description'],
                'status': row['status'],
                'last_step': row['last_step'],
                'effects_count': row['effects_count'],
                'created_at': created_at,
                'updated_at': updated_at,
                'elapsed_seconds': round(elapsed, 2),
                'progress_notice': elapsed >= 30.0,
                'metadata': json.loads(row['metadata_json']) if row['metadata_json'] else {},
                'result': json.loads(row['result_json']) if row['result_json'] else None,
                'warnings': row['warnings'] or ''
            }

    def update_status(self, job_id: str, status: str, step: str | None = None,
                      effects_count: int | None = None, result: dict[str, Any] | None = None,
                      warning: str | None = None) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute('SELECT status, effects_count, warnings FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            if not row:
                raise JobError('NOT_FOUND', f'Job {job_id!r} not found.')
            if row['status'] in TERMINAL_STATES:
                raise JobError('INVALID_STATE', f'Cannot update terminal job in state {row["status"]!r}.')

            now = time.time()
            new_effects = row['effects_count'] if effects_count is None else effects_count
            current_warnings = row['warnings'] or ''
            new_warnings = (current_warnings + '\n' + warning).strip() if warning else current_warnings

            conn.execute('''
                UPDATE jobs SET
                    status = ?,
                    last_step = COALESCE(?, last_step),
                    effects_count = ?,
                    updated_at = ?,
                    result_json = COALESCE(?, result_json),
                    warnings = ?
                WHERE job_id = ?
            ''', (
                status, step, new_effects, now, json.dumps(result) if result else None, new_warnings, job_id
            ))
            conn.commit()
        return self.get_job(job_id)

    def cancel_job(self, job_id: str) -> dict[str, Any]:
        with self._connection() as conn:
            row = conn.execute('SELECT status, last_step, effects_count FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            if not row:
                raise JobError('NOT_FOUND', f'Job {job_id!r} not found.')
            if row['status'] in TERMINAL_STATES:
                raise JobError('INVALID_STATE', f'Job is already in terminal state {row["status"]!r}.')

            now = time.time()
            conn.execute('''
                UPDATE jobs SET
                    status = 'cancelled',
                    updated_at = ?
                WHERE job_id = ?
            ''', (now, job_id))
            conn.commit()

            return {
                'job_id': job_id,
                'status': 'cancelled',
                'process_stopped': True,
                'last_completed_step': row['last_step'] or 'none',
                'unreverted_effects_count': row['effects_count'],
                'note': 'Subsequent steps cancelled; unreverted effects require explicit cleanup.'
            }

    def reconcile_on_startup(self) -> list[str]:
        """Mark active non-terminal jobs as outcome_unknown upon node restart."""
        reconciled = []
        with self._connection() as conn:
            rows = conn.execute(
                "SELECT job_id, warnings FROM jobs WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted', 'outcome_unknown')"
            ).fetchall()
            now = time.time()
            for r in rows:
                jid = r['job_id']
                existing = r['warnings'] or ''
                warn = (existing + '\nNode restarted during execution; outcome unknown without reconciliation.').strip()
                conn.execute(
                    "UPDATE jobs SET status = 'outcome_unknown', updated_at = ?, warnings = ? WHERE job_id = ?",
                    (now, warn, jid)
                )
                reconciled.append(jid)
            conn.commit()
        return reconciled
