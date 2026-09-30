"""Manager for executing coding tasks via PI Code CLI (pi) in project workspaces.

Features:
1. Resolves projects from:
   - Known aliases defined in ~/script/coding (e.g. sk1, sk2, wa, mp)
   - Registered Jarvis projects
   - Direct directory path or fuzzy search in ~/programmazione
2. Ensures proxy servers (cli-proxy-api and commandcode-proxy) are running in background if needed.
3. Invokes `pi -p -a` (headless, auto-approved) with `--continue` by default for multi-turn chats.
4. If output exceeds message limit (default 3500 chars), saves full markdown output to a dedicated file
   and returns the path and git diff summary.
"""
from __future__ import annotations

import difflib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import tempfile
import threading
import time
from typing import Any
import urllib.parse
import urllib.request

from jarvis_hermes.jobs import JobError, JobStore
from jarvis_hermes.projects import redact_secrets


class PiCodingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


def _confined(path: Path, root: Path) -> Path:
    """Return `path` only if, after resolving symlinks, it is a strict child of `root`.

    pi runs headless with auto-approval (-a), so the working directory is the real
    security boundary: it must be a project under ~/programmazione, never the root
    itself, $HOME, or a symlink that escapes.
    """
    try:
        real = path.resolve()
        real_root = root.resolve()
    except (OSError, RuntimeError):
        raise PiCodingError("FORBIDDEN_PATH", f"Cannot resolve path: {path}")
    if real == real_root or not real.is_relative_to(real_root):
        raise PiCodingError(
            "FORBIDDEN_PATH",
            f"pi_task may only run inside a project under {real_root} (got {real}).")
    return path


def resolve_project_path(project_query: str | None = None) -> tuple[Path, str]:
    """Resolve project directory from aliases, exact paths, or fuzzy match in ~/programmazione."""
    home = Path.home()
    programmazione_dir = home / "programmazione"
    
    # 1. Parse aliases from ~/script/coding if present
    aliases: dict[str, Path] = {}
    coding_script = home / "script" / "coding"
    if coding_script.is_file():
        try:
            content = coding_script.read_text(encoding="utf-8")
            # Extract block CODING_PROJECTS=( ... )
            block_match = re.search(r'CODING_PROJECTS=\((.*?)\)', content, re.DOTALL)
            if block_match:
                matches = re.findall(r'([a-zA-Z0-9_\-]+)\s+["\']?([^"\'\n]+)["\']?', block_match.group(1))
                for key, raw_path in matches:
                    clean_path = raw_path.strip().strip('"').strip("'")
                    expanded = Path(clean_path.replace("$HOME", str(home)))
                    aliases[key.lower()] = expanded
        except Exception:
            pass

    if not project_query or not project_query.strip():
        # Default to the current directory only if it is a project under ~/programmazione
        cwd = Path.cwd()
        try:
            return _confined(cwd, programmazione_dir), cwd.name
        except PiCodingError:
            pass
        default_p = programmazione_dir / "Jarvis-hermes"
        if default_p.is_dir():
            return _confined(default_p, programmazione_dir), "Jarvis-hermes"
        raise PiCodingError("PROJECT_NOT_FOUND", "Specify a project under ~/programmazione.")

    query = project_query.strip()
    query_lower = query.lower()

    # Direct alias match
    if query_lower in aliases and aliases[query_lower].is_dir():
        return _confined(aliases[query_lower], programmazione_dir), query_lower

    # Absolute or home-relative path
    p = Path(os.path.expanduser(query)).resolve()
    if p.is_dir():
        return _confined(p, programmazione_dir), p.name

    # Check directly inside ~/programmazione
    if programmazione_dir.is_dir():
        candidates = [d for d in programmazione_dir.iterdir() if d.is_dir()]
        
        # Exact name match (case-insensitive)
        for cand in candidates:
            if cand.name.lower() == query_lower:
                return _confined(cand, programmazione_dir), cand.name

        # Exact substring or normalized match (e.g. "hermes-jarvis" vs "Jarvis-hermes")
        def normalize(name: str) -> str:
            return re.sub(r'[^a-z0-9]', '', name.lower())

        norm_query = normalize(query)
        for cand in candidates:
            if normalize(cand.name) == norm_query:
                return _confined(cand, programmazione_dir), cand.name

        # Match reversed hyphenated tokens (e.g. "hermes-jarvis" -> ["hermes", "jarvis"])
        query_tokens = set(re.findall(r'[a-z0-9]+', query_lower))
        best_token_matches = []
        for cand in candidates:
            cand_tokens = set(re.findall(r'[a-z0-9]+', cand.name.lower()))
            if query_tokens and query_tokens == cand_tokens:
                return _confined(cand, programmazione_dir), cand.name
            if query_tokens and query_tokens.issubset(cand_tokens):
                best_token_matches.append(cand)

        if len(best_token_matches) == 1:
            return _confined(best_token_matches[0], programmazione_dir), best_token_matches[0].name

        # Fuzzy match with difflib
        names = [cand.name for cand in candidates]
        close = difflib.get_close_matches(query, names, n=3, cutoff=0.4)
        if close:
            matched_dir = programmazione_dir / close[0]
            return _confined(matched_dir, programmazione_dir), close[0]

    raise PiCodingError("PROJECT_NOT_FOUND", f"Could not find project directory matching '{project_query}'")


