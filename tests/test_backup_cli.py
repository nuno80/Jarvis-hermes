"""Tests for backup & restore CLI integration."""
import json
import os
import subprocess
import sys
import unittest


class BackupCliTests(unittest.TestCase):
    def test_backup_demo_runs_successfully(self):
        env = dict(os.environ)
        env["JARVIS_TEST_MODE"] = "1"
        res = subprocess.run(
            [sys.executable, "-m", "jarvis_hermes", "backup-demo", "--json"],
            env=env,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(res.returncode, 0, res.stderr)
        data = json.loads(res.stdout)
        self.assertEqual(data["scope"], "isolated_backup_restore_demo")
        self.assertTrue(data["restore"]["success"])
        self.assertFalse(data["vault_verification"]["secret_env_present"])
        self.assertEqual(data["restore"]["reconciled_jobs_count"], 1)
        self.assertEqual(data["restore"]["neutralized_approvals_count"], 1)
        self.assertEqual(data["vault_verification"]["explicit_preferences"]["travel"]["airline"], "StarAlliance")


if __name__ == "__main__":
    unittest.main()
