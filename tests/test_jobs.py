import tempfile
import unittest
from pathlib import Path
import time

from jarvis_hermes.jobs import JobStore, JobError


class JobStoreTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self.tmp.name) / "jobs.sqlite3"
        self.store = JobStore(self.db_path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_and_query_job(self):
        job = self.store.create_job(
            actor_id=12345,
            target="run_test_suite",
            description="Run full test suite",
            metadata={"device_id": "test-node"}
        )
        self.assertTrue(job["job_id"].startswith("job-"))
        self.assertEqual(job["status"], "received")
        self.assertEqual(job["target"], "run_test_suite")
        self.assertEqual(job["actor_id"], 12345)

        queried = self.store.get_job(job["job_id"])
        self.assertEqual(queried["job_id"], job["job_id"])
        self.assertEqual(queried["status"], "received")

    def test_job_lifecycle_transitions(self):
        job = self.store.create_job(actor_id=1, target="t1", description="desc")
        job_id = job["job_id"]

        self.store.update_status(job_id, "running", step="step 1: initialization")
        j1 = self.store.get_job(job_id)
        self.assertEqual(j1["status"], "running")
        self.assertEqual(j1["last_step"], "step 1: initialization")

        self.store.update_status(job_id, "succeeded", step="step 2: done", result={"code": 0})
        j2 = self.store.get_job(job_id)
        self.assertEqual(j2["status"], "succeeded")
        self.assertEqual(j2["result"], {"code": 0})

    def test_job_cancel_distinguishes_completed_steps_and_unreverted_effects(self):
        job = self.store.create_job(actor_id=1, target="batch_op", description="multi-step job")
        job_id = job["job_id"]

        # Job ran step 1 and recorded an effect
        self.store.update_status(job_id, "running", step="step 1: created temp directory", effects_count=1)

        # Cancel requested
        cancel_res = self.store.cancel_job(job_id)
        self.assertEqual(cancel_res["status"], "cancelled")
        self.assertEqual(cancel_res["last_completed_step"], "step 1: created temp directory")
        self.assertEqual(cancel_res["unreverted_effects_count"], 1)
        self.assertTrue(cancel_res["process_stopped"])

        # Cannot cancel already cancelled or finished job
        with self.assertRaises(JobError) as ctx:
            self.store.cancel_job(job_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_reconcile_on_restart_marks_interrupted_as_outcome_unknown(self):
        # Simulate a job that was 'running' when server abruptly restarted
        job = self.store.create_job(actor_id=1, target="remote_mutation", description="mutation")
        job_id = job["job_id"]
        self.store.update_status(job_id, "running", step="step 2: sent payload", effects_count=1)

        # Node restarts: JobStore initialized again
        new_store = JobStore(self.db_path)
        reconciled = new_store.reconcile_on_startup()
        self.assertIn(job_id, reconciled)

        status_after = new_store.get_job(job_id)
        self.assertEqual(status_after["status"], "outcome_unknown")
        self.assertEqual(status_after["last_step"], "step 2: sent payload")
        self.assertIn("Node restarted during execution", status_after["warnings"])