def send_telegram_notification(text: str) -> bool:
    """Send direct notification to Telegram using Bot API credentials if available."""
    token = os.environ.get("TELEGRAM_BOT_TOKEN")
    chat_id = os.environ.get("TELEGRAM_HOME_CHANNEL") or os.environ.get("TELEGRAM_ALLOWED_USERS")
    if not token or not chat_id:
        # Fallback reading from ~/.hermes/.env
        env_file = Path.home() / ".hermes" / ".env"
        if env_file.is_file():
            try:
                for line in env_file.read_text(encoding="utf-8").splitlines():
                    line = line.strip()
                    if line.startswith("TELEGRAM_BOT_TOKEN="):
                        token = line.split("=", 1)[1].strip().strip('"').strip("'")
                    elif line.startswith("TELEGRAM_HOME_CHANNEL="):
                        chat_id = line.split("=", 1)[1].strip().strip('"').strip("'")
            except Exception:
                pass

    if not token or not chat_id:
        return False

    first_chat_id = chat_id.split(",")[0].strip()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = urllib.parse.urlencode({
        "chat_id": first_chat_id,
        "text": text,
        "parse_mode": "Markdown",
    }).encode("utf-8")

    try:
        req = urllib.request.Request(url, data=payload, headers={"User-Agent": "Jarvis-Hermes/1.0"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status == 200
    except Exception:
        return False


def _extract_summary(full_output: str, max_lines: int = 15) -> str:
    """Extract clean summary from pi output without requiring a full LLM pass."""
    lines = [ln.strip() for ln in full_output.strip().splitlines() if ln.strip()]
    if not lines:
        return ""
    # Look for conclusion or take the last few lines
    return "\n".join(lines[-max_lines:])


def ensure_proxy_servers_running() -> dict[str, str]:
    home = Path.home()
    status = {}

    # 1. cli-proxy-api
    cli_proxy_cmd = shutil.which("cli-proxy-api")
    config_yaml = home / ".cli-proxy-api" / "config.yaml"
    is_cli_running = False
    try:
        pgrep = subprocess.run(["pgrep", "-f", "cli-proxy-api"], capture_output=True, text=True)
        if pgrep.returncode == 0:
            is_cli_running = True
    except Exception:
        pass

    if is_cli_running:
        status["cli-proxy-api"] = "already_running"
    elif cli_proxy_cmd and config_yaml.is_file():
        try:
            subprocess.Popen(
                [cli_proxy_cmd, "-config", str(config_yaml)],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.5)
            status["cli-proxy-api"] = "started"
        except Exception as e:
            status["cli-proxy-api"] = f"failed_to_start: {e}"
    else:
        status["cli-proxy-api"] = "not_found_or_not_configured"

    # 2. commandcode-proxy
    cc_proxy_dir = home / "commandcode-proxy"
    is_cc_running = False
    try:
        pgrep = subprocess.run(["pgrep", "-f", "proxy.mjs"], capture_output=True, text=True)
        if pgrep.returncode == 0:
            is_cc_running = True
    except Exception:
        pass

    if is_cc_running:
        status["commandcode-proxy"] = "already_running"
    elif (cc_proxy_dir / "proxy.mjs").is_file():
        try:
            node_cmd = shutil.which("node") or "node"
            subprocess.Popen(
                [node_cmd, "proxy.mjs"],
                cwd=str(cc_proxy_dir),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                start_new_session=True,
            )
            time.sleep(0.5)
            status["commandcode-proxy"] = "started"
        except Exception as e:
            status["commandcode-proxy"] = f"failed_to_start: {e}"
    else:
        status["commandcode-proxy"] = "not_found"

    return status


def snapshot_repo(target_dir: Path) -> dict[str, Any]:
    """Record the exact working-tree state in a private git ref before pi touches it.

    Uses a temporary index + commit-tree, so the user's branch, index, stash and working
    tree are left completely untouched (no `git stash push`, no commit on the branch).
    Untracked-but-not-ignored files are included. Restore with:
        git restore --source=<ref> --staged --worktree -- .
    """
    def git(*args: str, cwd: Path, env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                              timeout=30, env=env)

    try:
        top = git("rev-parse", "--show-toplevel", cwd=target_dir)
        if top.returncode != 0 or not (top.stdout or "").strip():
            return {"ref": None, "reason": "not_a_git_repository"}
        toplevel = Path(top.stdout.strip())
        with tempfile.TemporaryDirectory() as tmp:
            env = {
                **os.environ,
                "GIT_INDEX_FILE": str(Path(tmp) / "index"),
                "GIT_AUTHOR_NAME": "jarvis-pi", "GIT_AUTHOR_EMAIL": "jarvis-pi@localhost",
                "GIT_COMMITTER_NAME": "jarvis-pi", "GIT_COMMITTER_EMAIL": "jarvis-pi@localhost",
            }
            parent_args: list[str] = []
            head = git("rev-parse", "--verify", "-q", "HEAD", cwd=toplevel)
            if head.returncode == 0:
                parent_args = ["-p", head.stdout.strip()]
                git("read-tree", "HEAD", cwd=toplevel, env=env)
            if git("add", "-A", cwd=toplevel, env=env).returncode != 0:
                return {"ref": None, "reason": "git_add_failed"}
            tree = git("write-tree", cwd=toplevel, env=env)
            if tree.returncode != 0:
                return {"ref": None, "reason": "write_tree_failed"}
            commit = git("commit-tree", tree.stdout.strip(), *parent_args,
                         "-m", "jarvis pi_task pre-run snapshot", cwd=toplevel, env=env)
            if commit.returncode != 0:
                return {"ref": None, "reason": "commit_tree_failed"}
            ref = f"refs/jarvis/pi-snapshots/{int(time.time())}-{os.getpid()}"
            if git("update-ref", ref, commit.stdout.strip(), cwd=toplevel).returncode != 0:
                return {"ref": None, "reason": "update_ref_failed"}
        return {"ref": ref, "restore_hint": f"git restore --source={ref} --staged --worktree -- ."}
    except Exception as exc:  # snapshot is best-effort: never block the task, but report it
        return {"ref": None, "reason": f"snapshot_error: {exc}"}


def _run_pi_process(cmd: list[str], cwd: str, timeout: int,
                    job_store: JobStore | None, job_id: str | None) -> subprocess.CompletedProcess:
    """Run pi. With a JobStore the process gets its own session and is registered so
    job_cancel can kill the whole process group; without one, behave like subprocess.run."""
    if job_store is None or job_id is None:
        return subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)

    job_store.check_not_cancelled(job_id)
    proc = subprocess.Popen(cmd, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            text=True, start_new_session=(os.name != "nt"))
    try:
        pgid = os.getpgid(proc.pid) if os.name != "nt" else None
        job_store.register_process(job_id, proc.pid, pgid)
    except Exception:
        proc.kill()
        proc.wait()
        raise
    try:
        out, err = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        try:
            if os.name != "nt":
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            else:
                proc.kill()
        except (ProcessLookupError, PermissionError):
            pass
        proc.communicate()
        raise
    return subprocess.CompletedProcess(cmd, proc.returncode, out, err)


def run_pi_task(
    prompt: str,
    project_query: str | None = None,
    new_session: bool = False,
    model: str | None = None,
    timeout_seconds: int = 300,
    max_inline_chars: int = 3500,
    async_mode: bool = False,
    notify_telegram: bool = True,
    job_store: JobStore | None = None,
    actor_id: int = 0,
) -> dict[str, Any]:
    """Execute a coding task via pi CLI and return output or file path for large outputs."""
    pi_bin = shutil.which("pi")
    if not pi_bin:
        for candidate in [str(Path.home() / ".local" / "bin" / "pi"), str(Path.home() / ".pnpm-global" / "pi")]:
            if Path(candidate).is_file():
                pi_bin = candidate
                break
    if not pi_bin:
        raise PiCodingError("PI_NOT_INSTALLED", "'pi' CLI executable was not found in PATH")

    # 1. Resolve project directory
    target_dir, resolved_name = resolve_project_path(project_query)

    # 2. Ensure background model proxies are alive
    proxy_status = ensure_proxy_servers_running()

    # 2b. Pre-run snapshot (recoverable state) and job tracking (cancellable, survives restarts)
    snapshot = snapshot_repo(target_dir)
    job_id: str | None = None
    if job_store is not None:
        job = job_store.create_job(
            actor_id, f"pi_task:{resolved_name}", prompt[:200],
            metadata={"project_path": str(target_dir), "async": async_mode,
                      "snapshot_ref": snapshot.get("ref")})
        job_id = job["job_id"]
        job_store.update_status(job_id, "running", step="pi started")

    def _finish_job(status: str, warning: str | None = None, result: dict[str, Any] | None = None) -> None:
        if job_store is None or job_id is None:
            return
        try:
            job_store.update_status(job_id, status, warning=warning, result=result)
        except (JobError, sqlite3.Error):
            pass  # already terminal (e.g. cancelled by the owner) or job DB unavailable

    # 3. Assemble command: pi -p -a [--continue] [--model <model>] <prompt>
    cmd = [pi_bin, "-p", "-a"]
    if not new_session:
        cmd.append("-c")
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    def _execute_sync() -> dict[str, Any]:
        start_time = time.time()
        try:
            res = _run_pi_process(cmd, str(target_dir), timeout_seconds, job_store, job_id)
        except subprocess.TimeoutExpired:
            _finish_job("failed", warning="timeout")
            raise PiCodingError("TIMEOUT", f"pi task timed out after {timeout_seconds} seconds")
        except JobError as e:
            raise PiCodingError("JOB_CANCELLED" if e.code == "JOB_CANCELLED" else "JOB_ERROR", str(e))
        except Exception as e:
            _finish_job("failed", warning=f"execution failed: {e}")
            raise PiCodingError("EXECUTION_FAILED", f"Failed to execute pi: {e}")

        elapsed = round(time.time() - start_time, 2)
        stdout = res.stdout or ""
        stderr = res.stderr or ""
        full_output = stdout if stdout else stderr

        # Check git diff stat if target_dir is a git repository
        git_diff_stat = ""
        try:
            diff_res = subprocess.run(
                ["git", "diff", "--stat"],
                cwd=str(target_dir),
                capture_output=True,
                text=True,
                timeout=10,
            )
            if diff_res.returncode == 0 and diff_res.stdout.strip():
                git_diff_stat = diff_res.stdout.strip()
        except Exception:
            pass

        clean_output = redact_secrets(full_output.strip())
        is_long = len(clean_output) > max_inline_chars
        output_file_path: str | None = None
        summary = _extract_summary(clean_output)

        if is_long:
            state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
            output_dir = state_home / 'jarvis-hermes' / 'pi-outputs'
            output_dir.mkdir(parents=True, exist_ok=True)
            ts = int(time.time())
            file_path = output_dir / f"pi_{resolved_name}_{ts}.md"

            file_content = (
                f"# Pi Task Output - {resolved_name}\n\n"
                f"- **Timestamp:** {time.strftime('%Y-%m-%d %H:%M:%S')}\n"
                f"- **Directory:** `{target_dir}`\n"
                f"- **Prompt:** {prompt}\n\n"
                f"## Execution Log\n\n```text\n{clean_output}\n```\n"
            )
            if git_diff_stat:
                file_content += f"\n## Git Changes\n\n```text\n{git_diff_stat}\n```\n"

            file_path.write_text(file_content, encoding="utf-8")
            output_file_path = str(file_path)

        result_payload = {
            "ok": res.returncode == 0,
            "exit_code": res.returncode,
            "project_name": resolved_name,
            "project_path": str(target_dir),
            "elapsed_seconds": elapsed,
            "proxies": proxy_status,
            "is_long_output": is_long,
            "output_file": output_file_path,
            "summary": summary,
            "output": clean_output if not is_long else summary,
            "git_diff_stat": git_diff_stat if git_diff_stat else None,
            "snapshot": snapshot,
            "job_id": job_id,
            "message": (
                f"Task completato in {elapsed}s su '{resolved_name}'."
                if res.returncode == 0 else
                f"Task terminato con codice {res.returncode} in {elapsed}s."
            )
        }

        _finish_job("succeeded" if res.returncode == 0 else "failed",
                    result={"exit_code": res.returncode, "output_file": output_file_path})

        if notify_telegram:
            status_icon = "✅" if res.returncode == 0 else "❌"
            tg_msg = (
                f"{status_icon} *PI Task Completato* su `{resolved_name}` in {elapsed}s\n\n"
                f"*Prompt:* _{prompt[:120]}_\n\n"
            )
            if git_diff_stat:
                tg_msg += f"```text\n{git_diff_stat}\n```\n\n"
            if summary:
                tg_msg += f"*Conclusioni:*\n```text\n{summary[-800:]}\n```\n"
            if output_file_path:
                tg_msg += f"\n📄 Log completo: `{output_file_path}`"
            send_telegram_notification(tg_msg)

        return result_payload

    if async_mode:
        def _async_runner() -> None:
            try:
                _execute_sync()
            except PiCodingError as exc:
                _finish_job("failed", warning=f"{exc.code}: {exc}")
                if notify_telegram:
                    send_telegram_notification(f"❌ *PI Task fallito* su `{resolved_name}`: {exc}")

        job_thread = threading.Thread(target=_async_runner, daemon=True)
        job_thread.start()
        if notify_telegram:
            send_telegram_notification(
                f"🚀 *PI Task avviato in background*\n"
                f"- **Progetto:** `{resolved_name}`\n"
                f"- **Task:** _{prompt[:150]}_\n\n"
                f"Riceverai una notifica non appena completato."
            )
        return {
            "ok": True,
            "async": True,
            "status": "running_in_background",
            "project_name": resolved_name,
            "project_path": str(target_dir),
            "job_id": job_id,
            "snapshot": snapshot,
            "message": f"Task avviato in background su '{resolved_name}'. Riceverai una notifica al completamento.",
        }

    return _execute_sync()

