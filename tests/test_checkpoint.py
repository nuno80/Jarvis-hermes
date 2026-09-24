import tempfile
import unittest
from pathlib import Path

from jarvis_hermes.checkpoint import CheckpointManager, CheckpointError


class CheckpointTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name) / "state"
        self.project_dir = Path(self.tmp.name) / "project"
        self.project_dir.mkdir(parents=True)
        self.manager = CheckpointManager(storage_dir=self.state_dir)

    def tearDown(self):
        self.tmp.cleanup()

    def test_create_checkpoint_and_restore(self):
        file_path = self.project_dir / "code.py"
        file_path.write_text("initial code\n", encoding="utf-8")

        # 1. Create checkpoint before job edits
        cp = self.manager.create_checkpoint(
            job_id="job-1",
            file_path=file_path,
            project_id="test-proj",
            relative_path="code.py"
        )
        self.assertEqual(cp["job_id"], "job-1")
        initial_hash = cp["initial_hash"]
        self.assertTrue(initial_hash)

        # 2. Modify file as part of job
        file_path.write_text("modified code by job\n", encoding="utf-8")

        # 3. Restore checkpoint
        res = self.manager.restore_checkpoint(
            checkpoint_id=cp["checkpoint_id"],
            expected_job_id="job-1"
        )
        self.assertTrue(res["restored"])
        self.assertEqual(file_path.read_text(encoding="utf-8"), "initial code\n")
        self.assertIn("-modified code by job", res["diff"])
        self.assertIn("+initial code", res["diff"])

    def test_conflict_detected_when_file_modified_externally_after_checkpoint(self):
        file_path = self.project_dir / "code.py"
        file_path.write_text("initial code\n", encoding="utf-8")

        cp = self.manager.create_checkpoint(
            job_id="job-2",
            file_path=file_path,
            project_id="test-proj",
            relative_path="code.py"
        )

        # Concurrent external edit by user / another process
        file_path.write_text("manual edit by human\n", encoding="utf-8")

        # Attempt to write with safe_write checking initial hash/version
        with self.assertRaises(CheckpointError) as ctx:
            self.manager.safe_write_file(
                checkpoint_id=cp["checkpoint_id"],
                file_path=file_path,
                expected_initial_hash=cp["initial_hash"],
                new_content="job attempted write\n"
            )
        self.assertEqual(ctx.exception.code, "CONFLICT")
        # Ensure file content wasn't overwritten
        self.assertEqual(file_path.read_text(encoding="utf-8"), "manual edit by human\n")

    def test_restore_fails_if_modified_concurrently_without_force(self):
        file_path = self.project_dir / "code.py"
        file_path.write_text("initial code\n", encoding="utf-8")

        cp = self.manager.create_checkpoint(
            job_id="job-3",
            file_path=file_path,
            project_id="test-proj",
            relative_path="code.py"
        )

        # Job wrote something
        file_path.write_text("job code\n", encoding="utf-8")
        # Then manual edit happened
        file_path.write_text("manual edit on top\n", encoding="utf-8")

        # Restore detects conflict if current hash doesn't match last job state unless explicit
        with self.assertRaises(CheckpointError) as ctx:
            self.manager.restore_checkpoint(
                checkpoint_id=cp["checkpoint_id"],
                expected_job_id="job-3",
                expected_current_hash="some-stale-hash"
            )
        self.assertEqual(ctx.exception.code, "CONFLICT")
