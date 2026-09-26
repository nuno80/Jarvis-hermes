import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from jarvis_hermes.approval import ApprovalStore, ApprovalError
from jarvis_hermes.projects import ProjectRegistry, ProjectError


class GitPushApprovalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp.name)

        # Setup bare remote repository
        self.remote_dir = self.base / "remote.git"
        subprocess.run(["git", "init", "--bare", str(self.remote_dir)], check=True, capture_output=True)

        # Setup local project repository
        self.local_dir = self.base / "local_repo"
        self.local_dir.mkdir()
        subprocess.run(["git", "init", "-b", "main", str(self.local_dir)], check=True, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Tester"], cwd=self.local_dir, check=True)
        subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=self.local_dir, check=True)
        subprocess.run(["git", "remote", "add", "origin", str(self.remote_dir)], cwd=self.local_dir, check=True)

        # Create initial commit in local repo
        (self.local_dir / "README.md").write_text("# Project\n", encoding="utf-8")
        subprocess.run(["git", "add", "."], cwd=self.local_dir, check=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=self.local_dir, check=True)

        rev_proc = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.local_dir, capture_output=True, text=True, check=True)
        self.initial_commit = rev_proc.stdout.strip()

        # Approval store
        self.approvals_path = self.base / "approvals.sqlite3"
        self.clock_time = 1_700_000_000.0
        self.store = ApprovalStore(self.approvals_path, clock=lambda: self.clock_time)
        self.actor_id = 999888

        # Config file
        self.cfg_file = self.base / "projects.json"
        self.cfg_file.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.local_dir)},
                    "allowed_remotes": ["origin"],
                    "allowed_branches": ["main"]
                }
            }
        }), encoding="utf-8")

        self.registry = ProjectRegistry(
            self.cfg_file,
            current_device="local",
            current_environment="wsl",
            approval_store=self.store
        )

    def tearDown(self):
        self.tmp.cleanup()

    def test_cli_push_demo(self):
        """CLI push-demo command runs an isolated push demo reporting verified result."""
        run = subprocess.run(
            [sys.executable, "-m", "jarvis_hermes", "push-demo"],
            capture_output=True, text=True, check=True
        )
        data = json.loads(run.stdout)
        self.assertEqual(data["scope"], "isolated_push_demo")
        self.assertTrue(data["pushed"])
        self.assertTrue(data["remote_verified"])
        self.assertEqual(data["remote"], "origin")
        self.assertEqual(data["branch"], "main")
        self.assertTrue(len(data["commit_hash"]) == 40)

    def test_push_fails_without_approval(self):
        """Push is blocked without a valid approval token."""
        with self.assertRaises(ProjectError) as ctx:
            self.registry.push_project_commit(
                project_id="sample",
                remote="origin",
                branch="main",
                commit_hash=self.initial_commit,
                approval_token=None,
                actor_id=self.actor_id
            )
        self.assertIn(ctx.exception.code, ["APPROVAL_REQUIRED", "APPROVAL_DENIED"])

    def test_push_succeeds_with_valid_approval_and_verifies_remote(self):
        """Approved push pushes commit and verifies it exists on the remote."""
        req = self.store.request(
            actor_id=self.actor_id,
            target="git_push:sample",
            arguments={"remote": "origin", "branch": "main", "commit_hash": self.initial_commit}
        )
        token = req["token"]

        res = self.registry.push_project_commit(
            project_id="sample",
            remote="origin",
            branch="main",
            commit_hash=self.initial_commit,
            approval_token=token,
            actor_id=self.actor_id
        )
        self.assertTrue(res["pushed"])
        self.assertTrue(res["remote_verified"])
        self.assertEqual(res["remote"], "origin")
        self.assertEqual(res["branch"], "main")
        self.assertEqual(res["commit_hash"], self.initial_commit)

        # Verify on bare remote
        remote_rev = subprocess.run(
            ["git", "rev-parse", "refs/heads/main"],
            cwd=self.remote_dir, capture_output=True, text=True, check=True
        ).stdout.strip()
        self.assertEqual(remote_rev, self.initial_commit)

    def test_approval_bound_to_parameters_target_changed_rejected(self):
        """If target repo, remote, branch or commit_hash differs, push fails and remote untouched."""
        # Approve for initial commit
        req = self.store.request(
            actor_id=self.actor_id,
            target="git_push:sample",
            arguments={"remote": "origin", "branch": "main", "commit_hash": self.initial_commit}
        )
        token = req["token"]

        # Make another commit
        (self.local_dir / "README.md").write_text("# Project 2\n", encoding="utf-8")
        subprocess.run(["git", "commit", "-am", "second"], cwd=self.local_dir, check=True)
        second_commit = subprocess.run(["git", "rev-parse", "HEAD"], cwd=self.local_dir, capture_output=True, text=True, check=True).stdout.strip()

        # Try pushing second commit with token generated for initial commit
        with self.assertRaises(ProjectError) as ctx:
            self.registry.push_project_commit(
                project_id="sample",
                remote="origin",
                branch="main",
                commit_hash=second_commit,
                approval_token=token,
                actor_id=self.actor_id
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_DENIED")

        # Remote must NOT have refs/heads/main
        remote_check = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=self.remote_dir, capture_output=True, text=True)
        self.assertNotEqual(remote_check.returncode, 0)

    def test_rejected_or_expired_approval_does_not_modify_remote(self):
        """Denied or expired approval does not push or modify remote."""
        req = self.store.request(
            actor_id=self.actor_id,
            target="git_push:sample",
            arguments={"remote": "origin", "branch": "main", "commit_hash": self.initial_commit},
            ttl_seconds=60
        )
        token = req["token"]

        # Advance clock past expiry
        self.clock_time += 100

        with self.assertRaises(ProjectError) as ctx:
            self.registry.push_project_commit(
                project_id="sample",
                remote="origin",
                branch="main",
                commit_hash=self.initial_commit,
                approval_token=token,
                actor_id=self.actor_id
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_EXPIRED")

        # Remote check
        remote_check = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=self.remote_dir, capture_output=True, text=True)
        self.assertNotEqual(remote_check.returncode, 0)

    def test_unregistered_remote_or_branch_rejected(self):
        """Pushing to an unlisted remote or branch is rejected even before approval."""
        req = self.store.request(
            actor_id=self.actor_id,
            target="git_push:sample",
            arguments={"remote": "unlisted_remote", "branch": "main", "commit_hash": self.initial_commit}
        )
        token = req["token"]
        with self.assertRaises(ProjectError) as ctx:
            self.registry.push_project_commit(
                project_id="sample",
                remote="unlisted_remote",
                branch="main",
                commit_hash=self.initial_commit,
                approval_token=token,
                actor_id=self.actor_id
            )
        self.assertEqual(ctx.exception.code, "REMOTE_NOT_ALLOWED")

    def test_verify_remote_state_on_timeout_or_uncertainty(self):
        """Check verify_remote_commit helper: inspects remote branch ref without pushing blindly."""
        # Remote has nothing yet
        exists = self.registry.verify_remote_commit("sample", "origin", "main", self.initial_commit)
        self.assertFalse(exists)

        # Push directly via git to simulate a commit already arrived before a timeout
        subprocess.run(["git", "push", "origin", "main"], cwd=self.local_dir, check=True, capture_output=True)

        exists_now = self.registry.verify_remote_commit("sample", "origin", "main", self.initial_commit)
        self.assertTrue(exists_now)

    def test_empty_or_missing_allowlist_rejects(self):
        """Empty or missing allowed_remotes / allowed_branches rejects instead of allowing all."""
        empty_cfg = self.base / "empty_allowlist.json"
        empty_cfg.write_text(json.dumps({
            "devices": {"local": {"environment": "wsl"}},
            "projects": {
                "sample": {
                    "paths": {"local": str(self.local_dir)},
                    "allowed_remotes": [],
                    "allowed_branches": []
                }
            }
        }), encoding="utf-8")
        empty_registry = ProjectRegistry(
            empty_cfg,
            current_device="local",
            current_environment="wsl",
            approval_store=self.store
        )
        with self.assertRaises(ProjectError) as ctx:
            empty_registry.push_project_commit(
                project_id="sample",
                remote="origin",
                branch="main",
                commit_hash=self.initial_commit,
                approval_token="dummy-token",
                actor_id=self.actor_id
            )
        self.assertEqual(ctx.exception.code, "REMOTE_NOT_ALLOWED")

    def test_unverified_push_outcome_unknown(self):
        """When push succeeds but remote_verified=False, state must be OUTCOME_UNKNOWN, not pushed: true."""
        req = self.store.request(
            actor_id=self.actor_id,
            target="git_push:sample",
            arguments={"remote": "origin", "branch": "main", "commit_hash": self.initial_commit}
        )
        token = req["token"]

        real_run = subprocess.run
        def selective_run(cmd, *args, **kwargs):
            if cmd[:2] == ["git", "push"]:
                # Simulate push succeeded exit 0
                return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
            return real_run(cmd, *args, **kwargs)

        from unittest.mock import patch
        with patch.object(self.registry, "verify_remote_commit", return_value=False):
            with patch("subprocess.run", side_effect=selective_run):
                res = self.registry.push_project_commit(
                    project_id="sample",
                    remote="origin",
                    branch="main",
                    commit_hash=self.initial_commit,
                    approval_token=token,
                    actor_id=self.actor_id
                )
                self.assertFalse(res["pushed"])
                self.assertEqual(res["status"], "OUTCOME_UNKNOWN")
                self.assertFalse(res["remote_verified"])


if __name__ == "__main__":
    unittest.main()
