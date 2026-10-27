"""Tests for backup creation, consistent snapshots, restore to clean environment,
version preservation, index reconstruction, and security/secret separation (Issue #29 / J18).
"""
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest.mock import patch
from pathlib import Path

from jarvis_hermes.backup import BackupManager, BackupError
from jarvis_hermes.jobs import JobStore
from jarvis_hermes.approval import ApprovalStore
from jarvis_hermes.vault import Vault


class BackupRestoreTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.state_dir = self.root / "state"
        self.vault_dir = self.root / "vault"
        self.backup_dir = self.root / "backups"
        self.restore_dir = self.root / "clean_restore"

        self.state_dir.mkdir(parents=True)
        self.vault_dir.mkdir(parents=True)
        self.backup_dir.mkdir(parents=True)
        self.restore_dir.mkdir(parents=True)

        # Setup sample vault with preferences and notes
        (self.vault_dir / "preferences.yaml").write_text("travel:\n  preferred_airline: BA\n", encoding="utf-8")
        (self.vault_dir / "Projects").mkdir()
        (self.vault_dir / "Projects" / "alpha.md").write_text("# Project Alpha\nImportant notes.", encoding="utf-8")
        (self.vault_dir / "Knowledge").mkdir()
        (self.vault_dir / "Knowledge" / "facts.md").write_text("---\ntype: preference\ndomain: general\n---\nFacts note", encoding="utf-8")

        # Setup sample state databases
        self.job_store = JobStore(self.state_dir / "jobs.sqlite3")
        # 1 completed job, 1 running job, 1 received job
        j1 = self.job_store.create_job(actor_id=1, target="repo", description="job 1")
        self.job_store.update_status(j1["job_id"], "succeeded", step="done", effects_count=1)
        j2 = self.job_store.create_job(actor_id=1, target="repo", description="job 2")
        self.job_store.update_status(j2["job_id"], "running", step="step 1", effects_count=1)
        j3 = self.job_store.create_job(actor_id=1, target="repo", description="job 3")

        # Approvals store
        self.approval_store = ApprovalStore(self.state_dir / "approvals.sqlite3")
        # 1 pending approval, 1 executed
        self.app1 = self.approval_store.request(actor_id=1, target="cmd", arguments={"run": "ls"})
        self.app2 = self.approval_store.request(actor_id=1, target="cmd", arguments={"run": "date"})
        self.approval_store.decide(token=self.app2["token"], actor_id=1, target="cmd", arguments={"run": "date"}, approve=True)

        # Extra runtime data
        (self.state_dir / "llm_budget.json").write_text('{"total_cost": 1.25}', encoding="utf-8")

        # Secret file that should NOT be in backup
        (self.state_dir / ".env").write_text("SECRET_KEY=supersecret123\n", encoding="utf-8")
        (self.vault_dir / ".env").write_text("VAULT_SECRET=vaultsecret123\n", encoding="utf-8")
        (self.vault_dir / "credentials.json").write_text('{"token": "xyz"}', encoding="utf-8")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_backup_creates_archive_and_manifest_excluding_secrets(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
            retention_days=14,
        )
        result = mgr.create_backup(label="test-run")
        self.assertTrue(Path(result["archive_path"]).is_file())
        self.assertTrue(Path(result["manifest_path"]).is_file())

        manifest = json.loads(Path(result["manifest_path"]).read_text(encoding="utf-8"))
        self.assertEqual(manifest["schema_version"], "1.0")
        self.assertEqual(manifest["retention_days"], 14)
        self.assertIn("files", manifest)

        # Secrets must be strictly excluded from backup manifest and archive
        archived_files = [f["path"] for f in manifest["files"]]
        for f in archived_files:
            self.assertFalse(f.endswith(".env"))
            self.assertFalse("credential" in f.lower())

        self.assertIn(".env", manifest["excluded_secrets"])

    def test_restore_into_clean_environment_reconstructs_vault_and_state(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
        )
        backup_res = mgr.create_backup(label="full-snap")

        # Target clean directories
        restored_state = self.restore_dir / "restored_state"
        restored_vault = self.restore_dir / "restored_vault"

        res = mgr.restore(
            archive_path=backup_res["archive_path"],
            target_state_dir=restored_state,
            target_vault_dir=restored_vault,
        )
        self.assertTrue(res["success"])

        # Check vault files restored
        self.assertTrue((restored_vault / "preferences.yaml").is_file())
        self.assertTrue((restored_vault / "Projects" / "alpha.md").is_file())

        # Check vault versions and integrity
        vault = Vault(str(restored_vault))
        prof = vault.get_profile()
        self.assertEqual(prof["explicit_preferences"]["travel"]["preferred_airline"], "BA")
        self.assertGreater(len(prof["frontmatter_notes"]), 0)

        # Check that secrets were NOT restored
        self.assertFalse((restored_vault / ".env").exists())
        self.assertFalse((restored_vault / "credentials.json").exists())
        self.assertFalse((restored_state / ".env").exists())

    def test_restore_neutralizes_pending_jobs_and_approvals(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
        )
        backup_res = mgr.create_backup()

        restored_state = self.restore_dir / "state"
        restored_vault = self.restore_dir / "vault"

        mgr.restore(
            archive_path=backup_res["archive_path"],
            target_state_dir=restored_state,
            target_vault_dir=restored_vault,
        )

        # Check jobs state: running and received jobs must be marked 'outcome_unknown' or 'interrupted'
        restored_job_store = JobStore(restored_state / "jobs.sqlite3")
        with restored_job_store._connection() as conn:
            rows = conn.execute("SELECT job_id, status, warnings FROM jobs").fetchall()
            status_map = {r["job_id"]: (r["status"], r["warnings"]) for r in rows}

        # Succeeded job remains succeeded
        succeeded_jobs = [jid for jid, (st, _) in status_map.items() if st == "succeeded"]
        self.assertEqual(len(succeeded_jobs), 1)

        # Non-terminal jobs must be transitioned to outcome_unknown with warning
        for jid, (st, warn) in status_map.items():
            if st != "succeeded":
                self.assertEqual(st, "outcome_unknown")
                self.assertIn("restored from backup", warn.lower())

        # Check approvals state: pending approvals must be cancelled
        with sqlite3.connect(restored_state / "approvals.sqlite3") as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute("SELECT digest, status FROM approvals").fetchall()
            app_map = {r["digest"]: r["status"] for r in rows}

        self.assertIn(self.app2["digest"], app_map)
        self.assertEqual(app_map[self.app2["digest"]], "executed")
        self.assertIn(self.app1["digest"], app_map)
        self.assertEqual(app_map[self.app1["digest"]], "cancelled")

    def test_refuse_restore_over_live_vault_without_explicit_force(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
        )
        backup_res = mgr.create_backup()

        # Restoring over existing live vault directory without force should fail
        with self.assertRaises(BackupError) as ctx:
            mgr.restore(
                archive_path=backup_res["archive_path"],
                target_state_dir=self.restore_dir / "new_state",
                target_vault_dir=self.vault_dir,  # Live non-empty vault
                allow_existing=False,
            )
        self.assertEqual(ctx.exception.code, "DESTINATION_NOT_EMPTY")

    def test_retention_prune_expired_backups(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
            retention_days=7,
        )
        b1 = mgr.create_backup(label="old_backup")
        # Artificially age the archive & manifest files
        old_time = 1000000.0
        os.utime(b1["archive_path"], (old_time, old_time))
        os.utime(b1["manifest_path"], (old_time, old_time))

        b2 = mgr.create_backup(label="recent_backup")

        pruned = mgr.prune_backups()
        self.assertIn(b1["backup_id"], [p["backup_id"] for p in pruned])
        self.assertFalse(Path(b1["archive_path"]).exists())
        self.assertFalse(Path(b1["manifest_path"]).exists())
        self.assertTrue(Path(b2["archive_path"]).exists())

    def test_restore_fails_closed_and_preserves_targets_on_job_reconciliation_error(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
        )
        backup_res = mgr.create_backup()

        target_state = self.restore_dir / "state_canary_job"
        target_vault = self.restore_dir / "vault_canary_job"
        target_state.mkdir(parents=True, exist_ok=True)
        target_vault.mkdir(parents=True, exist_ok=True)

        canary_state_file = target_state / "canary.txt"
        canary_state_file.write_text("state_canary_data", encoding="utf-8")
        canary_vault_file = target_vault / "canary.txt"
        canary_vault_file.write_text("vault_canary_data", encoding="utf-8")

        # Mock sqlite3.connect when connecting to jobs.sqlite3 to raise an error
        orig_connect = sqlite3.connect

        def fake_connect(database, *args, **kwargs):
            if "jobs.sqlite3" in str(database):
                raise sqlite3.OperationalError("simulated database disk image is malformed")
            return orig_connect(database, *args, **kwargs)

        with patch("sqlite3.connect", side_effect=fake_connect):
            with self.assertRaises(BackupError) as ctx:
                mgr.restore(
                    archive_path=backup_res["archive_path"],
                    target_state_dir=target_state,
                    target_vault_dir=target_vault,
                    allow_existing=True,
                )
            self.assertEqual(ctx.exception.code, "RECONCILIATION_ERROR")

        # Target directories must be preserved unchanged
        self.assertEqual(canary_state_file.read_text(encoding="utf-8"), "state_canary_data")
        self.assertEqual(canary_vault_file.read_text(encoding="utf-8"), "vault_canary_data")
        self.assertFalse((target_state / "jobs.sqlite3").exists())
        self.assertFalse((target_vault / "preferences.yaml").exists())

    def test_restore_fails_closed_and_preserves_targets_on_approval_reconciliation_error(self):
        mgr = BackupManager(
            state_dir=self.state_dir,
            vault_dir=self.vault_dir,
            backup_root=self.backup_dir,
        )
        backup_res = mgr.create_backup()

        target_state = self.restore_dir / "state_canary_app"
        target_vault = self.restore_dir / "vault_canary_app"
        target_state.mkdir(parents=True, exist_ok=True)
        target_vault.mkdir(parents=True, exist_ok=True)

        canary_state_file = target_state / "canary.txt"
        canary_state_file.write_text("state_canary_data", encoding="utf-8")
        canary_vault_file = target_vault / "canary.txt"
        canary_vault_file.write_text("vault_canary_data", encoding="utf-8")

        # Mock sqlite3.connect when connecting to approvals.sqlite3 to raise an error
        orig_connect = sqlite3.connect

        def fake_connect(database, *args, **kwargs):
            if "approvals.sqlite3" in str(database):
                raise sqlite3.OperationalError("simulated database lock / corruption")
            return orig_connect(database, *args, **kwargs)

        with patch("sqlite3.connect", side_effect=fake_connect):
            with self.assertRaises(BackupError) as ctx:
                mgr.restore(
                    archive_path=backup_res["archive_path"],
                    target_state_dir=target_state,
                    target_vault_dir=target_vault,
                    allow_existing=True,
                )
            self.assertEqual(ctx.exception.code, "RECONCILIATION_ERROR")

        # Target directories must be preserved unchanged
        self.assertEqual(canary_state_file.read_text(encoding="utf-8"), "state_canary_data")
        self.assertEqual(canary_vault_file.read_text(encoding="utf-8"), "vault_canary_data")
        self.assertFalse((target_state / "approvals.sqlite3").exists())
        self.assertFalse((target_vault / "preferences.yaml").exists())


if __name__ == "__main__":
    unittest.main()
