"""Persistent job lifecycle management, cancellation, and restart reconciliation."""
import json
from hashlib import sha256
import os
from pathlib import Path
import signal
import sqlite3
import subprocess
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
                    warnings TEXT,
                    process_pid INTEGER,
                    process_pgid INTEGER
                )
            ''')
            # Migration check: add columns if table already existed without them
            cursor = conn.execute("PRAGMA table_info(jobs)")
            cols = {row['name'] for row in cursor.fetchall()}
            if 'process_pid' not in cols:
                conn.execute('ALTER TABLE jobs ADD COLUMN process_pid INTEGER')
            if 'process_pgid' not in cols:
                conn.execute('ALTER TABLE jobs ADD COLUMN process_pgid INTEGER')
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
                'process_pid': row['process_pid'] if 'process_pid' in row.keys() else None,
                'process_pgid': row['process_pgid'] if 'process_pgid' in row.keys() else None,
                'metadata': json.loads(row['metadata_json']) if row['metadata_json'] else {},
                'result': json.loads(row['result_json']) if row['result_json'] else None,
                'warnings': row['warnings'] or ''
            }

    def register_process(self, job_id: str, pid: int, pgid: int | None = None) -> None:
        """Register the OS process / process group running this job. Checks cancellation first."""
        with self._connection() as conn:
            row = conn.execute('SELECT status FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            if not row:
                raise JobError('NOT_FOUND', f'Job {job_id!r} not found.')
            if row['status'] == 'cancelled':
                raise JobError('JOB_CANCELLED', f'Job {job_id!r} was cancelled; cannot run new steps or processes.')
            if row['status'] in TERMINAL_STATES:
                raise JobError('INVALID_STATE', f'Job {job_id!r} is already in terminal state {row["status"]!r}.')

            conn.execute('''
                UPDATE jobs SET
                    process_pid = ?,
                    process_pgid = ?,
                    updated_at = ?
                WHERE job_id = ?
            ''', (pid, pgid or pid, time.time(), job_id))
            conn.commit()

    def check_not_cancelled(self, job_id: str) -> None:
        """Verify that job has not been cancelled. Useful before executing subsequent effects."""
        with self._connection() as conn:
            row = conn.execute('SELECT status FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
            if not row:
                return  # If job not tracked in DB, it is not cancelled
            if row['status'] == 'cancelled':
                raise JobError('JOB_CANCELLED', f'Job {job_id!r} was cancelled.')

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

    def cancel_job(self, job_id: str, timeout_seconds: float = 3.0) -> dict[str, Any]:
        """Cancel a running job. Signals process group (TERM then KILL) and verifies termination."""
        with self._connection() as conn:
            row = conn.execute('SELECT status, last_step, effects_count, process_pid, process_pgid FROM jobs WHERE job_id = ?', (job_id,)).fetchone()
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

            pid = row['process_pid']
            pgid = row['process_pgid']

            process_stopped = None
            term_note = 'Subsequent steps cancelled; unreverted effects require explicit cleanup.'

            if pid is None:
                term_note += ' No process handle was associated with this job (process_stopped: null).'
            else:
                # Process handle exists: attempt termination and verify
                stopped = terminate_process_tree(pid=pid, pgid=pgid, timeout_seconds=timeout_seconds)
                if stopped:
                    process_stopped = True
                    term_note += f' Process {pid} (pgid {pgid}) verified terminated.'
                else:
                    process_stopped = False
                    term_note += f' Cancel requested for process {pid}, but active descendants remain or termination could not be verified within timeout.'

            return {
                'job_id': job_id,
                'status': 'cancelled',
                'process_stopped': process_stopped,
                'last_completed_step': row['last_step'] or 'none',
                'unreverted_effects_count': row['effects_count'],
                'note': term_note
            }

    @staticmethod
    def _is_process_alive(pid: int) -> bool:
        """Check if process with given pid is still alive (not terminated or zombie)."""
        return len(get_active_descendants(pid=pid, pgid=None)) > 0

    @classmethod
    def _terminate_and_verify_process(cls, pid: int, pgid: int | None, timeout_seconds: float = 3.0) -> bool:
        """Signal process group and descendants with SIGTERM, then SIGKILL if needed, and verify termination."""
        return terminate_process_tree(pid=pid, pgid=pgid, timeout_seconds=timeout_seconds)

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


def _is_process_alive_nt(pid: int) -> bool:
    try:
        out = subprocess.run(['tasklist', '/FI', f'PID eq {pid}'], capture_output=True, text=True, timeout=2)
        return str(pid) in out.stdout
    except Exception:
        return False


def _get_windows_children(parent_pid: int) -> list[int]:
    try:
        cmd = ['powershell', '-NoProfile', '-Command',
               f"(Get-CimInstance Win32_Process -Filter 'ParentProcessId = {parent_pid}').ProcessId"]
        res = subprocess.run(cmd, capture_output=True, text=True, timeout=3)
        if res.returncode == 0:
            return [int(line.strip()) for line in res.stdout.splitlines() if line.strip().isdigit()]
    except Exception:
        pass
    return []


def get_active_descendants(pid: int, pgid: int | None = None) -> list[int]:
    """Return list of active (non-zombie) process IDs belonging to pgid or descended from pid."""
    if os.name == 'nt':
        living: list[int] = []
        if _is_process_alive_nt(pid):
            living.append(pid)
        for child_pid in _get_windows_children(pid):
            if _is_process_alive_nt(child_pid) and child_pid not in living:
                living.append(child_pid)
        return living

    # POSIX:
    # 1. Try reaping direct child if it has already exited
    try:
        os.waitpid(pid, os.WNOHANG)
    except (ChildProcessError, OSError):
        pass

    # 2. On Linux, inspect /proc to find all non-zombie processes matching pgid or descendant of pid
    proc_dir = Path('/proc')
    if proc_dir.exists():
        active: set[int] = set()
        children_map: dict[int, list[int]] = {}
        for entry in proc_dir.iterdir():
            if entry.name.isdigit():
                try:
                    stat_content = (entry / 'stat').read_text()
                    comm, rest = stat_content.rsplit(') ', 1)
                    fields = rest.split()
                    state = fields[0]
                    ppid = int(fields[1])
                    pgrp = int(fields[2])
                    p = int(entry.name)
                    if state != 'Z':
                        if pgid and pgrp == pgid:
                            active.add(p)
                        else:
                            children_map.setdefault(ppid, []).append(p)
                            if p == pid:
                                active.add(p)
                except (OSError, IndexError, ValueError):
                    pass

        # If any descendants were forked outside pgid or pgid not set
        if pid in children_map or any(k in children_map for k in active):
            queue = [pid] + list(active)
            visited = set(queue)
            while queue:
                curr = queue.pop(0)
                for child in children_map.get(curr, []):
                    if child not in visited:
                        visited.add(child)
                        active.add(child)
                        queue.append(child)

        return sorted(active)

    # Fallback for non-Linux POSIX (e.g. macOS / BSD)
    active_set: set[int] = set()
    if pgid:
        try:
            os.killpg(pgid, 0)
            active_set.add(pgid)
        except (ProcessLookupError, OSError):
            pass
    if pid:
        try:
            os.kill(pid, 0)
            active_set.add(pid)
        except (ProcessLookupError, OSError):
            pass
    return sorted(active_set)


def terminate_process_tree(pid: int, pgid: int | None = None, timeout_seconds: float = 3.0) -> bool:
    """Terminate a process and all its descendants with escalation (TERM then KILL)
    and verify that no active descendants remain. Returns True if all stopped, False otherwise."""
    if not get_active_descendants(pid, pgid):
        return True

    if os.name == 'nt':
        try:
            subprocess.run(['taskkill', '/F', '/T', '/PID', str(pid)], capture_output=True, timeout=timeout_seconds)
        except Exception:
            pass
        for child in _get_windows_children(pid):
            try:
                subprocess.run(['taskkill', '/F', '/T', '/PID', str(child)], capture_output=True, timeout=timeout_seconds)
            except Exception:
                pass

        deadline = time.time() + timeout_seconds
        while time.time() < deadline:
            if not get_active_descendants(pid, pgid):
                return True
            time.sleep(0.05)
        return len(get_active_descendants(pid, pgid)) == 0

    # POSIX:
    effective_pgid = pgid if (pgid and hasattr(os, 'killpg')) else (pid if hasattr(os, 'killpg') else None)

    def _send_signal(sig: int) -> None:
        if effective_pgid:
            try:
                os.killpg(effective_pgid, sig)
            except (ProcessLookupError, OSError):
                pass
        for p in get_active_descendants(pid, pgid):
            try:
                os.kill(p, sig)
            except (ProcessLookupError, OSError):
                pass

    # 1. Send SIGTERM to entire process group / tree
    _send_signal(signal.SIGTERM)

    # Poll for graceful termination
    term_grace = min(1.0, timeout_seconds / 2.0)
    deadline = time.time() + term_grace
    while time.time() < deadline:
        if not get_active_descendants(pid, pgid):
            return True
        time.sleep(0.05)

    # 2. Descendants or process still active: escalate to SIGKILL
    _send_signal(signal.SIGKILL)

    kill_deadline = time.time() + max(0.5, timeout_seconds - term_grace)
    while time.time() < kill_deadline:
        if not get_active_descendants(pid, pgid):
            return True
        time.sleep(0.05)

    return len(get_active_descendants(pid, pgid)) == 0


def drain_process_pipes(proc: subprocess.Popen, timeout: float = 1.0) -> tuple[str, str]:
    """Drain stdout and stderr from proc with a strict timeout limit.
    Ensures pipe handles are closed so reading never hangs indefinitely."""
    out, err = "", ""
    try:
        raw_out, raw_err = proc.communicate(timeout=timeout)
        if raw_out is not None:
            out = raw_out.decode('utf-8', errors='replace') if isinstance(raw_out, bytes) else raw_out
        if raw_err is not None:
            err = raw_err.decode('utf-8', errors='replace') if isinstance(raw_err, bytes) else raw_err
    except subprocess.TimeoutExpired as exc:
        raw_out = exc.output
        raw_err = exc.stderr
        if raw_out is not None:
            out = raw_out.decode('utf-8', errors='replace') if isinstance(raw_out, bytes) else raw_out
        if raw_err is not None:
            err = raw_err.decode('utf-8', errors='replace') if isinstance(raw_err, bytes) else raw_err
    except Exception:
        pass
    finally:
        for pipe in (proc.stdout, proc.stderr, proc.stdin):
            if pipe:
                try:
                    pipe.close()
                except Exception:
                    pass
    return out, err
