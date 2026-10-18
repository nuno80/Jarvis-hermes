"""Shared state-dir, environment, and SQLite-connection helpers.

Ponytail: solo cio che serve a chiudere A01/A03/A04 senza cambiare
il comportamento runtime (pragma e transazioni restano dei chiamanti).
"""
from __future__ import annotations

from contextlib import contextmanager
import os
import platform
from pathlib import Path
import sqlite3
from typing import Iterator


def is_wsl() -> bool:
    """True se il processo gira dentro Windows Subsystem for Linux."""
    return platform.system() == "Linux" and "microsoft" in platform.release().lower()


def current_environment() -> str:
    """'wsl', 'windows' o 'linux', coerente in tutti i moduli."""
    if is_wsl():
        return "wsl"
    return "windows" if os.name == "nt" else "linux"


def ensure_state_dir(subpath: str | None = None) -> Path:
    """XDG state dir di jarvis-hermes con permessi 0o700."""
    state_home = Path(os.environ.get("XDG_STATE_HOME", Path.home() / ".local" / "state"))
    state_dir = state_home / "jarvis-hermes"
    if subpath:
        state_dir = state_dir / subpath
    state_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        state_dir.chmod(0o700)
    except OSError:
        pass
    return state_dir


def ensure_secure_file_permissions(path: Path) -> Path:
    """0o600 sul file se esiste; mai un'eccezione."""
    try:
        if path.exists():
            path.chmod(0o600)
    except OSError:
        pass
    return path


def resolve_state_file(filename: str) -> Path:
    """Path dentro la state dir protetta; 0o600 se il file esiste gia."""
    return ensure_secure_file_permissions(ensure_state_dir() / filename)


@contextmanager
def sqlite_connection(
    db_path: Path | str,
    *,
    timeout: float = 10.0,
    isolation_level: str | None = "",
) -> Iterator[sqlite3.Connection]:
    """Come sqlite3.connect ma chiude SEMPRE la connessione all'uscita.

    Preserva la semantica esistente: pragma, row_factory e transazioni
    restano dei chiamanti (niente WAL/FK forzati: backup.py copia solo
    *.sqlite3 e alcuni store fanno commit espliciti).
    """
    path = Path(db_path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    conn = sqlite3.connect(str(path), timeout=timeout, isolation_level=isolation_level)
    ensure_secure_file_permissions(path)
    try:
        conn.row_factory = sqlite3.Row
        if isolation_level is not None:
            with conn:
                yield conn
        else:
            # Autocommit / transazioni manuali (es. BEGIN IMMEDIATE in llm.py).
            yield conn
    finally:
        conn.close()
