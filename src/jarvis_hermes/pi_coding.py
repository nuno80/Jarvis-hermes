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
import subprocess
import time
from typing import Any

from jarvis_hermes.projects import redact_secrets


class PiCodingError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


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
        # Default to current directory if valid, otherwise Jarvis-hermes
        cwd = Path.cwd()
        if cwd != home and programmazione_dir in cwd.parents or cwd == programmazione_dir:
            return cwd, cwd.name
        default_p = programmazione_dir / "Jarvis-hermes"
        if default_p.is_dir():
            return default_p, "Jarvis-hermes"
        return programmazione_dir, "programmazione"

    query = project_query.strip()
    query_lower = query.lower()

    # Direct alias match
    if query_lower in aliases and aliases[query_lower].is_dir():
        return aliases[query_lower], query_lower

    # Absolute or home-relative path
    p = Path(os.path.expanduser(query)).resolve()
    if p.is_dir():
        # Security check: must be inside home and not sensitive
        if home not in p.parents and p != home:
            raise PiCodingError("FORBIDDEN_PATH", f"Path must be within user directory: {p}")
        sensitive = [".ssh", ".aws", ".gnupg", ".local/state/jarvis-hermes", ".hermes"]
        for s in sensitive:
            if str(home / s) in str(p):
                raise PiCodingError("SENSITIVE_DIRECTORY", f"Access to sensitive directory blocked: {s}")
        return p, p.name

    # Check directly inside ~/programmazione
    if programmazione_dir.is_dir():
        candidates = [d for d in programmazione_dir.iterdir() if d.is_dir()]
        
        # Exact name match (case-insensitive)
        for cand in candidates:
            if cand.name.lower() == query_lower:
                return cand, cand.name

        # Exact substring or normalized match (e.g. "hermes-jarvis" vs "Jarvis-hermes")
        def normalize(name: str) -> str:
            return re.sub(r'[^a-z0-9]', '', name.lower())

        norm_query = normalize(query)
        for cand in candidates:
            if normalize(cand.name) == norm_query:
                return cand, cand.name

        # Match reversed hyphenated tokens (e.g. "hermes-jarvis" -> ["hermes", "jarvis"])
        query_tokens = set(re.findall(r'[a-z0-9]+', query_lower))
        best_token_matches = []
        for cand in candidates:
            cand_tokens = set(re.findall(r'[a-z0-9]+', cand.name.lower()))
            if query_tokens and query_tokens == cand_tokens:
                return cand, cand.name
            if query_tokens and query_tokens.issubset(cand_tokens):
                best_token_matches.append(cand)

        if len(best_token_matches) == 1:
            return best_token_matches[0], best_token_matches[0].name

        # Fuzzy match with difflib
        names = [cand.name for cand in candidates]
        close = difflib.get_close_matches(query, names, n=3, cutoff=0.4)
        if close:
            matched_dir = programmazione_dir / close[0]
            return matched_dir, close[0]

    raise PiCodingError("PROJECT_NOT_FOUND", f"Could not find project directory matching '{project_query}'")


def ensure_proxy_servers_running() -> dict[str, str]:
    """Ensure cli-proxy-api (8317) and commandcode-proxy (3050) are running in background."""
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


def run_pi_task(
    prompt: str,
    project_query: str | None = None,
    new_session: bool = False,
    model: str | None = None,
    timeout_seconds: int = 300,
    max_inline_chars: int = 3500,
) -> dict[str, Any]:
    """Execute a coding task via pi CLI and return output or file path for large outputs."""
    pi_bin = shutil.which("pi")
    if not pi_bin:
        for candidate in ["/home/nuno/.local/bin/pi", "/home/nuno/.pnpm-global/pi"]:
            if Path(candidate).is_file():
                pi_bin = candidate
                break
    if not pi_bin:
        raise PiCodingError("PI_NOT_INSTALLED", "'pi' CLI executable was not found in PATH")

    # 1. Resolve project directory
    target_dir, resolved_name = resolve_project_path(project_query)

    # 2. Ensure background model proxies are alive
    proxy_status = ensure_proxy_servers_running()

    # 3. Assemble command: pi -p -a [--continue] [--model <model>] <prompt>
    cmd = [pi_bin, "-p", "-a"]
    if not new_session:
        cmd.append("-c")
    if model:
        cmd.extend(["--model", model])
    cmd.append(prompt)

    # 4. Execute pi in target_dir
    start_time = time.time()
    try:
        res = subprocess.run(
            cmd,
            cwd=str(target_dir),
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        raise PiCodingError("TIMEOUT", f"pi task timed out after {timeout_seconds} seconds")
    except Exception as e:
        raise PiCodingError("EXECUTION_FAILED", f"Failed to execute pi: {e}")

    elapsed = round(time.time() - start_time, 2)
    stdout = res.stdout or ""
    stderr = res.stderr or ""
    full_output = stdout if stdout else stderr

    # 5. Check git diff stat if target_dir is a git repository
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

    # 6. Check length and handle large output file
    clean_output = redact_secrets(full_output.strip())
    is_long = len(clean_output) > max_inline_chars
    output_file_path: str | None = None

    if is_long:
        # Save output to a markdown file
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

    return {
        "ok": res.returncode == 0,
        "exit_code": res.returncode,
        "project_name": resolved_name,
        "project_path": str(target_dir),
        "elapsed_seconds": elapsed,
        "proxies": proxy_status,
        "is_long_output": is_long,
        "output_file": output_file_path,
        "output": clean_output if not is_long else None,
        "git_diff_stat": git_diff_stat if git_diff_stat else None,
        "message": (
            f"Task completato in {elapsed}s sul progetto '{resolved_name}'."
            if res.returncode == 0 else
            f"Task terminato con codice {res.returncode} in {elapsed}s."
        )
    }
