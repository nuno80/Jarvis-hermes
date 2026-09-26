import subprocess
import unittest
import tempfile
import json
from pathlib import Path
from jarvis_hermes.projects import ProjectRegistry, ProjectError


class WorkflowAndCommitTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.proj_dir = Path(self.tmp.name) / "test_repo"
        self.proj_dir.mkdir(parents=True)
        # Initialize git repo
        subprocess.run(["git", "init"], cwd=self.proj_dir, check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=self.proj_dir, check=True)

        self.cfg_file = Path(self.tmp.name) / "projects.json"

    def tearDown(self):
        self.tmp.cleanup()

    def test_run_project_workflow_allowed_command_success(self):
        """Workflow executes an allowed test/build command and reports success."""
        # Initial commit with a test file
        test_file = self.proj_dir / "test_sample.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_ok(self): self.assertTrue(True)\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "test": {
                            "command": ["python3", "-m", "unittest", "discover", "-s", ".", "-v"]
                        }
                    }
                }
            }
        }), encoding="utf-8")

        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        res = registry.run_project_workflow("sample", "test")
        self.assertTrue(res["ok"])
        self.assertEqual(res["exit_code"], 0)
        self.assertIn("Ran 1 test", res["output"])

    def test_unregistered_or_arbitrary_workflow_rejected(self):
        """Attempting to run an unlisted command or unknown workflow is rejected."""
        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "test": {"command": ["python3", "-m", "unittest"]}
                    }
                }
            }
        }), encoding="utf-8")

        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        with self.assertRaises(ProjectError) as ctx:
            registry.run_project_workflow("sample", "malicious_workflow")
        self.assertEqual(ctx.exception.code, "WORKFLOW_NOT_ALLOWED")

    def test_workflow_detects_modified_or_untrusted_git_hook(self):
        """If git hooks are modified or untrusted, workflow fails closed."""
        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "test": {"command": ["python3", "-c", "print('ok')"]}
                    }
                }
            }
        }), encoding="utf-8")

        # Inject a git hook
        hook_path = self.proj_dir / ".git" / "hooks" / "pre-commit"
        hook_path.write_text("#!/bin/sh\necho evil\n", encoding="utf-8")
        hook_path.chmod(0o755)

        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        with self.assertRaises(ProjectError) as ctx:
            registry.run_project_workflow("sample", "test")
        self.assertEqual(ctx.exception.code, "HOOK_MODIFIED")

    def test_workflow_detects_modified_script(self):
        """If a script specified in workflow files/dependencies has an altered hash, reject it."""
        script_file = self.proj_dir / "build.sh"
        script_file.write_text("echo building\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "add script"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "build": {
                            "command": ["bash", "build.sh"],
                            "expected_hashes": {"build.sh": "0000000000000000000000000000000000000000000000000000000000000000"}
                        }
                    }
                }
            }
        }), encoding="utf-8")

        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        with self.assertRaises(ProjectError) as ctx:
            registry.run_project_workflow("sample", "build")
        self.assertEqual(ctx.exception.code, "SCRIPT_MODIFIED")

    def test_workflow_failure_is_communicated_with_output(self):
        """Failed test check reports failure explicitly and does not present as success."""
        test_file = self.proj_dir / "test_sample.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_fail(self): self.fail('intentional failure')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "fail test"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "test": {"command": ["python3", "-m", "unittest", "discover", "-s", "."]}
                    }
                }
            }
        }), encoding="utf-8")

        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        res = registry.run_project_workflow("sample", "test")
        self.assertFalse(res["ok"])
        self.assertNotEqual(res["exit_code"], 0)
        self.assertIn("FAIL", res["output"])

    def test_commit_project_changes_commits_only_specified_files_preserving_unrelated(self):
        """Commit stages and commits only relevant files modified by the job, preserving unrelated modifications."""
        # Initial commit
        (self.proj_dir / "f1.py").write_text("orig 1\n", encoding="utf-8")
        (self.proj_dir / "f2.py").write_text("orig 2\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "initial files"], cwd=self.proj_dir, check=True)

        # Job modifies f1.py; unrelated user modification in f2.py; untracked file f3.txt
        (self.proj_dir / "f1.py").write_text("modified by job\n", encoding="utf-8")
        (self.proj_dir / "f2.py").write_text("unrelated user edit\n", encoding="utf-8")
        (self.proj_dir / "f3.txt").write_text("untracked\n", encoding="utf-8")

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {
                        "check": {"command": ["python3", "-c", "print('ok')"]}
                    }
                }
            }
        }), encoding="utf-8")

        state_dir = Path(self.tmp.name) / "state"
        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl", state_dir=state_dir)
        wf_res = registry.run_project_workflow("sample", "check")
        self.assertTrue(wf_res["ok"])
        vr_id = wf_res["verification_run_id"]

        res = registry.commit_project_changes(
            project_id="sample",
            files=["f1.py"],
            commit_message="Fix small issue in f1",
            verification_run_id=vr_id
        )
        self.assertTrue(res["committed"])
        self.assertTrue(res["commit_hash"])
        self.assertEqual(res["files"], ["f1.py"])
        self.assertEqual(res["verification_run_id"], vr_id)

        # Verify git status in repo: f2.py is still modified in working tree! f3.txt still untracked!
        status_proc = subprocess.run(["git", "status", "--porcelain"], cwd=self.proj_dir, capture_output=True, text=True, check=True)
        self.assertIn(" M f2.py", status_proc.stdout)
        self.assertIn("?? f3.txt", status_proc.stdout)
        self.assertNotIn("f1.py", status_proc.stdout)

        # Inspect commit diff: only f1.py is touched
        diff_proc = subprocess.run(["git", "show", "--stat", res["commit_hash"]], cwd=self.proj_dir, capture_output=True, text=True, check=True)
        self.assertIn("f1.py", diff_proc.stdout)
        self.assertNotIn("f2.py", diff_proc.stdout)
        self.assertNotIn("f3.txt", diff_proc.stdout)

    def test_commit_rejects_dictionary_verification(self):
        """Dict verification (untrusted model claim) is rejected with VERIFICATION_REQUIRED."""
        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {"sample": {"paths": {"local": str(self.proj_dir)}}}
        }), encoding="utf-8")
        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl")
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes(
                project_id="sample",
                files=["f1.py"],
                commit_message="dummy",
                verification_run_id={"passed": True}  # type: ignore
            )
        self.assertEqual(ctx.exception.code, "VERIFICATION_REQUIRED")

    def test_commit_rejects_missing_failed_or_wrong_project_run(self):
        """Missing run, failed run, or run belonging to another project is rejected."""
        test_file = self.proj_dir / "test_sample.py"
        test_file.write_text("import unittest\nclass T(unittest.TestCase):\n    def test_fail(self): self.fail('err')\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {"test": {"command": ["python3", "-m", "unittest", "discover", "-s", "."]}}
                },
                "other": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {"test": {"command": ["python3", "-c", "print('ok')"]}}
                }
            }
        }), encoding="utf-8")
        state_dir = Path(self.tmp.name) / "state"
        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl", state_dir=state_dir)

        # 1. Non-existent verification run
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["test_sample.py"], "msg", "vr-nonexistent")
        self.assertEqual(ctx.exception.code, "VERIFICATION_REQUIRED")

        # 2. Failed verification run
        failed_res = registry.run_project_workflow("sample", "test")
        self.assertFalse(failed_res["ok"])
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["test_sample.py"], "msg", failed_res["verification_run_id"])
        self.assertEqual(ctx.exception.code, "VERIFICATION_REQUIRED")

        # 3. Wrong project verification run
        other_res = registry.run_project_workflow("other", "test")
        self.assertTrue(other_res["ok"])
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["test_sample.py"], "msg", other_res["verification_run_id"])
        self.assertEqual(ctx.exception.code, "VERIFICATION_REQUIRED")

    def test_commit_rejects_file_modified_after_verification(self):
        """Files changed after verification run are rejected."""
        f = self.proj_dir / "app.py"
        f.write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {"test": {"command": ["python3", "-c", "print('ok')"]}}
                }
            }
        }), encoding="utf-8")
        state_dir = Path(self.tmp.name) / "state"
        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl", state_dir=state_dir)

        wf_res = registry.run_project_workflow("sample", "test")
        self.assertTrue(wf_res["ok"])
        vr_id = wf_res["verification_run_id"]

        # Now tamper with app.py AFTER verification
        f.write_text("v2 altered\n", encoding="utf-8")

        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["app.py"], "tampered msg", vr_id)
        self.assertEqual(ctx.exception.code, "VERIFICATION_REQUIRED")

    def test_commit_rejects_untrusted_git_hook_or_allows_configured_no_verify(self):
        """Commit executes _verify_git_hooks and rejects altered hooks; allows --no-verify only if configured."""
        f = self.proj_dir / "app.py"
        f.write_text("v1\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.proj_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.proj_dir, check=True)

        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {"test": {"command": ["python3", "-c", "print('ok')"]}},
                    "allow_no_verify": False
                }
            }
        }), encoding="utf-8")
        state_dir = Path(self.tmp.name) / "state"
        registry = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl", state_dir=state_dir)

        f.write_text("v2\n", encoding="utf-8")
        wf_res = registry.run_project_workflow("sample", "test")
        self.assertTrue(wf_res["ok"])
        vr_id = wf_res["verification_run_id"]

        # Alter git hook before commit
        hook = self.proj_dir / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        hook.chmod(0o755)

        # 1. Commit with untrusted hook fails HOOK_MODIFIED
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["app.py"], "commit with hook", vr_id)
        self.assertEqual(ctx.exception.code, "HOOK_MODIFIED")

        # 2. Attempting --no-verify when allow_no_verify is False fails PERMISSION_DENIED
        with self.assertRaises(ProjectError) as ctx:
            registry.commit_project_changes("sample", ["app.py"], "commit no verify", vr_id, no_verify=True)
        self.assertEqual(ctx.exception.code, "PERMISSION_DENIED")

        # 3. If allow_no_verify is True in config, commit with no_verify succeeds
        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.proj_dir)},
                    "workflows": {"test": {"command": ["python3", "-c", "print('ok')"]}},
                    "allow_no_verify": True
                }
            }
        }), encoding="utf-8")
        registry_allowed = ProjectRegistry(self.cfg_file, current_device="local", current_environment="wsl", state_dir=state_dir)
        res = registry_allowed.commit_project_changes("sample", ["app.py"], "commit no verify", vr_id, no_verify=True)
        self.assertTrue(res["committed"])
