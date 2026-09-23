"""Public CLI behavior, including truthful capability and private-path reporting."""
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path


class DoctorTests(unittest.TestCase):
    def invoke(self, *args, vault=None):
        env = dict(os.environ)
        env.pop("JARVIS_VAULT_PATH", None)
        if vault is not None:
            env["JARVIS_VAULT_PATH"] = vault
        return subprocess.run(
            [sys.executable, "-m", "jarvis_hermes", "doctor", *args],
            env=env, capture_output=True, text=True, check=False,
        )

    def test_json_has_real_disk_and_no_claim_of_remote_connectivity(self):
        result = self.invoke("--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertGreater(report["disk"]["total_bytes"], 0)
        self.assertGreaterEqual(report["disk"]["free_bytes"], 0)
        self.assertLessEqual(report["disk"]["free_bytes"], report["disk"]["total_bytes"])
        self.assertEqual(report["scope"], "current_host_only")
        self.assertEqual(report["integrations"]["telegram"], "not_verified")
        self.assertEqual(report["integrations"]["mcp"], "not_implemented")
        self.assertEqual(report["vault"]["status"], "not_configured")

    def test_vault_presence_without_leaking_path_or_reading_note(self):
        with tempfile.TemporaryDirectory(prefix="private-vault-") as directory:
            (Path(directory) / "private-note.md").write_text("SECRET_CANARY_713", encoding="utf-8")
            result = self.invoke("--json", vault=directory)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(result.stdout)["vault"], {"status": "available", "contents_read": False})
            self.assertNotIn(directory, result.stdout)
            self.assertNotIn("SECRET_CANARY_713", result.stdout)

    def test_unavailable_vault_is_not_reported_ready(self):
        result = self.invoke("--json", vault="relative-unconfigured-vault")
        self.assertEqual(json.loads(result.stdout)["vault"]["status"], "unavailable")

    def test_missing_disk_is_actionable_without_echoing_private_path(self):
        with tempfile.TemporaryDirectory() as directory:
            missing = str(Path(directory) / "missing-private-path")
            result = self.invoke("--json", "--disk-path", missing)
            self.assertEqual(result.returncode, 2)
            self.assertIn("Cannot inspect", result.stderr)
            self.assertNotIn(missing, result.stderr)
            self.assertNotIn("Traceback", result.stderr)


if __name__ == "__main__":
    unittest.main()
