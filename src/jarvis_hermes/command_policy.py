"""Advanced administrative command execution policy and enforcement.

Enforces:
1. Automatic execution for read-only commands without side effects.
2. File modifications or recoverable writes use checkpoints when applicable.
3. System changes, external effects, privileged actions (sudo, systemctl, service restart),
   and destructive actions (rm, drop) require single-use approval bound to the exact command digest.
4. Unparseable or complex compositions (pipes, evals, subshells) require confirmation of the exact command.
5. LLM classification is never trusted to grant permissions (server-side deterministic parser enforces policy).
6. Protected targets (policy files, secret stores, credentials, shadow, etc.) are strictly immutable
   and blocked unconditionally.
"""
from __future__ import annotations

import difflib
import hashlib
import json
import os
import re
import shlex
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jarvis_hermes.approval import ApprovalError, ApprovalStore
from jarvis_hermes.checkpoint import CheckpointError, CheckpointManager
from jarvis_hermes.projects import redact_secrets


class CommandPolicyError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


@dataclass
class CommandClassification:
    category: str  # READONLY, WRITE_RECOVERABLE, PRIVILEGED_MAINTENANCE, EXTERNAL_EFFECT, DESTRUCTIVE, UNPARSEABLE_COMPLEX
    requires_approval: bool
    is_unparseable_or_complex: bool
    command_digest: str
    tokens: list[str]
    reason: str
    target_file: str | None = None


# Absolute or relative sensitive targets that cannot be read/modified/overwritten by run_command
IMMUTABLE_TARGET_PATTERNS = [
    re.compile(r'(?i)(\.env|credentials|secret|shadow|sudoers|\.ssh|id_rsa|id_ed25519)'),
    re.compile(r'(?i)(\.hermes/config\.yaml|projects\.json|approvals\.sqlite3|jobs\.sqlite3|checkpoints\.sqlite3|policy\.json)'),
]

READONLY_COMMANDS = {
    'ls', 'dir', 'cat', 'head', 'tail', 'grep', 'rg', 'find', 'wc', 'file',
    'echo', 'printf', 'uname', 'hostname', 'uptime', 'whoami', 'id', 'date',
    'df', 'du', 'free', 'top', 'ps', 'systemctl status', 'service status',
    'journalctl', 'dmesg', 'git status', 'git log', 'git diff', 'git branch',
    'docker ps', 'docker status', 'docker inspect', 'docker logs',
    'pwd', 'which', 'whereis', 'env', 'printenv'
}

PRIVILEGED_MAINTENANCE_COMMANDS = {
    'systemctl', 'service', 'docker', 'podman', 'kill', 'pkill', 'killall',
    'sudo', 'su', 'chown', 'chmod', 'apt', 'apt-get', 'dnf', 'yum', 'pacman',
    'apk', 'pip', 'npm', 'uv', 'cargo'
}

DESTRUCTIVE_COMMANDS = {
    'rm', 'del', 'erase', 'mkfs', 'dd', 'wipe', 'shred', 'dropdb', 'truncate'
}

EXTERNAL_EFFECT_COMMANDS = {
    'curl', 'wget', 'ssh', 'scp', 'rsync', 'ftp', 'sftp', 'nc', 'netcat', 'telnet'
}


