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
            )

            self.assertTrue(res["ok"])
            self.assertEqual(res["project_name"], "test_repo")
            self.assertIn("Task completato", res["message"])
            self.assertFalse(res["is_long_output"])

            # Verify subprocess call arguments
            mock_run.assert_called()
            called_args = mock_run.call_args_list[0][0][0]
            self.assertEqual(called_args[0], "/usr/local/bin/pi")
            self.assertIn("-p", called_args)
            self.assertIn("-a", called_args)
            self.assertIn("-c", called_args)
            self.assertIn("--model", called_args)
            self.assertIn("gemini-3.8-flash-high", called_args)
            self.assertEqual(called_args[-1], "Add test suite")

    def test_run_pi_task_large_output_saved_to_file(self):
        target_dir = self.base_path / "programmazione" / "large_repo"
        target_dir.mkdir(parents=True)

        large_output = "X" * 4000

        with patch("pathlib.Path.home", return_value=self.base_path), \
             patch("shutil.which", return_value="/usr/local/bin/pi"), \
             patch("jarvis_hermes.pi_coding.ensure_proxy_servers_running", return_value={}), \
             patch("subprocess.run") as mock_run:

            mock_res = MagicMock()
            mock_res.returncode = 0
            mock_res.stdout = large_output
            mock_res.stderr = ""
            mock_run.return_value = mock_res

            res = run_pi_task(
                prompt="Generate big codebase",
                project_query="large_repo",
                max_inline_chars=3500,
            )

            self.assertTrue(res["ok"])
            self.assertTrue(res["is_long_output"])
            self.assertIsNotNone(res["output_file"])
            self.assertIsNone(res["output"])
            self.assertTrue(Path(res["output_file"]).is_file())


if __name__ == "__main__":
    unittest.main()
