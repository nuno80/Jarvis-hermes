import os
import unittest
from pathlib import Path
import tempfile

from jarvis_hermes.projects import ProjectRegistry, ProjectError, redact_secrets


class ProjectRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        
        # Setup directories with spaces in path
        self.wsl_project = self.root / "wsl projects" / "fantavega app"
        self.wsl_project.mkdir(parents=True)
        (self.wsl_project / "app.log").write_text("line 1\nline 2\n", encoding="utf-8")
        
        # A file with spaces
        (self.wsl_project / "path with spaces.txt").write_text("content with spaces", encoding="utf-8")

        # Secret canary
        (self.wsl_project / "auth.log").write_text("api_key = ghp_123456789012345678901234567890123456\npassword: 'supersecretpass'\n", encoding="utf-8")

        self.config_file = self.root / "projects.json"
        self.config_content = f'''{{
            "devices": {{
                "local": {{"environment": "wsl", "alias": ["wsl"]}},
                "windows-pc": {{"environment": "windows", "alias": ["win"]}},
                "wsl-remote": {{"environment": "wsl", "alias": ["wsl2"]}}
            }},
            "projects": {{
                "fantavega": {{
                    "name": "Fantavega App",
                    "paths": {{
                        "local": "{self.wsl_project.as_posix()}",
                        "windows-pc": "C:/Projects/Fantavega"
                    }}
                }}
            }}
        }}'''
        self.config_file.write_text(self.config_content, encoding="utf-8")

    def tearDown(self):
        self.tmp.cleanup()

    def test_resolve_project_and_read_file_with_spaces(self):
        registry = ProjectRegistry(config_path=self.config_file, current_device="local", current_environment="wsl")
        res = registry.read_project_file("fantavega", "path with spaces.txt", device_id="local")
        self.assertEqual(res["content"], "content with spaces")
        self.assertEqual(res["project_id"], "fantavega")
        self.assertEqual(res["device_id"], "local")
        self.assertEqual(res["environment"], "wsl")
        self.assertFalse(res["is_truncated"])

    def test_traversal_and_symlink_are_blocked(self):
        registry = ProjectRegistry(config_path=self.config_file, current_device="local", current_environment="wsl")
        
        # Path traversal with ..
        with self.assertRaises(ProjectError) as ctx:
            registry.read_project_file("fantavega", "../outside.txt")
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

        with self.assertRaises(ProjectError) as ctx:
            registry.read_project_file("fantavega", "/etc/passwd")
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

        # Symlink outside project
        outside_file = self.root / "secret.txt"
        outside_file.write_text("TOP_SECRET", encoding="utf-8")
        link_file = self.wsl_project / "symlink.txt"
        try:
            link_file.symlink_to(outside_file)
            symlink_created = True
        except OSError:
            symlink_created = False

        if symlink_created:
            with self.assertRaises(ProjectError) as ctx:
                registry.read_project_file("fantavega", "symlink.txt")
            self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

    def test_secret_redaction(self):
        registry = ProjectRegistry(config_path=self.config_file, current_device="local", current_environment="wsl")
        res = registry.read_project_file("fantavega", "auth.log")
        self.assertNotIn("ghp_123456789012345678901234567890123456", res["content"])
        self.assertNotIn("supersecretpass", res["content"])
        self.assertIn("[REDACTED]", res["content"])

    def test_pagination_and_artifact(self):
        big_file = self.wsl_project / "big.log"
        big_content = "x" * 20000
        big_file.write_text(big_content, encoding="utf-8")

        registry = ProjectRegistry(config_path=self.config_file, current_device="local", current_environment="wsl")
        res1 = registry.read_project_file("fantavega", "big.log", offset=0, limit=8000)
        self.assertEqual(len(res1["content"]), 8000)
        self.assertEqual(res1["offset"], 0)
        self.assertEqual(res1["next_offset"], 8000)
        self.assertTrue(res1["is_truncated"])

        res2 = registry.read_project_file("fantavega", "big.log", offset=8000, limit=8000)
        self.assertEqual(len(res2["content"]), 8000)
        self.assertEqual(res2["offset"], 8000)
        self.assertEqual(res2["next_offset"], 16000)

        res3 = registry.read_project_file("fantavega", "big.log", offset=16000, limit=8000)
        self.assertEqual(len(res3["content"]), 4000)
        self.assertEqual(res3["offset"], 16000)
        self.assertIsNone(res3["next_offset"])
        self.assertFalse(res3["is_truncated"])

    def test_distinct_errors(self):
        registry = ProjectRegistry(config_path=self.config_file, current_device="local", current_environment="wsl")

        # 1. Missing file: FILE_NOT_FOUND
        with self.assertRaises(ProjectError) as ctx:
            registry.read_project_file("fantavega", "nonexistent.log")
        self.assertEqual(ctx.exception.code, "FILE_NOT_FOUND")

        # 2. Permission denied (simulated with mode 000 if not root)
        perm_file = self.wsl_project / "restricted.log"
        perm_file.write_text("private", encoding="utf-8")
        perm_file.chmod(0o000)
        try:
            if os.access(perm_file, os.R_OK):
                # If running as root in container/environment, skip or test via other means
                pass
            else:
                with self.assertRaises(ProjectError) as ctx:
                    registry.read_project_file("fantavega", "restricted.log")
                self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")
        finally:
            perm_file.chmod(0o644)

        # 3. WSL unavailable
        # Say current environment is Windows, target device is wsl-remote (environment=wsl)
        win_registry = ProjectRegistry(config_path=self.config_file, current_device="windows-pc", current_environment="windows")
        with self.assertRaises(ProjectError) as ctx:
            win_registry.read_project_file("fantavega", "app.log", device_id="wsl-remote")
        self.assertEqual(ctx.exception.code, "WSL_UNAVAILABLE")

        # 4. Device offline
        with self.assertRaises(ProjectError) as ctx:
            registry.read_project_file("fantavega", "app.log", device_id="windows-pc")
        self.assertEqual(ctx.exception.code, "DEVICE_OFFLINE")
