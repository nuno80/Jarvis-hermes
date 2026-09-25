"""Read-only local diagnostics. No network, shell execution or credential access."""
from __future__ import annotations

import argparse
import json
import os
import platform
import shutil
import tempfile
from pathlib import Path


def diagnose(disk_path: Path, vault_path: str | None) -> dict:
    usage = shutil.disk_usage(disk_path)
    release = platform.release().lower()
    vault_status = "not_configured"
    if vault_path:
        vault = Path(vault_path).expanduser()
        vault_status = "available" if vault.is_absolute() and vault.is_dir() else "unavailable"
    return {
        "schema_version": "1.0",
        "scope": "current_host_only",
        "host": {
            "system": platform.system(),
            "python": platform.python_version(),
            "is_wsl": platform.system() == "Linux" and "microsoft" in release,
        },
        "disk": {"total_bytes": usage.total, "used_bytes": usage.used, "free_bytes": usage.free},
        "vault": {"status": vault_status, "contents_read": False},
        "integrations": {
            "hermes": "not_verified",
            "telegram": "not_verified",
            "windows_gui": "not_verified",
            "mcp": "stdio_available",
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="JARVIS local read-only diagnostics")
    sub = parser.add_subparsers(dest="command", required=True)
    doctor = sub.add_parser("doctor", help="Inspect only the current host, without connections")
    doctor.add_argument("--json", action="store_true", dest="as_json")
    doctor.add_argument("--disk-path", type=Path, default=Path.cwd())
    sub.add_parser("serve", help="Run the local read-only MCP server over stdio")
    sub.add_parser("approval-demo", help="Run an isolated simulated approval, without Telegram or external effects")
    push_demo = sub.add_parser("push-demo", help="Run a push approval demo against an isolated local bare repository")
    args = parser.parse_args()
    if args.command == "approval-demo":
        from .approval import ApprovalError, ApprovalStore
        with tempfile.TemporaryDirectory(prefix="jarvis-approval-demo-") as directory:
            store = ApprovalStore(Path(directory) / "demo.sqlite3")
            action = {"actor_id": 123456, "target": "demo-only", "arguments": {"message": "test"}}
            pending = store.request(**action)
            accepted = store.decide(token=pending["token"], approve=True, **action)
            try:
                store.decide(token=pending["token"], approve=True, **action)
            except ApprovalError as exc:
                replay = exc.code
            print(json.dumps({"scope": "isolated_simulation", "digest": pending["digest"],
                              "first": accepted["status"], "effects_recorded": accepted["effects_recorded"],
                              "replay": replay, "ledger_total": len(store.effects()),
                              "telegram": "not_connected"}))
        return 0
    if args.command == "push-demo":
        import subprocess
        from .approval import ApprovalStore
        from .projects import ProjectRegistry
        with tempfile.TemporaryDirectory(prefix="jarvis-push-demo-") as directory:
            base = Path(directory)
            remote_bare = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote_bare)], check=True, capture_output=True)
            local_repo = base / "local_repo"
            local_repo.mkdir()
            subprocess.run(["git", "init", "-b", "main", str(local_repo)], check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=local_repo, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=local_repo, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_bare)], cwd=local_repo, check=True)
            (local_repo / "README.md").write_text("# Demo\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=local_repo, check=True, capture_output=True)
            subprocess.run(["git", "commit", "-m", "demo commit"], cwd=local_repo, check=True, capture_output=True)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=local_repo, capture_output=True, text=True, check=True).stdout.strip()

            cfg_file = base / "projects.json"
            cfg_file.write_text(json.dumps({
                "devices": {"local": {"environment": "wsl"}},
                "projects": {
                    "demo-project": {
                        "paths": {"local": str(local_repo)},
                        "allowed_remotes": ["origin"],
                        "allowed_branches": ["main"]
                    }
                }
            }), encoding="utf-8")

            store = ApprovalStore(base / "approvals.sqlite3")
            actor_id = 123456
            req = store.request(
                actor_id=actor_id,
                target="git_push:demo-project",
                arguments={"remote": "origin", "branch": "main", "commit_hash": commit_hash}
            )
            registry = ProjectRegistry(cfg_file, current_device="local", current_environment="wsl", approval_store=store)
            res = registry.push_project_commit(
                project_id="demo-project",
                remote="origin",
                branch="main",
                commit_hash=commit_hash,
                approval_token=req["token"],
                actor_id=actor_id
            )
            print(json.dumps({
                "scope": "isolated_push_demo",
                "pushed": res["pushed"],
                "remote_verified": res["remote_verified"],
                "commit_hash": res["commit_hash"],
                "branch": res["branch"],
                "remote": res["remote"]
            }))
        return 0
    if args.command == "serve":
        from .server import main as serve
        serve()
        return 0
    try:
        report = diagnose(args.disk_path, os.environ.get("JARVIS_VAULT_PATH"))
    except OSError:
        # Do not echo paths or exception strings that could contain private data.
        parser.exit(2, "Cannot inspect the requested disk path. Check that it exists and is accessible.\n")
    if args.as_json:
        print(json.dumps(report, indent=2))
    else:
        print(f"Host: {report['host']['system']}; Python: {report['host']['python']}")
        print(f"Free disk: {report['disk']['free_bytes'] / (1024**3):.1f} GiB")
        print(f"Vault: {report['vault']['status']}")
        print("Hermes, Telegram and Windows GUI: not verified. MCP: local stdio server available; host connection not verified.")
        print("This report describes the current host only.")
    return 0
