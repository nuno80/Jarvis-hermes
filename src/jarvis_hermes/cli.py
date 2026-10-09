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
    cancel_demo = sub.add_parser("cancel-demo", help="Run an isolated job cancellation demo with a real subprocess (sleep)")
    travel_demo = sub.add_parser("travel-demo", help="Run a travel provider demo showing normalization and credentials check")
    travel_demo.add_argument("--origin", default="MXP", help="Origin IATA code")
    travel_demo.add_argument("--destination", default="JFK", help="Destination IATA code")
    travel_demo.add_argument("--date", default="2026-11-15", help="Departure date (YYYY-MM-DD)")
    travel_demo.add_argument("--passengers", type=int, default=1, help="Number of passengers")
    travel_demo.add_argument("--json", action="store_true", dest="as_json")
    calendar_demo = sub.add_parser("calendar-demo", help="Run an isolated Google Calendar read + local draft demo (no event created)")
    calendar_demo.add_argument("--query", default="", help="Free text search")
    calendar_demo.add_argument("--json", action="store_true", dest="as_json")
    email_demo = sub.add_parser("email-demo", help="Run an isolated IMAP email read + local reply draft demo (no email sent)")
    email_demo.add_argument("--query", default="", help="Free text or subject search")
    email_demo.add_argument("--json", action="store_true", dest="as_json")
    web_demo = sub.add_parser("web-demo", help="Run an isolated web page read, fill, and consent-gated submission demo")
    gemini_demo = sub.add_parser("gemini-demo", help="Run an isolated Gemini call demo recording token and cost tracking")
    routing_demo = sub.add_parser("routing-demo", help="Run an isolated routing demo testing deterministic fast-path, Jev timeout, and conservative fallback")
    voice_demo = sub.add_parser("voice-demo", help="Run an isolated Telegram voice flow demo (STT usage, ack <=3s, clarification, no consent)")
    voice_demo.add_argument("--json", action="store_true", dest="as_json")
    routing_report = sub.add_parser("routing-report", help="Print routing decisions telemetry report (p50/p95 latency, escalation rate, path distribution)")
    routing_report.add_argument("--json", action="store_true", dest="as_json", help="Output report in JSON format")
    routing_report.add_argument("--cleanup", action="store_true", help="Run retention cleanup before generating report")
    calibrate = sub.add_parser("routing-calibrate", help="Reproducible calibration report: accuracy per question, ECE, escalation rate, per-action-class thresholds (issue 40 / JARVIS-38)")
    calibrate.add_argument("--dataset", default=str(Path(__file__).resolve().parents[2] / "tests" / "data" / "routing_it.jsonl"), help="Path to the labeled routing dataset (JSONL)")
    calibrate.add_argument("--thresholds", default=None, help="Path to the thresholds config JSON (default: config/routing_thresholds.json)")
    calibrate.add_argument("--seed", type=int, default=42, help="Deterministic split seed")
    calibrate.add_argument("--json", action="store_true", dest="as_json", help="Output report in JSON format")
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
    if args.command == "cancel-demo":
        from .jobs import JobStore
        import subprocess
        with tempfile.TemporaryDirectory(prefix="jarvis-cancel-demo-") as directory:
            db_path = Path(directory) / "jobs.sqlite3"
            store = JobStore(db_path)
            # Create a long-running process
            proc = subprocess.Popen(["sleep", "60"], start_new_session=True)
            job = store.create_job(actor_id=12345, target="long_running_task", description="Demo sleep process")
            jid = job["job_id"]
            pgid = os.getpgid(proc.pid) if hasattr(os, "getpgid") else proc.pid
            store.register_process(job_id=jid, pid=proc.pid, pgid=pgid)

            cancel_res = store.cancel_job(jid, timeout_seconds=3.0)
            proc.poll()
            is_alive = store._is_process_alive(proc.pid)

            print(json.dumps({
                "scope": "isolated_cancel_demo",
                "job_id": jid,
                "status": cancel_res["status"],
                "process_stopped": cancel_res["process_stopped"],
                "process_alive_after_cancel": is_alive,
                "note": cancel_res["note"],
                "verified": cancel_res["process_stopped"] is True and not is_alive,
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
        from .telemetry import RoutingDecisionStore
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
            telemetry_store = RoutingDecisionStore(Path(directory) / "routing_decisions.sqlite3")

            # 1. Test fast path
            jev_client = JevClient(endpoint_url="http://mock-jev-url", api_key="test-key")
            router = RequestRouter(jev_client=jev_client, budget_tracker=tracker, telemetry_store=telemetry_store)
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

            # Verify telemetry recording and report generation
            demo_report = telemetry_store.generate_report()

            print(json.dumps({
                "scope": "isolated_routing_demo",
                "fast_path_verified": fast_route.used_fast_path is True and fast_route.target.value == "deterministic",
                "fast_path_bypassed_llm": tracker.get_job_usage("fast-path-job")["calls_count"] == 0,
                "dataset_sample_count": len(dataset_results),
                "jev_timeout_fallback_applied": fallback_route.fallback_applied is True,
                "fallback_target": fallback_route.target.value,
                "ambiguous_fallback_clarification": clarify_route.target.value == "clarification",
                "classification_cannot_authorize": True,
                "telemetry_recorded_count": demo_report["total_decisions"],
            }))
        return 0
    if args.command == "voice-demo":
        import time as _time
        from .jobs import JobStore
        from .llm import BudgetTracker
        from .voice import (
            ACK_LATENCY_BUDGET_MS, AUDIO_RETENTION_POLICY,
            VOICE_MAX_DURATION_SECONDS, VoiceError, handle_voice_transcript,
        )
        with tempfile.TemporaryDirectory(prefix="jarvis-voice-demo-") as directory:
            base = Path(directory)
            tracker = BudgetTracker(base / "llm_budget.json",
                                    job_limit_usd=1.00, daily_limit_usd=10.00)
            store = JobStore(base / "jobs.sqlite3")

            def _dispatch(text):
                lowered = text.lower()
                if "spazio libero" in lowered or "spazio" in lowered:
                    return {"path": "system_1", "target": "deterministic",
                            "intent": "disk_usage", "handler": "disk_usage",
                            "confidence": 1.0, "reply": "Spazio libero: 10.0 GiB.",
                            "escalation_reason": None}
                if "cancella" in lowered or "elimina" in lowered:
                    return {"path": "system_2", "target": "clarification",
                            "intent": "ambiguous", "handler": "ask_clarification",
                            "confidence": 0.4, "reply": None,
                            "escalation_reason": "needs_clarification"}
                return {"path": "system_2", "target": "reasoning_llm",
                        "intent": "web_search", "handler": "web_search",
                        "confidence": 0.5, "reply": None,
                        "escalation_reason": "low_confidence"}

            latencies = {}

            def _run(label, transcript, **kw):
                started = _time.perf_counter()
                res = handle_voice_transcript(
                    transcript, actor_id=123456, job_id=f"voice-{label}",
                    duration_seconds=kw.get("duration_seconds", 12),
                    budget_tracker=tracker, job_store=store, dispatch_fn=_dispatch)
                latencies[label] = round((_time.perf_counter() - started) * 1000.0, 2)
                return res

            fast = _run("fast", "quanto spazio libero ho sul disco")
            working = _run("system2", "cercami voli per Bali a novembre")
            clarify = _run("clarify", "cancella tutto dal server")
            empty = _run("empty", "   ")
            boundary = _run("boundary", "ciao",
                            duration_seconds=VOICE_MAX_DURATION_SECONDS)
            try:
                handle_voice_transcript(
                    "vocale lunghissimo", actor_id=123456, job_id="voice-toolong",
                    duration_seconds=VOICE_MAX_DURATION_SECONDS + 60,
                    budget_tracker=tracker, job_store=store, dispatch_fn=_dispatch)
                too_long = {"rejected": False}
            except VoiceError as exc:
                too_long = {"rejected": exc.code == "VOICE_TOO_LONG"}

            usage = tracker.get_job_usage("voice-system2")
            ack_ok = all(latencies[k] < ACK_LATENCY_BUDGET_MS
                         for k in ("fast", "system2", "clarify"))
            ack_no_outcome = all(w not in working["ack"].lower()
                                 for w in ("completato", "fatto", "inviato", "eseguito"))
            jobs = {"fast": store.get_job(fast["job_id"]),
                    "working": store.get_job(working["job_id"]),
                    "clarify": store.get_job(clarify["job_id"])}
            payload = {
                "scope": "isolated_voice_demo",
                "fast_answered": fast["status"] == "answered" and "Spazio libero" in fast["reply"],
                "system2_working_ack": working["status"] == "working" and working["job_id"] in working["ack"],
                "ambiguous_clarification_no_effect": (
                    clarify["status"] == "needs_clarification"
                    and "Non ho eseguito nulla" in clarify["reply"]
                    and jobs["clarify"]["effects_count"] == 0),
                "empty_asks_resend": empty["status"] == "needs_resend" and empty["job_id"] is None,
                "boundary_duration_accepted": boundary["status"] == "working",
                "too_long_rejected": too_long["rejected"],
                "ack_latency_ms": latencies,
                "ack_within_3s": ack_ok,
                "ack_claims_no_outcome": ack_no_outcome,
                "stt_calls_recorded": usage["calls_count"] >= 1,
                "stt_tokens_recorded": usage["total_tokens"] > 0,
                "job_states": {k: v["status"] for k, v in jobs.items()},
                "audio_retention": AUDIO_RETENTION_POLICY,
                "no_consent_from_audio": "approval_token" not in jobs["working"]["metadata"],
                "machine_state": {"job_db": str(base / "jobs.sqlite3"),
                                  "fast_job": fast["job_id"],
                                  "working_job": working["job_id"]},
            }
            if args.as_json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(f"Fast vocale: {fast['reply'][:80]}")
                print(f"Ack System 2: {working['ack'][:100]}")
                print(f"Chiarimento: {clarify['reply'][:100]}")
                print(f"Latenze ack (ms): {latencies} (budget {ACK_LATENCY_BUDGET_MS})")
                print(f"STT job voice-system2: {usage['calls_count']} chiamate, {usage['total_tokens']} token")
                print(f"Retention: {AUDIO_RETENTION_POLICY}")
        return 0
    if args.command == "routing-report":
        from .telemetry import RoutingDecisionStore
        state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
        state_dir = state_home / 'jarvis-hermes'
        db_path = state_dir / 'routing_decisions.sqlite3'
        retention = int(os.environ.get('JARVIS_ROUTING_RETENTION_DAYS', '30'))
        store = RoutingDecisionStore(db_path, retention_days=retention)
        if args.cleanup:
            store.cleanup_old_records()
        report = store.generate_report()
        if args.as_json:
            print(json.dumps(report, indent=2))
        else:
            print(f"=== Jarvis Routing Decisions & Telemetry Report ===")
            print(f"Total decisions: {report['total_decisions']}")
            print(f"Escalation rate: {report['escalation_rate'] * 100:.1f}%")
            print(f"System 1 Latency (ms): p50={report['latency_system_1_p50_ms']}, p95={report['latency_system_1_p95_ms']}")
            print(f"First Message Latency (ms): p50={report['latency_first_msg_p50_ms']}, p95={report['latency_first_msg_p95_ms']}")
            print(f"Total tokens: {report['total_tokens']} | Total cost: ${report['total_cost_usd']:.6f}")
            print("\nPath distribution:")
            for path_name, stats in report['path_distribution'].items():
                print(f"  • {path_name}: {stats['count']} ({stats['percentage']}%)")
            print(f"\nUser corrections ({report['corrections_count']}):")
            for c in report['corrections']:
                print(f"  • [#{c['id']}] Path: {c['path_chosen']} | Request: {c['request']} | Correction: {c['correzione_utente']}")
        return 0
    if args.command == "routing-calibrate":
        from .calibration import (
            load_dataset,
            load_thresholds,
            protected_fast_path_errors,
            run_calibration,
        )
        from .router import RequestRouter
        rows = load_dataset(args.dataset)
        thresholds = load_thresholds(args.thresholds)
        router = RequestRouter(action_class_thresholds=thresholds)
        report = run_calibration(
            rows, router=router, seed=args.seed, thresholds=thresholds,
        )
        # AT13: zero wrong fast paths on protected actions on the full dataset too
        report["protected_fast_path_errors_full_dataset"] = len(protected_fast_path_errors(rows, router))
        if args.as_json:
            print(json.dumps(report, indent=2, ensure_ascii=False))
        else:
            print("=== Jarvis Routing Calibration Report ===")
            print(f"Dataset: {report['dataset_size']} examples (calibration {report['calibration_size']} / test {report['test_size']}, seed {report['calibration_split_seed']})")
            print("\nAccuracy per question (test set):")
            for q, d in report["accuracy_per_question"].items():
                print(f"  • {q}: accuracy={d['accuracy']}, ECE={d['ece']}, n={d['sample_count']}")
            print(f"\nEscalation rate (test): {report['escalation_rate'] * 100:.1f}%")
            print(f"Wrong fast paths on protected actions (test): {report['protected_fast_path_errors_on_test']}")
            print(f"Wrong fast paths on protected actions (full dataset): {report['protected_fast_path_errors_full_dataset']}")
            print("\nChosen per-action-class thresholds:")
            for cls, thr in report["chosen_thresholds"].items():
                print(f"  • {cls}: {thr}")
        return 0
    if args.command == "travel-demo":
        from .travel import TravelError, TravelManager
        mgr = TravelManager()
        try:
            res = mgr.search_flights({
                "origin": args.origin,
                "destination": args.destination,
                "departure_date": args.date,
                "passengers": args.passengers,
            })
            if args.as_json:
                print(json.dumps(res, indent=2, ensure_ascii=False))
            else:
                print(f"Provider: {res['provider']}")
                print(f"Offerte trovate: {res['offers_count']}")
                for off in res["offers"]:
                    print(f"  - Offerta {off['offer_id']}: {off['total_amount']} {off['currency']} (per pax: {off['price_per_passenger']})")
                    for s in off["slices"]:
                        print(f"    Tratta {s['origin']} -> {s['destination']} ({s['duration']}), scali: {s['stops_count']}")
        except TravelError as exc:
            payload = {"error": {"code": exc.code, "message": exc.message, "retryable": exc.retryable}}
            if args.as_json:
                print(json.dumps(payload, indent=2, ensure_ascii=False))
            else:
                print(f"[{exc.code}] {exc.message}")
            return 1
        return 0
    if args.command == "calendar-demo":
        from .calendar import CalendarError, CalendarManager
        mgr = CalendarManager()
        try:
            res = mgr.search_events({"q": args.query or None, "max_results": 5})
            searched = {"events_count": res["events_count"],
                        "provider": res["provider"],
                        "calendar_timezone": res["calendar_timezone"]}
        except CalendarError as exc:
            searched = {"error": {"code": exc.code, "message": exc.message}}
        draft = CalendarManager.draft_event({
            "summary": "Bozza demo",
            "start": "2026-11-01T10:00:00+01:00",
            "end": "2026-11-01T10:30:00+01:00",
        })
        payload = {"scope": "isolated_calendar_demo",
                   "search": searched,
                   "draft": {"status": draft["status"],
                               "created_event": draft["created_event"],
                               "duration": draft["duration"]}}
        if args.as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Ricerca: {searched}")
            print(f"Bozza: {draft['summary']} {draft['start']} -> {draft['end']} (creato: {draft['created_event']})")
        return 0 if "error" not in searched or searched["error"]["code"] == "PROVIDER_NOT_CONFIGURED" else 1
    if args.command == "email-demo":
        from .email import EmailError, EmailManager
        mgr = EmailManager()
        try:
            res = mgr.search_emails({"query": args.query or None, "max_results": 5})
            searched = {"emails_count": res["emails_count"],
                        "provider": res["provider"],
                        "folder": res["folder"]}
        except EmailError as exc:
            searched = {"error": {"code": exc.code, "message": exc.message}}
        draft = EmailManager.draft_reply({
            "original_message": {
                "uid": "1",
                "message_id": "<demo@example.com>",
                "subject": "Demo Email",
                "from": "sender@example.com",
                "to": ["user@example.com"],
                "body_text": "Demo content",
            },
            "reply_body": "Risposta di prova locale.",
        })

        # 3. Dimostrazione invio protetto con consenso monouso in ambiente isolato
        with tempfile.TemporaryDirectory(prefix="jarvis-email-demo-") as demo_dir:
            from .approval import ApprovalStore
            from .email import EmailAuditLog, SmtpEmailSender
            demo_store = ApprovalStore(Path(demo_dir) / "approvals.sqlite3")
            demo_audit = EmailAuditLog(Path(demo_dir) / "audit.sqlite3")
            demo_sender = SmtpEmailSender(user="tester@yahoo.com", password="app_password")
            
            # Simuliamo l'invio SMTP per non contattare server reali durante la demo locale
            demo_sender.send_message = lambda **kwargs: {
                "sent": True,
                "status": "SENT",
                "message_id": kwargs.get("message_id") or "<demo-sent@yahoo.com>",
                "from": "tester@yahoo.com",
                "to": kwargs.get("to"),
                "cc": kwargs.get("cc", []),
                "bcc": kwargs.get("bcc", []),
                "subject": kwargs.get("subject"),
                "attachments_count": len(kwargs.get("attachments", [])),
                "sent_at": "2026-10-10T12:00:00Z",
            }
            email_mgr = EmailManager(sender=demo_sender, approval_store=demo_store, audit_log=demo_audit)
            actor = 12345
            stage = email_mgr.stage_email_send(
                to=["recipient@example.com"],
                subject="Test invio approvato",
                body="Corpo della email verificata.",
                actor_id=actor,
            )
            # Invio con token
            send_res = email_mgr.send_email(
                to=["recipient@example.com"],
                subject="Test invio approvato",
                body="Corpo della email verificata.",
                approval_token=stage["approval_token"],
                actor_id=actor,
                message_id=stage["message_id"],
            )

        payload = {"scope": "isolated_email_demo",
                   "search": searched,
                   "draft": {"status": draft["status"],
                             "sent": draft["sent"],
                             "subject": draft["subject"],
                             "to": draft["to"]},
                   "send_demo": {"status": send_res["status"],
                                 "message_id": send_res["message_id"],
                                 "digest": send_res["digest"]}}
        if args.as_json:
            print(json.dumps(payload, indent=2, ensure_ascii=False))
        else:
            print(f"Ricerca: {searched}")
            print(f"Bozza: {draft['subject']} -> {draft['to']} (inviato: {draft['sent']})")
            print(f"Invio approvato: {send_res['status']} -> {send_res['message_id']}")
        return 0 if "error" not in searched or searched["error"]["code"] == "PROVIDER_NOT_CONFIGURED" else 1
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
