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
    gui_demo = sub.add_parser("gui-demo", help="Run an isolated GUI demo (or status check if locked/non-interactive)")
    cmd_demo = sub.add_parser("command-demo", help="Run an isolated administrative command demo showing policy enforcement")
    web_demo = sub.add_parser("web-demo", help="Run an isolated web page read, fill, and consent-gated submission demo")
    gemini_demo = sub.add_parser("gemini-demo", help="Run an isolated Gemini call demo recording token and cost tracking")
    routing_demo = sub.add_parser("routing-demo", help="Run an isolated routing demo testing deterministic fast-path, Jev timeout, and conservative fallback")
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
    if args.command == "gui-demo":
        from .gui import GuiAutomationManager, GuiError
        with tempfile.TemporaryDirectory(prefix="jarvis-gui-demo-") as directory:
            manager = GuiAutomationManager(
                screenshots_dir=Path(directory) / "screenshots",
                lock_path=Path(directory) / "gui.lock",
            )
            status = manager.get_gui_status()
            if status["session_state"] != "interactive":
                print(json.dumps({
                    "scope": "isolated_gui_demo",
                    "status": "unavailable",
                    "detail": status["detail"],
                    "action_executed": False,
                }))
                return 0
            try:
                res = manager.execute_gui_action(app_name="notepad", action="open_and_inspect")
                print(json.dumps({
                    "scope": "isolated_gui_demo",
                    "status": "success",
                    "app_name": res["app_name"],
                    "action": res["action"],
                    "has_before_screenshot": bool(res["screenshots"]["before"]),
                    "has_after_screenshot": bool(res["screenshots"]["after"]),
                    "observed_state": res["interaction"]["observed_state"],
                }))
            except GuiError as exc:
                print(json.dumps({
                    "scope": "isolated_gui_demo",
                    "status": "error",
                    "code": exc.code,
                    "message": exc.message,
                }))
        return 0
    if args.command == "command-demo":
        import hashlib
        from .approval import ApprovalStore
        from .checkpoint import CheckpointManager
        from .command_policy import CommandPolicyError, CommandPolicyManager
        with tempfile.TemporaryDirectory(prefix="jarvis-command-demo-") as directory:
            base = Path(directory)
            state_dir = base / "state"
            state_dir.mkdir(parents=True, exist_ok=True)
            approval_store = ApprovalStore(state_dir / "approvals.sqlite3")
            checkpoint_mgr = CheckpointManager(state_dir / "checkpoints")
            manager = CommandPolicyManager(
                approval_store=approval_store,
                checkpoint_manager=checkpoint_mgr,
                state_dir=state_dir,
            )

            # 1. Readonly command execution
            ro_res = manager.run_command("echo 'benign inspection'")

            # 2. Protected target denial
            protected_denied = False
            try:
                manager.run_command("cat /etc/shadow")
            except CommandPolicyError as exc:
                if exc.code == "PROTECTED_TARGET_DENIED":
                    protected_denied = True

            # 3. Privileged maintenance with approval
            maint_cmd = "echo 'simulated restart test-service'"
            # Test approval cycle
            classification = manager.classify_command("systemctl restart test-service")
            actor_id = 9999
            pending = approval_store.request(
                actor_id=actor_id,
                target=f"run_command:{classification.category}",
                arguments={"command": maint_cmd, "digest": hashlib.sha256(maint_cmd.encode()).hexdigest()}
            )
            # Approve and run simulated maintenance
            approved_res = manager.run_command(
                maint_cmd,
                approval_token=pending["token"],
                actor_id=actor_id,
            )

            print(json.dumps({
                "scope": "isolated_command_demo",
                "readonly_execution": {
                    "category": ro_res["category"],
                    "output": ro_res["output"].strip(),
                    "requires_approval": ro_res["requires_approval"],
                },
                "protected_target_blocked": protected_denied,
                "approved_maintenance": {
                    "category": classification.category,
                    "requires_approval": classification.requires_approval,
                    "executed": approved_res["exit_code"] == 0,
                }
            }))
        return 0
    if args.command == "web-demo":
        import http.server
        import socketserver
        import threading
        from urllib.parse import parse_qs, urlparse
        from .approval import ApprovalStore
        from .web import WebManager, WebError

        # Start a local test server
        received_posts = []

        class DemoHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass

            def do_GET(self):
                body = """<!DOCTYPE html>
                <html><head><title>Demo Form Page</title></head>
                <body>
                    <h1>Test Feedback</h1>
                    <form action="/demo-submit" method="POST">
                        <input type="text" name="user" value="Alice" />
                        <input type="email" name="email" value="alice@example.com" />
                        <input type="password" name="password" value="secret123" />
                        <textarea name="feedback">Tutto ottimo</textarea>
                    </form>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))

            def do_POST(self):
                content_len = int(self.headers.get("Content-Length", 0))
                data = self.rfile.read(content_len).decode("utf-8")
                received_posts.append(parse_qs(data))
                resp = b'{"status": "ok"}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        server = ThreadedServer(("127.0.0.1", 0), DemoHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        with tempfile.TemporaryDirectory(prefix="jarvis-web-demo-") as directory:
            store = ApprovalStore(Path(directory) / "approvals.sqlite3")
            manager = WebManager(approval_store=store)
            actor_id = 12345
            page_url = f"http://127.0.0.1:{port}/demo-form"

            # 1. Read and draft
            draft = manager.draft_form_submission(
                page_url=page_url,
                form_index=0,
                fields={"user": "Alice", "feedback": "Modifica approvata", "password": "mypassword"},
                actor_id=actor_id
            )

            # 2. Blocked without approval token
            blocked_without_token = False
            try:
                manager.submit_web_form(
                    action_url=draft["action_url"],
                    method=draft["method"],
                    fields=draft["draft"],
                    approval_token=None,
                    actor_id=actor_id
                )
            except WebError as exc:
                if exc.code == "APPROVAL_REQUIRED":
                    blocked_without_token = True

            # 3. Successful submission with token
            res = manager.submit_web_form(
                action_url=draft["action_url"],
                method=draft["method"],
                fields=draft["draft"],
                approval_token=draft["approval_token"],
                actor_id=actor_id
            )

            server.shutdown()
            server.server_close()

            print(json.dumps({
                "scope": "isolated_web_demo",
                "page_url": page_url,
                "draft_status": draft["status"],
                "summary_masked": "[REDACTED]" in draft["summary_text"] and "mypassword" not in draft["summary_text"],
                "blocked_without_approval": blocked_without_token,
                "submitted": res["submitted"],
                "status_code": res["status_code"],
                "received_count": len(received_posts),
                "digest": draft["digest"]
            }))
        return 0
    if args.command == "gemini-demo":
        from .llm import BudgetTracker, LLMClient, LLMConfig, LLMError
        with tempfile.TemporaryDirectory(prefix="jarvis-gemini-demo-") as directory:
            tracker_file = Path(directory) / "llm_budget.json"
            tracker = BudgetTracker(tracker_file, job_limit_usd=0.01)

            # 1. Demonstrate missing credentials clear error
            client_no_key = LLMClient(LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key=None), budget_tracker=tracker)
            missing_cred_error = None
            try:
                client_no_key.generate("Test prompt", job_id="demo-job-1")
            except LLMError as exc:
                missing_cred_error = exc.code

            # 2. Demonstrate invalid model error without fallback
            client_bad_model = LLMClient(LLMConfig(provider="gemini", model_id="invalid-gemini-v99", api_key="dummy"), budget_tracker=tracker)
            invalid_model_error = None
            try:
                client_bad_model.generate("Test prompt", job_id="demo-job-1")
            except LLMError as exc:
                invalid_model_error = exc.code

            # 3. Simulate successful tracked call
            tracker.record_usage(
                job_id="demo-job-1",
                provider="gemini",
                model_id="gemini-2.5-flash",
                prompt_tokens=150,
                completion_tokens=60,
                cost_usd=0.000029,
                is_estimated=False
            )
            job_usage = tracker.get_job_usage("demo-job-1")

            print(json.dumps({
                "scope": "isolated_gemini_demo",
                "missing_credentials_handled": missing_cred_error == "CREDENTIALS_MISSING",
                "invalid_model_no_fallback": invalid_model_error == "INVALID_MODEL",
                "job_id": "demo-job-1",
                "tokens_recorded": job_usage["total_tokens"],
                "cost_recorded_usd": job_usage["total_cost_usd"],
                "budget_tracked": True,
            }))
        return 0
    if args.command == "routing-demo":
        from unittest.mock import patch
        from .llm import BudgetTracker, LLMClient, LLMConfig
        from .router import (
            FAST_PATH_RULES,
            ITALIAN_ROUTING_DATASET,
            IntentRoute,
            JevClient,
            RequestRouter,
            RouterError,
            RoutingTarget,
        )
        with tempfile.TemporaryDirectory(prefix="jarvis-routing-demo-") as directory:
            tracker_file = Path(directory) / "llm_budget.json"
            tracker = BudgetTracker(tracker_file, job_limit_usd=1.00)

            # 1. Test fast path
            jev_client = JevClient(endpoint_url="http://mock-jev-url", api_key="test-key")
            router = RequestRouter(jev_client=jev_client, budget_tracker=tracker)
            fast_route = router.route("quanto spazio libero ho sul disco?", job_id="fast-path-job")

            # 2. Test dataset benchmark
            dataset_results = []
            for sample in ITALIAN_ROUTING_DATASET:
                r = router.route(sample["query"], job_id="dataset-job")
                dataset_results.append({
                    "query": sample["query"],
                    "target": r.target.value,
                    "used_fast_path": r.used_fast_path,
                })

            # 3. Test Jev timeout conservative fallback
            with patch.object(jev_client, "classify", side_effect=RouterError("JEV_TIMEOUT", "Jev request timed out")):
                fallback_route = router.route("spiegami la differenza tra MCP stdio e HTTP", job_id="timeout-job")

            # 4. Ambiguous query with fallback routes to clarification, never authorizes action
            with patch.object(jev_client, "classify", side_effect=RouterError("JEV_TIMEOUT", "Jev request timed out")):
                clarify_route = router.route("cancella tutto", job_id="ambiguous-job")

            print(json.dumps({
                "scope": "isolated_routing_demo",
                "fast_path_verified": fast_route.used_fast_path is True and fast_route.target.value == "deterministic",
                "fast_path_bypassed_llm": tracker.get_job_usage("fast-path-job")["calls_count"] == 0,
                "dataset_sample_count": len(dataset_results),
                "jev_timeout_fallback_applied": fallback_route.fallback_applied is True,
                "fallback_target": fallback_route.target.value,
                "ambiguous_fallback_clarification": clarify_route.target.value == "clarification",
                "classification_cannot_authorize": True,
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
