"""Tests for pi_coding and coding session MCP integration."""
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch, MagicMock

from jarvis_hermes.pi_coding import (
    PiCodingError,
    ensure_proxy_servers_running,
    resolve_project_path,
    run_pi_task,
)


class PiCodingTests(unittest.TestCase):
    def setUp(self):
        self.test_dir = tempfile.TemporaryDirectory()
        self.base_path = Path(self.test_dir.name)

    def tearDown(self):
        self.test_dir.cleanup()

    def test_resolve_project_alias_and_fuzzy(self):
        # Create mock programmazione directory structure
        prog_dir = self.base_path / "programmazione"
        prog_dir.mkdir(parents=True)
        (prog_dir / "Jarvis-hermes").mkdir()
        (prog_dir / "fantavega").mkdir()
        (prog_dir / "12_starter_kit_t3_sqlite").mkdir()

        # Mock Path.home()
        with patch("pathlib.Path.home", return_value=self.base_path):
            # Test direct exact match
            path, name = resolve_project_path("fantavega")
            self.assertEqual(name, "fantavega")
            self.assertEqual(path, prog_dir / "fantavega")

            # Test inverted hyphen match (hermes-jarvis -> Jarvis-hermes)
            path, name = resolve_project_path("hermes-jarvis")
            self.assertEqual(name, "Jarvis-hermes")
            self.assertEqual(path, prog_dir / "Jarvis-hermes")

            # Test typo / close match
            path, name = resolve_project_path("fantaveg")
            self.assertEqual(name, "fantavega")

            # Test non-existent project raises PiCodingError
            with self.assertRaises(PiCodingError):
                resolve_project_path("progetto_assolutamente_inesistente_xyz_123")

    def test_run_pi_task_command_assembly_and_execution(self):
        target_dir = self.base_path / "programmazione" / "test_repo"
        target_dir.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.base_path), \
             patch("shutil.which", return_value="/usr/local/bin/pi"), \
             patch("jarvis_hermes.pi_coding.ensure_proxy_servers_running", return_value={"cli-proxy-api": "already_running"}), \
             patch("subprocess.run") as mock_run:

            # Mock successful execution
            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = "Task executed successfully: added new feature"
            mock_res.stderr = ""
            mock_run.return_value = mock_res

            res = run_pi_task(
                prompt="Add test suite",
                project_query="test_repo",
                new_session=False,
                model="gemini-3.8-flash-high",
                notify_telegram=False,
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["project_name"], "test_repo")
            self.assertIn("Task completato", res["message"])
            self.assertFalse(res["is_long_output"])

            # Verify subprocess call arguments
            mock_run.assert_called()
            # git is called first for the pre-run snapshot; find the pi invocation itself
            pi_calls = [c for c in mock_run.call_args_list if c[0][0][0] == "/usr/local/bin/pi"]
            self.assertEqual(len(pi_calls), 1)
            called_args = pi_calls[0][0][0]
            self.assertEqual(called_args[0], "/usr/local/bin/pi")
            self.assertIn("-p", called_args)
            self.assertIn("-a", called_args)
            self.assertIn("-c", called_args)
            self.assertIn("--model", called_args)
            self.assertIn("gemini-3.8-flash-high", called_args)
            self.assertEqual(called_args[-1], "Add test suite")

    def test_run_pi_task_async_mode(self):
        target_dir = self.base_path / "programmazione" / "async_repo"
        target_dir.mkdir(parents=True)

        with patch("pathlib.Path.home", return_value=self.base_path), \
             patch("shutil.which", return_value="/usr/local/bin/pi"), \
             patch("jarvis_hermes.pi_coding.ensure_proxy_servers_running", return_value={}), \
             patch("jarvis_hermes.pi_coding.send_telegram_notification") as mock_tg:

            res = run_pi_task(
                prompt="Run async background task",
                project_query="async_repo",
                async_mode=True,
                notify_telegram=True,
            )

            self.assertTrue(res["ok"])
            self.assertTrue(res.get("async"))
            self.assertEqual(res["status"], "running_in_background")
            mock_tg.assert_called()


if __name__ == "__main__":
    unittest.main()


import shutil
import sqlite3
import stat
import subprocess
import time

from jarvis_hermes.jobs import JobStore
from jarvis_hermes.pi_coding import snapshot_repo


def _git(cwd, *args):
    return subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args],
        cwd=str(cwd), capture_output=True, text=True, check=True).stdout


class PiConfinementTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-pi-")).resolve()
        self.prog = self.tmp / "programmazione"
        (self.prog / "proj").mkdir(parents=True)
        (self.tmp / "outside").mkdir()
        (self.prog / "evil").symlink_to(self.tmp / "outside")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_paths_outside_programmazione_are_rejected(self):
        with patch("pathlib.Path.home", return_value=self.tmp):
            for query in (str(self.tmp), str(self.tmp / "outside"), str(self.prog), "~", "/etc", "evil"):
                with self.assertRaises(PiCodingError, msg=query) as ctx:
                    resolve_project_path(query)
                self.assertIn(ctx.exception.code, {"FORBIDDEN_PATH", "PROJECT_NOT_FOUND"}, query)

    def test_project_inside_programmazione_is_accepted(self):
        with patch("pathlib.Path.home", return_value=self.tmp):
            path, name = resolve_project_path("proj")
            self.assertEqual(name, "proj")
            path, name = resolve_project_path(str(self.prog / "proj"))
            self.assertEqual(name, "proj")


class PiSnapshotTests(unittest.TestCase):
    def setUp(self):
        self.repo = Path(tempfile.mkdtemp(prefix="jarvis-snap-")).resolve()
        _git(self.repo, "init", "-q", "-b", "main")
        (self.repo / "tracked.txt").write_text("v1\n")
        _git(self.repo, "add", "-A")
        _git(self.repo, "commit", "-q", "-m", "init")

    def tearDown(self):
        shutil.rmtree(self.repo, ignore_errors=True)

    def test_snapshot_captures_dirty_and_untracked_without_touching_the_repo(self):
        (self.repo / "tracked.txt").write_text("v2-dirty\n")
        (self.repo / "new.txt").write_text("untracked\n")
        head_before = _git(self.repo, "rev-parse", "HEAD")
        status_before = _git(self.repo, "status", "--porcelain")

        snap = snapshot_repo(self.repo)

        self.assertIsNotNone(snap["ref"])
        self.assertEqual(_git(self.repo, "show", f"{snap['ref']}:tracked.txt"), "v2-dirty\n")
        self.assertEqual(_git(self.repo, "show", f"{snap['ref']}:new.txt"), "untracked\n")
        self.assertEqual(_git(self.repo, "rev-parse", "HEAD"), head_before)
        self.assertEqual(_git(self.repo, "status", "--porcelain"), status_before)
        self.assertEqual(_git(self.repo, "stash", "list"), "")
        self.assertEqual((self.repo / "tracked.txt").read_text(), "v2-dirty\n")

    def test_non_git_directory_is_reported_not_fatal(self):
        plain = Path(tempfile.mkdtemp(prefix="jarvis-plain-")).resolve()
        try:
            snap = snapshot_repo(plain)
            self.assertIsNone(snap["ref"])
            self.assertEqual(snap["reason"], "not_a_git_repository")
        finally:
            shutil.rmtree(plain, ignore_errors=True)


class PiJobTrackingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="jarvis-job-")).resolve()
        (self.tmp / "programmazione" / "proj").mkdir(parents=True)
        self.store = JobStore(self.tmp / "jobs.sqlite3")
        self.fake_pi = self.tmp / "pi"

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _fake_pi(self, body):
        self.fake_pi.write_text("#!/bin/sh\n" + body + "\n")
        self.fake_pi.chmod(self.fake_pi.stat().st_mode | stat.S_IEXEC)

    def _run(self, **kwargs):
        with patch("pathlib.Path.home", return_value=self.tmp), \
             patch("shutil.which", return_value=str(self.fake_pi)), \
             patch("jarvis_hermes.pi_coding.ensure_proxy_servers_running", return_value={}):
            return run_pi_task(prompt="do it", project_query="proj", notify_telegram=False,
                               job_store=self.store, actor_id=42, **kwargs)

    def _wait(self, job_id, terminal=True, timeout=10):
        deadline = time.time() + timeout
        while time.time() < deadline:
            job = self.store.get_job(job_id)
            if (terminal and job["status"] in {"succeeded", "failed", "cancelled"}) or \
                    (not terminal and job["status"] == "running"):
                return job
            time.sleep(0.1)
        self.fail(f"job {job_id} did not reach expected state")

    def test_sync_task_is_tracked_as_a_job(self):
        self._fake_pi("echo done")
        res = self._run()
        self.assertTrue(res["ok"])
        self.assertEqual(self.store.get_job(res["job_id"])["status"], "succeeded")

    def test_failed_task_marks_job_failed(self):
        self._fake_pi("exit 3")
        res = self._run()
        self.assertFalse(res["ok"])
        self.assertEqual(self.store.get_job(res["job_id"])["status"], "failed")

    def test_async_task_can_be_cancelled_and_process_is_killed(self):