class CommandPolicyManager:
    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        state_dir: Path | None = None,
        job_store: Any | None = None,
    ):
        self.approval_store = approval_store
        self.checkpoint_manager = checkpoint_manager
        self.job_store = job_store
        if state_dir is None:
            state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
            self.state_dir = state_home / 'jarvis-hermes'
        else:
            self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def _check_immutable_targets(self, command: str) -> None:
        """Reject any command that mentions or targets immutable system/policy/secret files."""
        for pat in IMMUTABLE_TARGET_PATTERNS:
            if pat.search(command):
                raise CommandPolicyError(
                    'PROTECTED_TARGET_DENIED',
                    'Command references protected secret stores, policy configuration, or system credentials.'
                )

    def classify_command(self, command: str) -> CommandClassification:
        if not command or not command.strip():
            raise CommandPolicyError('INVALID_ARGUMENT', 'Command cannot be empty.')

        clean_cmd = command.strip()
        command_digest = hashlib.sha256(clean_cmd.encode('utf-8')).hexdigest()

        # Check for protected immutable paths first
        self._check_immutable_targets(clean_cmd)

        # Parse command tokens safely using shlex
        try:
            lexer = shlex.shlex(clean_cmd, posix=True, punctuation_chars=True)
            lexer.whitespace_split = True
            tokens = list(lexer)
        except Exception:
            # Cannot be safely parsed
            return CommandClassification(
                category='UNPARSEABLE_COMPLEX',
                requires_approval=True,
                is_unparseable_or_complex=True,
                command_digest=command_digest,
                tokens=[],
                reason='Command syntax cannot be parsed safely by shlex.'
            )

        if not tokens:
            raise CommandPolicyError('INVALID_ARGUMENT', 'Parsed command is empty.')

        # Check for complex shell operators (pipes, subshells, chaining, eval, redirects)
        # Any composition with redirects, subshells, semicolons, ||, &&, or tricky quoting
        complex_operators = {';', '&&', '||', '|', '&', '`', '$(', '<', '>', '>>', 'eval', 'exec'}
        has_complex_composition = any(op in clean_cmd for op in ['`', '$(']) or any(t in complex_operators for t in tokens)

        # Detect nested subshell execution like bash -c "..." or sh -c "..."
        is_subshell_wrapper = tokens[0] in {'bash', 'sh', 'zsh', 'ksh'} and ('-c' in tokens)

        # Check for simple output redirection (e.g. echo "content" > file.txt or tee file.txt)
        # If writing to a project/local file without other complex operators (like ;, &&, ||), it is WRITE_RECOVERABLE with checkpoint!
        other_complex = {';', '&&', '||', '|', '&', '`', '$(', 'eval', 'exec'}
        has_other_complex = any(op in clean_cmd for op in ['`', '$(']) or any(t in other_complex for t in tokens)

        if not has_other_complex and ('>' in tokens or '>>' in tokens) and len(tokens) >= 3:
            # Simple redirect like echo foo > bar
            # Identify target file
            redir_idx = tokens.index('>') if '>' in tokens else tokens.index('>>')
            if redir_idx + 1 < len(tokens):
                target_filename = tokens[redir_idx + 1]
                # If command before redirect is safe like echo/printf
                prefix_cmd = tokens[0]
                if prefix_cmd in {'echo', 'printf', 'cat'}:
                    return CommandClassification(
                        category='WRITE_RECOVERABLE',
                        requires_approval=False,
                        is_unparseable_or_complex=False,
                        command_digest=command_digest,
                        tokens=tokens,
                        reason=f'File modification ({target_filename}) is recoverable with automatic checkpoint.',
                        target_file=target_filename,
                    )

        if has_complex_composition or is_subshell_wrapper:
            # Non-trivial shell composition: must present exact command for confirmation!
            return CommandClassification(
                category='UNPARSEABLE_COMPLEX',
                requires_approval=True,
                is_unparseable_or_complex=True,
                command_digest=command_digest,
                tokens=tokens,
                reason='Complex or composite shell command requires exact confirmation.'
            )

        cmd_name = Path(tokens[0]).name

        # Check destructive commands
        if cmd_name in DESTRUCTIVE_COMMANDS:
            return CommandClassification(
                category='DESTRUCTIVE',
                requires_approval=True,
                is_unparseable_or_complex=False,
                command_digest=command_digest,
                tokens=tokens,
                reason=f'Destructive action ({cmd_name}) requires owner approval.'
            )

        # Check privileged maintenance or service modification commands
        if cmd_name in PRIVILEGED_MAINTENANCE_COMMANDS:
            # Check if this is a readonly sub-operation (e.g. systemctl status, docker ps)
            if len(tokens) > 1 and tokens[1] in {'status', 'is-active', 'is-enabled', 'ps', 'inspect', 'logs'}:
                return CommandClassification(
                    category='READONLY',
                    requires_approval=False,
                    is_unparseable_or_complex=False,
                    command_digest=command_digest,
                    tokens=tokens,
                    reason=f'Inspection command ({cmd_name} {tokens[1]}) is read-only.'
                )
            return CommandClassification(
                category='PRIVILEGED_MAINTENANCE',
                requires_approval=True,
                is_unparseable_or_complex=False,
                command_digest=command_digest,
                tokens=tokens,
                reason=f'Privileged maintenance command ({cmd_name}) requires owner approval.'
            )

        # Check external effect commands
        if cmd_name in EXTERNAL_EFFECT_COMMANDS:
            return CommandClassification(
                category='EXTERNAL_EFFECT',
                requires_approval=True,
                is_unparseable_or_complex=False,
                command_digest=command_digest,
                tokens=tokens,
                reason=f'External networking command ({cmd_name}) requires owner approval.'
            )

        # Check readonly commands
        if cmd_name in READONLY_COMMANDS:
            return CommandClassification(
                category='READONLY',
                requires_approval=False,
                is_unparseable_or_complex=False,
                command_digest=command_digest,
                tokens=tokens,
                reason=f'Safe read-only command ({cmd_name}).'
            )

        # Any unrecognized / new command without explicit whitelist
        # If it's a simple benign binary execution without redirection or privileges, default to confirmation
        return CommandClassification(
            category='PRIVILEGED_MAINTENANCE',
            requires_approval=True,
            is_unparseable_or_complex=False,
            command_digest=command_digest,
            tokens=tokens,
            reason=f'Unclassified command ({cmd_name}) requires confirmation before execution.'
        )

    def run_command(
        self,
        command: str,
        approval_token: str | None = None,
        actor_id: int | None = None,
        job_id: str | None = None,
        timeout_seconds: int = 60,
        cwd: str | Path | None = None,
    ) -> dict[str, Any]:
        """Execute command following policy rules."""
        classification = self.classify_command(command)

        if classification.requires_approval:
            if not approval_token or actor_id is None:
                raise CommandPolicyError(
                    'APPROVAL_REQUIRED',
                    f"Command requires confirmation ({classification.category}): {classification.reason}"
                )

            if not self.approval_store:
                raise CommandPolicyError('APPROVAL_UNAVAILABLE', 'Approval store not configured.')

            target = f"run_command:{classification.category}"
            arguments = {'command': command, 'digest': classification.command_digest}

            try:
                decision = self.approval_store.decide(
                    token=approval_token,
                    actor_id=actor_id,
                    target=target,
                    arguments=arguments,
                    approve=True
                )
            except ApprovalError as exc:
                raise CommandPolicyError(exc.code, f"Approval check failed: {exc.code}")

            if decision.get('status') != 'executed':
                raise CommandPolicyError('APPROVAL_DENIED', 'Command execution approval was not executed.')

        # Execute command safely
        start_time = time.time()
        exec_cwd = Path(cwd).resolve() if cwd else Path.cwd()

        if job_id and self.job_store:
            self.job_store.check_not_cancelled(job_id)

        # If WRITE_RECOVERABLE and checkpoint_manager is configured, snapshot the target file before running
        checkpoint_id = None
        if classification.category == 'WRITE_RECOVERABLE' and classification.target_file and self.checkpoint_manager:
            target_path = (exec_cwd / classification.target_file).resolve()
            rel_name = classification.target_file
            jid = job_id or f"job-cmd-{int(start_time)}"
            cp_res = self.checkpoint_manager.create_checkpoint(
                job_id=jid,
                file_path=target_path,
                project_id="local_command",
                relative_path=rel_name,
            )
            checkpoint_id = cp_res['checkpoint_id']

        try:
            # We execute with shell=True if unparseable/complex or if redirected, or run token list directly if simple
            if classification.is_unparseable_or_complex or classification.category == 'WRITE_RECOVERABLE':
                popen_args: dict[str, Any] = {
                    'cwd': exec_cwd,
                    'stdout': subprocess.PIPE,
                    'stderr': subprocess.PIPE,
                    'text': True,
                    'shell': True,
                }
                cmd_input = command
            else:
                popen_args = {
                    'cwd': exec_cwd,
                    'stdout': subprocess.PIPE,
                    'stderr': subprocess.PIPE,
                    'text': True,
                    'shell': False,
                }
                cmd_input = classification.tokens

            if os.name != 'nt':
                popen_args['start_new_session'] = True

            proc = subprocess.Popen(
                cmd_input,
                **popen_args
            )

            # Register PID/PGID with job if job_id provided
            if job_id and self.job_store:
                pgid = None
                if os.name != 'nt':
                    try:
                        pgid = os.getpgid(proc.pid)
                    except OSError:
                        pgid = proc.pid
                try:
                    self.job_store.register_process(job_id=job_id, pid=proc.pid, pgid=pgid)
                except Exception:
                    # Job was already cancelled: terminate immediately
                    proc.kill()
                    proc.wait()
                    raise

            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_data, stderr_data = proc.communicate()
                elapsed = round(time.time() - start_time, 3)
                return {
                    'exit_code': -1,
                    'output': 'Command execution timed out.',
                    'elapsed_seconds': elapsed,
                    'category': classification.category,
                    'requires_approval': classification.requires_approval,
                    'checkpoint_id': checkpoint_id,
                    'command': command,
                    'digest': classification.command_digest,
                }

            elapsed = round(time.time() - start_time, 3)
            raw_output = (stdout_data or '') + (("\n" + stderr_data) if stderr_data else "")
            redacted_output = redact_secrets(raw_output)

            return {
                'exit_code': proc.returncode,
                'output': redacted_output,
                'elapsed_seconds': elapsed,
                'category': classification.category,
                'requires_approval': classification.requires_approval,
                'checkpoint_id': checkpoint_id,
                'command': command,
                'digest': classification.command_digest,
            }
        except subprocess.TimeoutExpired:
            elapsed = round(time.time() - start_time, 3)
            return {
                'exit_code': -1,
                'output': 'Command execution timed out.',
                'elapsed_seconds': elapsed,
                'category': classification.category,
                'requires_approval': classification.requires_approval,
                'command': command,
                'digest': classification.command_digest,
            }
        except Exception as exc:
            raise CommandPolicyError('EXECUTION_FAILED', f'Failed to execute command: {exc}')
