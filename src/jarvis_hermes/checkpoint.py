"""File checkpoints and conflict-aware modifications for projects."""
import difflib
from hashlib import sha256
import os
from pathlib import Path
import sqlite3
import time
from typing import Any


class CheckpointError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class CheckpointManager:
    def __init__(self, storage_dir: str | Path | None = None):
        if storage_dir is None:
            state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
            self.storage_dir = state_home / 'jarvis-hermes' / 'checkpoints'
        else:
            self.storage_dir = Path(storage_dir)

        self.storage_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.storage_dir.chmod(0o700)
        except OSError:
            pass

        self.db_path = self.storage_dir / 'checkpoints.sqlite3'
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS checkpoints (
                    checkpoint_id TEXT PRIMARY KEY,
                    job_id TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    relative_path TEXT NOT NULL,
                    target_path TEXT NOT NULL,
                    initial_hash TEXT NOT NULL,
                    snapshot_path TEXT NOT NULL,
                    created_at REAL NOT NULL,
                    restored_at REAL
                )
            ''')
            conn.commit()

    @staticmethod
    def compute_hash(path: Path) -> str:
        if not path.exists():
            return ''
        h = sha256()
        with open(path, 'rb') as f:
            while chunk := f.read(65536):
                h.update(chunk)
        return h.hexdigest()

    def create_checkpoint(self, job_id: str, file_path: Path, project_id: str, relative_path: str) -> dict[str, Any]:
        target = file_path.resolve()
        initial_hash = self.compute_hash(target)
        timestamp = time.time()
        checkpoint_id = f"cp-{sha256(f'{job_id}:{project_id}:{relative_path}:{timestamp}'.encode()).hexdigest()[:16]}"
        
        # Save snapshot
        snapshots_dir = self.storage_dir / 'snapshots'
        snapshots_dir.mkdir(parents=True, exist_ok=True)
        snapshot_path = snapshots_dir / f"{checkpoint_id}.bak"

        if target.exists():
            snapshot_path.write_bytes(target.read_bytes())
        else:
            # File did not exist initially (creation checkpoint)
            snapshot_path.write_bytes(b'')

        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO checkpoints (
                    checkpoint_id, job_id, project_id, relative_path, target_path,
                    initial_hash, snapshot_path, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                checkpoint_id, job_id, project_id, relative_path, str(target),
                initial_hash, str(snapshot_path), timestamp
            ))
            conn.commit()

        return {
            'checkpoint_id': checkpoint_id,
            'job_id': job_id,
            'project_id': project_id,
            'relative_path': relative_path,
            'initial_hash': initial_hash,
            'created_at': timestamp
        }

    def safe_write_file(self, checkpoint_id: str, file_path: Path, expected_initial_hash: str, new_content: str) -> dict[str, Any]:
        target = file_path.resolve()
        current_hash = self.compute_hash(target)

        if current_hash != expected_initial_hash:
            raise CheckpointError('CONFLICT', f'File was modified externally (expected hash {expected_initial_hash}, got {current_hash}).')

        # Atomically write
        tmp_target = target.with_suffix(target.suffix + '.tmp')
        encoded = new_content.encode('utf-8')
        tmp_target.write_bytes(encoded)
        os.replace(tmp_target, target)

        new_hash = sha256(encoded).hexdigest()
        return {
            'written': True,
            'path': str(target),
            'previous_hash': current_hash,
            'new_hash': new_hash
        }

    def restore_checkpoint(self, checkpoint_id: str, expected_job_id: str | None = None, expected_current_hash: str | None = None) -> dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM checkpoints WHERE checkpoint_id = ?', (checkpoint_id,)).fetchone()
            if not row:
                raise CheckpointError('NOT_FOUND', f'Checkpoint {checkpoint_id!r} not found.')

            if expected_job_id and row['job_id'] != expected_job_id:
                raise CheckpointError('PERMISSION_DENIED', 'Checkpoint does not belong to expected job.')

            target = Path(row['target_path'])
            snapshot_path = Path(row['snapshot_path'])
            if not snapshot_path.exists():
                raise CheckpointError('RESOURCE_UNAVAILABLE', 'Checkpoint snapshot missing.')

            current_hash = self.compute_hash(target)
            if expected_current_hash and current_hash != expected_current_hash:
                raise CheckpointError('CONFLICT', 'Target file modified since last known job state.')

            old_content = target.read_text(encoding='utf-8', errors='replace') if target.exists() else ''
            restored_raw = snapshot_path.read_bytes()
            restored_content = restored_raw.decode('utf-8', errors='replace')

            # Produce diff
            diff_lines = list(difflib.unified_diff(
                old_content.splitlines(keepends=True),
                restored_content.splitlines(keepends=True),
                fromfile='modified_by_job',
                tofile='restored_from_checkpoint'
            ))
            diff_str = ''.join(diff_lines)

            if row['initial_hash'] == '':
                # File did not exist initially, restore removes it
                if target.exists():
                    target.unlink()
            else:
                tmp_target = target.with_suffix(target.suffix + '.rst')
                tmp_target.write_bytes(restored_raw)
                os.replace(tmp_target, target)

            now = time.time()
            conn.execute('UPDATE checkpoints SET restored_at = ? WHERE checkpoint_id = ?', (now, checkpoint_id))
            conn.commit()

            return {
                'restored': True,
                'checkpoint_id': checkpoint_id,
                'job_id': row['job_id'],
                'project_id': row['project_id'],
                'relative_path': row['relative_path'],
                'diff': diff_str,
                'restored_at': now
            }
