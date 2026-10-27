import os
import subprocess
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

        # Cancel requested without process handle
        cancel_res = self.store.cancel_job(job_id)
        self.assertEqual(cancel_res["status"], "cancelled")
        self.assertEqual(cancel_res["last_completed_step"], "step 1: created temp directory")
        self.assertEqual(cancel_res["unreverted_effects_count"], 1)
        self.assertIsNone(cancel_res["process_stopped"])
        self.assertIn("null", cancel_res["note"])

        # Cannot cancel already cancelled or finished job
        with self.assertRaises(JobError) as ctx:
            self.store.cancel_job(job_id)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_job_cancel_terminates_real_process_and_verifies(self):
        import subprocess
        # Start a real long-running sleep process in its own process group
        proc = subprocess.Popen(['sleep', '60'], start_new_session=True)
        job = self.store.create_job(actor_id=1, target="long_task", description="sleep command")
        job_id = job["job_id"]
        pgid = os.getpgid(proc.pid) if hasattr(os, 'getpgid') else proc.pid
        self.store.register_process(job_id=job_id, pid=proc.pid, pgid=pgid)

        # Cancel the job
        cancel_res = self.store.cancel_job(job_id, timeout_seconds=2.0)
        self.assertEqual(cancel_res["status"], "cancelled")
        self.assertTrue(cancel_res["process_stopped"])
        self.assertIn("verified terminated", cancel_res["note"])

        # Process should be dead
        proc.poll()
        self.assertFalse(self.store._is_process_alive(proc.pid))

        # Check subsequent check_not_cancelled raises
        with self.assertRaises(JobError) as ctx:
            self.store.check_not_cancelled(job_id)
        self.assertEqual(ctx.exception.code, "JOB_CANCELLED")

    def test_job_cancel_escalates_and_kills_descendant_ignoring_sigterm(self):
        """A descendant ignoring SIGTERM must be killed via SIGKILL escalation and not survive."""
        import subprocess
        from jarvis_hermes.jobs import get_active_descendants
        
        # Script spawns child that ignores SIGTERM and stays alive, while leader exits or sleeps
        script = (
            "import os, time, signal, sys\n"
            "pid = os.fork()\n"
            "if pid == 0:\n"
            "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
            "    time.sleep(30)\n"
            "    os._exit(0)\n"
            "time.sleep(30)\n"
        )
        proc = subprocess.Popen(['python3', '-c', script], start_new_session=True)
        try:
            pid = proc.pid
            pgid = os.getpgid(pid) if hasattr(os, 'getpgid') else pid
            job = self.store.create_job(actor_id=1, target="sigterm_ignoring_tree", description="tree with sigterm ignorer")
            job_id = job["job_id"]
            self.store.register_process(job_id=job_id, pid=pid, pgid=pgid)

            # Wait briefly for child to fork
            time.sleep(0.3)
            active_before = get_active_descendants(pid, pgid)
            self.assertGreaterEqual(len(active_before), 2, "Both leader and child should be active")

            # Cancel job with escalation
            cancel_res = self.store.cancel_job(job_id, timeout_seconds=2.0)
            self.assertEqual(cancel_res["status"], "cancelled")
            self.assertTrue(cancel_res["process_stopped"])
            self.assertIn("verified terminated", cancel_res["note"])

            # Verify no descendants remain alive
            active_after = get_active_descendants(pid, pgid)
            self.assertEqual(active_after, [], "All descendants including SIGTERM ignorer must be terminated")
        finally:
            # Ensure cleanup
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

    def test_job_cancel_reports_unverified_if_descendant_remains(self):
        """If a descendant remains active, cancel_job reports process_stopped=False instead of false success."""
        from unittest.mock import patch
        proc = subprocess.Popen(['sleep', '10'], start_new_session=True)
        try:
            pid = proc.pid
            pgid = os.getpgid(pid) if hasattr(os, 'getpgid') else pid
            job = self.store.create_job(actor_id=1, target="stubborn_job", description="stubborn process")
            job_id = job["job_id"]
            self.store.register_process(job_id=job_id, pid=pid, pgid=pgid)

            # Mock terminate_process_tree returning False (unverified / active remaining)
            with patch('jarvis_hermes.jobs.terminate_process_tree', return_value=False):
                cancel_res = self.store.cancel_job(job_id, timeout_seconds=1.0)

            self.assertEqual(cancel_res["status"], "cancelled")
            self.assertFalse(cancel_res["process_stopped"])
            self.assertIn("could not be verified within timeout", cancel_res["note"])
        finally:
            try:
                proc.kill()
                proc.wait(timeout=1.0)
            except Exception:
                pass

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

    def test_concurrent_cross_process_admission_control(self):
        """Simulate concurrent tasks across distinct processes / connections on same JobStore DB."""
        import multiprocessing

        def worker_acquire(db_file, proj_name, out_queue):
            try:
                s = JobStore(db_file)
                j = s.acquire_pi_job(actor_id=99, project_name=proj_name, prompt="p")
                out_queue.put(("ok", j["job_id"]))
            except JobError as exc:
                out_queue.put(("error", exc.code, exc.retryable))

        q = multiprocessing.Queue()
        # Five concurrent attempts across 5 distinct processes
        # 3 for project A, 2 for project B
        procs = [
            multiprocessing.Process(target=worker_acquire, args=(self.db_path, "projA", q)),
            multiprocessing.Process(target=worker_acquire, args=(self.db_path, "projA", q)),
            multiprocessing.Process(target=worker_acquire, args=(self.db_path, "projA", q)),
            multiprocessing.Process(target=worker_acquire, args=(self.db_path, "projB", q)),
            multiprocessing.Process(target=worker_acquire, args=(self.db_path, "projB", q)),
        ]
        for p in procs:
            p.start()
        for p in procs:
            p.join(timeout=10.0)

        results = []
        while not q.empty():
            results.append(q.get())

        self.assertEqual(len(results), 5)
        oks = [r for r in results if r[0] == "ok"]
        errors = [r for r in results if r[0] == "error"]

        # Exactly 2 tasks succeeded (1 for projA, 1 for projB)
        self.assertEqual(len(oks), 2)
        # Exactly 3 tasks failed with retryable PROJECT_BUSY or CAPACITY_EXCEEDED
        self.assertEqual(len(errors), 3)
        for err in errors:
            self.assertIn(err[1], {"PROJECT_BUSY", "CAPACITY_EXCEEDED"})
            self.assertTrue(err[2])

