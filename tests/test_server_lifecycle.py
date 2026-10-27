"""Tests separating FastMCP server construction/introspection from startup reconciliation (Issue #53)."""

import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path

from jarvis_hermes.jobs import JobStore
from jarvis_hermes.reminder import ReminderStore
from jarvis_hermes.server import build_server, reconcile_startup_state


class ServerLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.temp_dir.name)
        self.orig_env = os.environ.get("XDG_STATE_HOME")
        os.environ["XDG_STATE_HOME"] = str(self.state_dir)

    def tearDown(self):
        if self.orig_env is not None:
            os.environ["XDG_STATE_HOME"] = self.orig_env
        else:
            os.environ.pop("XDG_STATE_HOME", None)
        self.temp_dir.cleanup()

    def test_build_server_does_not_modify_running_jobs_or_reminders(self):
        """Consecutive build_server calls must not change running jobs to outcome_unknown or reconcile reminders."""
        db_jobs = self.state_dir / "jarvis-hermes" / "jobs.sqlite3"
        job_store = JobStore(db_jobs)
        job = job_store.create_job(actor_id=1, target="t1", description="desc")
        jid = job["job_id"]
        job_store.update_status(jid, "running", step="step1")

        db_rem = self.state_dir / "jarvis-hermes" / "reminders.sqlite3"
        rem_store = ReminderStore(db_rem)
        past_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        r = rem_store.create(actor_id=1, text="past reminder", due_at=past_dt)
        rid = r["reminder_id"]

        # Call build_server first time
        s1 = build_server()
        self.assertIsNotNone(s1)

        # Job must still be running and cancellable
        j_after_s1 = job_store.get_job(jid)
        self.assertEqual(j_after_s1["status"], "running")
        rem_after_s1 = rem_store.get(rid)
        self.assertEqual(rem_after_s1["status"], "scheduled")

        # Call build_server second time (introspection/inspection simulation)
        s2 = build_server()
        self.assertIsNotNone(s2)

        j_after_s2 = job_store.get_job(jid)
        self.assertEqual(j_after_s2["status"], "running")
        rem_after_s2 = rem_store.get(rid)
        self.assertEqual(rem_after_s2["status"], "scheduled")

        # Must still be cancellable
        cancelled = job_store.cancel_job(jid)
        self.assertEqual(cancelled["status"], "cancelled")

    def test_inventory_script_does_not_modify_state_or_database(self):
        """Running scripts/inventory_tools.py must leave databases completely unchanged."""
        db_jobs = self.state_dir / "jarvis-hermes" / "jobs.sqlite3"
        job_store = JobStore(db_jobs)
        job = job_store.create_job(actor_id=1, target="t_inv", description="inv job")
        jid = job["job_id"]
        job_store.update_status(jid, "running", step="processing")

        db_rem = self.state_dir / "jarvis-hermes" / "reminders.sqlite3"
        rem_store = ReminderStore(db_rem)
        past_dt = datetime.now(timezone.utc) - timedelta(minutes=30)
        r = rem_store.create(actor_id=1, text="reminder inv", due_at=past_dt)
        rid = r["reminder_id"]

        # Snapshot hash/stat or state
        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.state_dir)
        repo_root = Path(__file__).resolve().parents[1]
        res = subprocess.run(
            [sys.executable, str(repo_root / "scripts" / "inventory_tools.py")],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn("# Inventario Tool MCP", res.stdout)

        # Verify job and reminder states were not reconciled
        j_current = job_store.get_job(jid)
        self.assertEqual(j_current["status"], "running")
        r_current = rem_store.get(rid)
        self.assertEqual(r_current["status"], "scheduled")

    def test_doctor_command_is_read_only_and_does_not_modify_state(self):
        """Executing 'jarvis doctor --json' must not reconcile jobs or reminders."""
        db_jobs = self.state_dir / "jarvis-hermes" / "jobs.sqlite3"
        job_store = JobStore(db_jobs)
        job = job_store.create_job(actor_id=1, target="t_doc", description="doc job")
        jid = job["job_id"]
        job_store.update_status(jid, "running", step="processing")

        db_rem = self.state_dir / "jarvis-hermes" / "reminders.sqlite3"
        rem_store = ReminderStore(db_rem)
        past_dt = datetime.now(timezone.utc) - timedelta(hours=1)
        r = rem_store.create(actor_id=1, text="rem doc", due_at=past_dt)
        rid = r["reminder_id"]

        env = dict(os.environ)
        env["XDG_STATE_HOME"] = str(self.state_dir)
        res = subprocess.run(
            [sys.executable, "-m", "jarvis_hermes", "doctor", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=True,
        )
        self.assertIn('"pending_count": 1', res.stdout)
        self.assertIn('"outcome_unknown_count": 0', res.stdout)

        # Ensure database records remain running and scheduled
        j_doc = job_store.get_job(jid)
        self.assertEqual(j_doc["status"], "running")
        r_doc = rem_store.get(rid)
        self.assertEqual(r_doc["status"], "scheduled")

    def test_real_startup_reconciliation_executes_once(self):
        """reconcile_startup_state marks running jobs as outcome_unknown and expired reminders as missed."""
        db_jobs = self.state_dir / "jarvis-hermes" / "jobs.sqlite3"
        job_store = JobStore(db_jobs)
        job = job_store.create_job(actor_id=1, target="t_real", description="real job")
        jid = job["job_id"]
        job_store.update_status(jid, "running", step="executing")

        db_rem = self.state_dir / "jarvis-hermes" / "reminders.sqlite3"
        rem_store = ReminderStore(db_rem)
        past_dt = datetime.now(timezone.utc) - timedelta(hours=2)
        r = rem_store.create(actor_id=1, text="past reminder", due_at=past_dt)
        rid = r["reminder_id"]

        result = reconcile_startup_state()
        self.assertIn(jid, result["reconciled_jobs"])
        self.assertEqual(len(result["reconciled_reminders"]), 1)
        self.assertEqual(result["reconciled_reminders"][0]["reminder_id"], rid)

        j_after = job_store.get_job(jid)
        self.assertEqual(j_after["status"], "outcome_unknown")
        r_after = rem_store.get(rid)
        self.assertEqual(r_after["status"], "missed_during_downtime")

        # Second invocation reconciles nothing new
        result2 = reconcile_startup_state()
        self.assertEqual(result2["reconciled_jobs"], [])
        self.assertEqual(result2["reconciled_reminders"], [])

