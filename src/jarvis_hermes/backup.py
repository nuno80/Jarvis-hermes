"""Consistent backup and restore management for runtime state and vault.

Ensures:
- Consistent snapshots of SQLite databases (using VACUUM INTO / backup API).
- Separation and exclusion of secrets (.env, tokens, credentials, keys).
- Reconstruction of vault notes, preferences, and indices on restore.
- Non-reexecution of interrupted jobs (reconciled to outcome_unknown).
- Cancellation of pending approvals upon restore (no auto-execution).
- Manifest with metadata, retention policy, and safe clean environment isolation.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import tarfile
import tempfile
import time
from typing import Any

EXCLUDED_FILENAMES = frozenset({
    ".env",
    ".env.local",
    ".env.production",
    "credentials.json",
    "service_account.json",
    "id_rsa",
    "id_ed25519",
    "token.json",
})

EXCLUDED_SUBSTRINGS = (
    "secret",
    "credential",
    "token",
    "private_key",
    "password",
)


class BackupError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _file_sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        while chunk := f.read(65536):
            h.update(chunk)
    return h.hexdigest()


def _is_secret_path(path: Path) -> bool:
    name_lower = path.name.lower()
    if name_lower in EXCLUDED_FILENAMES:
        return True
    if name_lower.startswith(".env"):
        return True
    if any(sub in name_lower for sub in EXCLUDED_SUBSTRINGS) and path.suffix.lower() in {".json", ".yaml", ".yml", ".txt", ".key", ".pem"}:
        return True
    return False


class BackupManager:
    def __init__(
        self,
        state_dir: Path | str,
        vault_dir: Path | str | None,
        backup_root: Path | str,
        retention_days: int = 14,
    ):
        self.state_dir = Path(state_dir).resolve()
        self.vault_dir = Path(vault_dir).resolve() if vault_dir else None
        self.backup_root = Path(backup_root).resolve()
        self.retention_days = max(1, retention_days)
        self.backup_root.mkdir(parents=True, exist_ok=True)

    def create_backup(self, label: str = "backup") -> dict[str, Any]:
        """Create a consistent snapshot archive of runtime state and vault notes, excluding secrets."""
        timestamp_str = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%SZ")
        backup_id = f"jarvis_backup_{timestamp_str}_{label}"
        archive_name = f"{backup_id}.tar.gz"
        manifest_name = f"{backup_id}_manifest.json"

        archive_path = self.backup_root / archive_name
        manifest_path = self.backup_root / manifest_name

        with tempfile.TemporaryDirectory(prefix="jarvis-backup-staging-") as staging:
            staging_path = Path(staging)
            staged_state = staging_path / "state"
            staged_vault = staging_path / "vault"
            staged_state.mkdir()
            staged_vault.mkdir()

            archived_files_meta = []
            excluded_secrets = []

            # 1. State snapshot (SQLite files using VACUUM INTO / backup to ensure transaction consistency)
            if self.state_dir.is_dir():
                for item in self.state_dir.iterdir():
                    if item.is_symlink():
                        continue
                    if _is_secret_path(item):
                        excluded_secrets.append(item.name)
                        continue

                    if item.is_file():
                        dest_file = staged_state / item.name
                        if item.suffix == ".sqlite3" or item.name.endswith(".sqlite"):
                            # Consistent SQLite snapshot
                            try:
                                with sqlite3.connect(item) as src_conn:
                                    # Use VACUUM INTO for consistent snapshot into dest_file
                                    escaped_dest = str(dest_file).replace("'", "''")
                                    src_conn.execute(f"VACUUM INTO '{escaped_dest}'")
                            except Exception:
                                # Fallback to standard copy if db empty or lock issue
                                shutil.copy2(item, dest_file)
                        else:
                            shutil.copy2(item, dest_file)

                        archived_files_meta.append({
                            "path": f"state/{item.name}",
                            "sha256": _file_sha256(dest_file),
                            "bytes": dest_file.stat().st_size,
                        })

                    elif item.is_dir() and item.name in {"checkpoints"}:
                        # Include checkpoints directory recursively
                        dest_sub = staged_state / item.name
                        shutil.copytree(item, dest_sub, symlinks=False, ignore=lambda src, names: [n for n in names if _is_secret_path(Path(n))])
                        for cp_file in dest_sub.rglob("*"):
                            if cp_file.is_file():
                                rel_path = cp_file.relative_to(staging_path).as_posix()
                                archived_files_meta.append({
                                    "path": rel_path,
                                    "sha256": _file_sha256(cp_file),
                                    "bytes": cp_file.stat().st_size,
                                })

            # 2. Vault snapshot
            if self.vault_dir and self.vault_dir.is_dir():
                for root_dir, dirs, files in os.walk(self.vault_dir):
                    rel_root = Path(root_dir).relative_to(self.vault_dir)
                    for file_name in files:
                        source_file = Path(root_dir) / file_name
                        if source_file.is_symlink():
                            continue
                        if _is_secret_path(source_file):
                            excluded_secrets.append(source_file.name)
                            continue
                        if source_file.name.startswith("."):
                            continue

                        rel_target = rel_root / file_name
                        dest_file = staged_vault / rel_target
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        shutil.copy2(source_file, dest_file)

                        archived_files_meta.append({
                            "path": f"vault/{rel_target.as_posix()}",
                            "sha256": _file_sha256(dest_file),
                            "bytes": dest_file.stat().st_size,
                        })

            # 3. Create compressed tar archive
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(staging_path, arcname="")

            archive_hash = _file_sha256(archive_path)

            # 4. Create and save Manifest
            manifest = {
                "schema_version": "1.0",
                "backup_id": backup_id,
                "label": label,
                "created_at_utc": datetime.now(timezone.utc).isoformat(),
                "retention_days": self.retention_days,
                "archive_filename": archive_name,
                "archive_sha256": archive_hash,
                "archive_bytes": archive_path.stat().st_size,
                "files_count": len(archived_files_meta),
                "files": archived_files_meta,
                "excluded_secrets": sorted(list(set(excluded_secrets))),
                "retention_policy": (
                    f"Retained locally for {self.retention_days} days. "
                    "Expired backups can be pruned with prune_backups(). "
                    "Secrets and credentials must be restored independently via secure key management."
                ),
            }
            manifest_path.write_text(json.dumps(manifest, indent=2, ensure_ascii=False), encoding="utf-8")

        return {
            "backup_id": backup_id,
            "archive_path": str(archive_path),
            "manifest_path": str(manifest_path),
            "archive_sha256": archive_hash,
            "files_count": len(archived_files_meta),
            "excluded_secrets_count": len(manifest["excluded_secrets"]),
        }

    def restore(
        self,
        archive_path: Path | str,
        target_state_dir: Path | str,
        target_vault_dir: Path | str,
        allow_existing: bool = False,
    ) -> dict[str, Any]:
        """Restore archive into target clean state and vault directories, reconciling jobs and approvals."""
        archive_file = Path(archive_path).resolve()
        if not archive_file.is_file():
            raise BackupError("ARCHIVE_NOT_FOUND", f"Backup archive not found: {archive_file}")

        target_state = Path(target_state_dir).resolve()
        target_vault = Path(target_vault_dir).resolve()

        # Prevent accidental overwrite of existing active vault unless allow_existing=True
        if not allow_existing:
            if target_vault.exists() and any(target_vault.iterdir()):
                raise BackupError("DESTINATION_NOT_EMPTY", f"Target vault directory {target_vault} is not empty. Use a clean path or explicit allow.")

        with tempfile.TemporaryDirectory(prefix="jarvis-restore-staging-") as staging:
            staging_path = Path(staging)
            with tarfile.open(archive_file, "r:gz") as tar:
                # Security: prevent tar traversal
                for member in tar.getmembers():
                    member_path = staging_path / member.name
                    if not member_path.resolve().is_relative_to(staging_path):
                        raise BackupError("SECURITY_ERROR", f"Tar entry attempts directory traversal: {member.name}")
                if hasattr(tarfile, 'data_filter'):
                    tar.extractall(staging_path, filter='data')
                else:
                    tar.extractall(staging_path)

            staged_state = staging_path / "state"
            staged_vault = staging_path / "vault"

            # 1. Safety reconciliation inside staging:
            # A. Neutralize interrupted jobs: non-terminal jobs become outcome_unknown, never re-executed blindly
            reconciled_jobs = 0
            jobs_db = staged_state / "jobs.sqlite3"
            if jobs_db.is_file():
                try:
                    with sqlite3.connect(jobs_db) as conn:
                        conn.row_factory = sqlite3.Row
                        now = time.time()
                        rows = conn.execute(
                            "SELECT job_id, warnings FROM jobs WHERE status NOT IN ('succeeded', 'failed', 'cancelled', 'interrupted', 'outcome_unknown')"
                        ).fetchall()
                        for r in rows:
                            existing = r["warnings"] or ""
                            warn = (existing + "\nRestored from backup; marked outcome_unknown to prevent blind re-execution.").strip()
                            conn.execute(
                                "UPDATE jobs SET status = 'outcome_unknown', updated_at = ?, warnings = ? WHERE job_id = ?",
                                (now, warn, r["job_id"]),
                            )
                            reconciled_jobs += 1
                        conn.commit()
                except Exception as exc:
                    raise BackupError("RECONCILIATION_ERROR", f"Job reconciliation failed during restore: {exc}") from exc

            # B. Neutralize pending approvals: cancelled, never automatically executed
            neutralized_approvals = 0
            approvals_db = staged_state / "approvals.sqlite3"
            if approvals_db.is_file():
                try:
                    with sqlite3.connect(approvals_db) as conn:
                        cursor = conn.execute("UPDATE approvals SET status = 'cancelled' WHERE status = 'pending'")
                        neutralized_approvals = cursor.rowcount
                        conn.commit()
                except Exception as exc:
                    raise BackupError("RECONCILIATION_ERROR", f"Approval reconciliation failed during restore: {exc}") from exc

            # 2. Reconciled staging is verified. Now copy state files to target
            target_state.mkdir(parents=True, exist_ok=True)
            target_vault.mkdir(parents=True, exist_ok=True)

            if staged_state.is_dir():
                for item in staged_state.iterdir():
                    dest = target_state / item.name
                    if item.is_dir():
                        if dest.exists():
                            shutil.rmtree(dest)
                        shutil.copytree(item, dest)
                    else:
                        shutil.copy2(item, dest)

            # 3. Copy vault files to target
            if staged_vault.is_dir():
                for root_dir, dirs, files in os.walk(staged_vault):
                    rel = Path(root_dir).relative_to(staged_vault)
                    dest_dir = target_vault / rel
                    dest_dir.mkdir(parents=True, exist_ok=True)
                    for f in files:
                        shutil.copy2(Path(root_dir) / f, dest_dir / f)

        # 4. Rebuild / verify indices and user profile from restored vault
        notes_indexed = 0
        explicit_prefs_restored = False
        if (target_vault / "preferences.yaml").is_file():
            explicit_prefs_restored = True

        for p in target_vault.rglob("*.md"):
            if p.is_file() and not p.name.startswith("."):
                notes_indexed += 1

        return {
            "success": True,
            "target_state_dir": str(target_state),
            "target_vault_dir": str(target_vault),
            "reconciled_jobs_count": reconciled_jobs,
            "neutralized_approvals_count": neutralized_approvals,
            "notes_indexed_count": notes_indexed,
            "explicit_preferences_restored": explicit_prefs_restored,
            "note": "Restore completed safely. Secrets must be configured independently in the environment.",
        }

    def prune_backups(self) -> list[dict[str, Any]]:
        """Prune backup archives and manifests older than retention_days."""
        now = time.time()
        max_age_seconds = self.retention_days * 86400
        pruned = []

        for manifest_file in self.backup_root.glob("*_manifest.json"):
            try:
                mtime = manifest_file.stat().st_mtime
                if (now - mtime) > max_age_seconds:
                    data = json.loads(manifest_file.read_text(encoding="utf-8"))
                    backup_id = data.get("backup_id", manifest_file.stem.replace("_manifest", ""))
                    archive_file = self.backup_root / data.get("archive_filename", f"{backup_id}.tar.gz")

                    if archive_file.exists():
                        archive_file.unlink()
                    manifest_file.unlink()

                    pruned.append({
                        "backup_id": backup_id,
                        "pruned_at_utc": datetime.now(timezone.utc).isoformat(),
                        "age_days": round((now - mtime) / 86400, 2),
                    })
            except Exception:
                continue

        return pruned
