"""Durable, single-use consent for a simulated effect.

Only a trusted gateway adapter may call this service with an authenticated numeric
Telegram user ID. Never publish decide() as an MCP tool with an actor_id argument.
No real external action is supported by this module.
"""
from __future__ import annotations

import hashlib
import json
import secrets
import sqlite3
import time
from contextlib import closing, contextmanager
from pathlib import Path
from typing import Callable


class ApprovalError(Exception):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _payload(target: str, arguments: dict) -> tuple[str, str]:
    if not isinstance(target, str) or not target or len(target) > 200:
        raise ApprovalError('INVALID_ARGUMENT')
    if not isinstance(arguments, dict):
        raise ApprovalError('INVALID_ARGUMENT')
    try:
        serialized = json.dumps({'action': 'simulate', 'target': target, 'arguments': arguments},
                                sort_keys=True, separators=(',', ':'), ensure_ascii=False, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ApprovalError('INVALID_ARGUMENT') from exc
    if len(serialized.encode('utf-8')) > 4096:
        raise ApprovalError('INVALID_ARGUMENT')
    return serialized, hashlib.sha256(serialized.encode('utf-8')).hexdigest()


class ApprovalStore:
    """SQLite gate: the simulated effect and token consumption commit together."""

    def __init__(self, path: Path, *, clock: Callable[[], float] = time.time):
        self.path = Path(path)
        self.clock = clock
        # The caller owns the storage directory and its access policy.
        with self._connect() as connection:
            connection.executescript('''
                CREATE TABLE IF NOT EXISTS approvals (
                    token_hash TEXT PRIMARY KEY,
                    actor_id INTEGER NOT NULL,
                    digest TEXT NOT NULL,
                    expires_at INTEGER NOT NULL,
                    status TEXT NOT NULL CHECK (status IN ('pending', 'cancelled', 'executed'))
                );
                CREATE TABLE IF NOT EXISTS simulated_effects (
                    token_hash TEXT PRIMARY KEY REFERENCES approvals(token_hash),
                    payload TEXT NOT NULL,
                    executed_at INTEGER NOT NULL
                );
            ''')

    @contextmanager
    def _connect(self):
        with closing(sqlite3.connect(self.path, timeout=10)) as connection:
            connection.execute('PRAGMA foreign_keys = ON')
            with connection:
                yield connection

    @staticmethod
    def _actor(actor_id: int):
        if type(actor_id) is not int or actor_id <= 0 or actor_id > 9223372036854775807:
            raise ApprovalError('INVALID_ARGUMENT')

    def request(self, *, actor_id: int, target: str, arguments: dict,
                ttl_seconds: int = 300) -> dict:
        """Stage a simulation; relay the opaque token only via the trusted channel."""
        self._actor(actor_id)
        if type(ttl_seconds) is not int or not 1 <= ttl_seconds <= 600:
            raise ApprovalError('INVALID_ARGUMENT')
        payload, digest = _payload(target, arguments)
        token = secrets.token_urlsafe(32)
        token_hash = hashlib.sha256(token.encode('ascii')).hexdigest()
        expires_at = int(self.clock()) + ttl_seconds
        with self._connect() as connection:
            connection.execute('INSERT INTO approvals VALUES (?, ?, ?, ?, ?)',
                               (token_hash, actor_id, digest, expires_at, 'pending'))
        return {'token': token, 'status': 'pending', 'digest': digest,
                'expires_at': expires_at,
                'summary': f'Simulated effect on {target}: {payload}; SHA-256 {digest}; expires {expires_at} UTC epoch.'}

    def decide(self, *, token: str, actor_id: int, target: str,
               arguments: dict, approve: bool) -> dict:
        """Gateway callback seam; no LLM-controlled actor or approval flag is trusted."""
        self._actor(actor_id)
        if type(approve) is not bool or not isinstance(token, str) or len(token) > 100:
            raise ApprovalError('INVALID_ARGUMENT')
        payload, digest = _payload(target, arguments)
        token_hash = hashlib.sha256(token.encode('utf-8')).hexdigest()
        with self._connect() as connection:
            connection.execute('BEGIN IMMEDIATE')
            row = connection.execute(
                'SELECT actor_id, digest, expires_at, status FROM approvals WHERE token_hash = ?',
                (token_hash,)).fetchone()
            if row is None or row[0] != actor_id or not secrets.compare_digest(row[1], digest):
                raise ApprovalError('APPROVAL_DENIED')
            if row[3] != 'pending':
                raise ApprovalError('APPROVAL_USED')
            if int(self.clock()) >= row[2]:
                raise ApprovalError('APPROVAL_EXPIRED')
            if approve:
                connection.execute('INSERT INTO simulated_effects VALUES (?, ?, ?)',
                                   (token_hash, payload, int(self.clock())))
            status = 'executed' if approve else 'cancelled'
            connection.execute('UPDATE approvals SET status = ? WHERE token_hash = ?', (status, token_hash))
            return {'status': status, 'digest': digest}

    def effects(self) -> list[dict]:
        """Inspect the local simulation ledger for tests and dry-run adapters."""
        with self._connect() as connection:
            rows = connection.execute('SELECT payload FROM simulated_effects ORDER BY rowid').fetchall()
        return [{'target': (data := json.loads(row[0]))['target'], 'arguments': data['arguments']}
                for row in rows]
