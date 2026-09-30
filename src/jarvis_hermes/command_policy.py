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
    re.compile(r'(?i)(/etc/passwd|/etc/master\.passwd|/etc/security|/etc/pam\.d)'),
    re.compile(r'(?i)(\.aws/credentials|\.kube/config|\.gnupg)'),
    re.compile(r'(?i)(/proc/[^/\s]+/environ|\.npmrc|\.netrc|\.pypirc|\.git-credentials|\.docker/config\.json|\.config/gh/)'),
]

READONLY_COMMANDS = {
    'ls', 'dir', 'cat', 'head', 'tail', 'grep', 'rg', 'find', 'wc', 'file',
    'echo', 'printf', 'uname', 'hostname', 'uptime', 'whoami', 'id', 'date',
    'df', 'du', 'free', 'top', 'ps', 'systemctl status', 'service status',
    'journalctl', 'dmesg', 'git status', 'git log', 'git diff', 'git branch',
    'docker ps', 'docker status', 'docker inspect', 'docker logs',
    'pwd', 'which', 'whereis',
}  # env/printenv removed: they dump every secret in the process environment

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

# Options that turn an otherwise read-only program into one that deletes, writes or executes.
# Long/single-dash options match exactly or as `--opt=value`; short clusters are matched per letter.
DANGEROUS_READONLY_OPTIONS: dict[str, frozenset[str]] = {
    'find': frozenset({'-delete', '-exec', '-execdir', '-ok', '-okdir',
                       '-fprint', '-fprint0', '-fprintf', '-fls'}),
    'rg': frozenset({'--pre', '--hostname-bin'}),
    'file': frozenset({'--compile'}),
    'journalctl': frozenset({'--vacuum-size', '--vacuum-time', '--vacuum-files',
                             '--rotate', '--flush', '--sync', '--relinquish-var'}),
    'dmesg': frozenset({'--clear', '--read-clear'}),
    'date': frozenset({'--set'}),
}
DANGEROUS_READONLY_SHORT_FLAGS: dict[str, frozenset[str]] = {
    'file': frozenset('C'),
    'dmesg': frozenset('cC'),
    'date': frozenset('s'),
}
# hostname changes the machine name when given a positional argument or -F/-b.
HOSTNAME_SAFE_FLAGS = frozenset({'-f', '-s', '-i', '-I', '-a', '-d', '-A', '-y', '--fqdn', '--short',
                                 '--ip-address', '--all-ip-addresses', '--domain', '--alias',
                                 '--long', '--all-fqdns', '--yp', '--nis'})
# Sub-commands that are inspection-only, but only for programs where that is really true.
INSPECTION_SUBCOMMAND_PROGRAMS = {'systemctl', 'service', 'docker', 'podman'}


class CommandPolicyManager:
    def __init__(
        self,
        approval_store: ApprovalStore | None = None,
        checkpoint_manager: CheckpointManager | None = None,
        state_dir: Path | None = None,
        job_store: Any | None = None,
        project_registry: Any | None = None,
    ):
        self.approval_store = approval_store
        self.checkpoint_manager = checkpoint_manager
        self.job_store = job_store
        self.project_registry = project_registry
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

    def _is_within_registered_root(self, path: Path) -> bool:
        """Check if a path is strictly inside one of the registered project roots (no symlinks escaping root)."""
        if not self.project_registry or not hasattr(self.project_registry, 'get_registered_roots'):
            return False
        roots = self.project_registry.get_registered_roots()
        if not roots:
            return False
        try:
            # Check for symlink / junction along the path
            resolved = path.resolve()
            for r in roots:
                resolved_root = r.resolve()
                if resolved.is_relative_to(resolved_root):
                    # Check each parent component to ensure no symlink / junction escapes
                    curr = path
                    has_symlink = False
                    while True:
                        if curr.is_symlink() or (hasattr(curr, 'is_junction') and curr.is_junction()):
                            has_symlink = True
                            break
                        if curr == curr.parent or curr == resolved_root:
                            break
                        curr = curr.parent
                    if not has_symlink:
                        return True
        except Exception:
            return False
        return False

    @staticmethod
    def _is_recursive_reader(tokens: list[str]) -> bool:
        """Tools that walk directory trees and can reach secrets never named in the command."""
        name = Path(tokens[0]).name
        if name in {'rg', 'find'}:
            return True
        if name == 'grep':
            return any(t in {'--recursive', '--dereference-recursive'}
                       or (t.startswith('-') and not t.startswith('--') and ('r' in t or 'R' in t))
                       for t in tokens[1:])
        return False

    def _check_resolved_targets(self, tokens: list[str], cwd: str | Path | None) -> bool:
        """Path checks for auto-executed read-only commands.

        The regex denylist only sees the raw string, so quoting (`.e'n'v`) defeats it.
        1. Every path-like argument is resolved (shlex already removed quotes, symlinks are
           followed) and checked against the protected patterns: hard block for all readers.
        2. Recursive readers (grep -r, rg, find) must start inside a registered project root,
           because `grep -r PRIVATE /home/me` reaches ~/.ssh without ever naming it.
        Returns False -> caller asks for approval. Plain reads elsewhere stay automatic.
        """
        base = Path(cwd).resolve() if cwd else Path.cwd().resolve()
        recursive = self._is_recursive_reader(tokens)
        confined = self._is_within_registered_root(base) if recursive else True
        for tok in tokens[1:]:
            cand = tok.split('=', 1)[1] if tok.startswith('-') and '=' in tok else tok
            if not cand or (tok.startswith('-') and '=' not in tok):
                continue
            path = Path(os.path.expanduser(cand))
            if not path.is_absolute():
                path = base / path
            try:
                real = path.resolve()
            except (OSError, RuntimeError):
                return False
            for pat in IMMUTABLE_TARGET_PATTERNS:
                if pat.search(str(real)):
                    raise CommandPolicyError(
                        'PROTECTED_TARGET_DENIED',
                        'Command resolves to protected secret stores, policy configuration, or system credentials.'
                    )
            if recursive and not self._is_within_registered_root(real):
                looks_like_path = (cand.startswith(('/', '~', '.', '\\')) or '/' in cand or '\\' in cand\n                                  or bool(re.match(r'^[A-Za-z]:[\\/]', cand)) or real.exists())
                if looks_like_path:
                    confined = False
        return confined

    @staticmethod
    def _has_dangerous_option(tokens: list[str]) -> str | None:
        """Return the offending option if a 'read-only' program is asked to write/delete/execute."""
        name = Path(tokens[0]).name
        args = tokens[1:]
        if name == 'hostname':
            for t in args:
                if t not in HOSTNAME_SAFE_FLAGS:
                    return t
            return None
        exact = DANGEROUS_READONLY_OPTIONS.get(name, frozenset())
        letters = DANGEROUS_READONLY_SHORT_FLAGS.get(name, frozenset())
        for t in args:
            if any(t == o or t.startswith(o + '=') for o in exact):
                return t
            if letters and t.startswith('-') and not t.startswith('--') and any(c in letters for c in t[1:]):
                return t
        return None

    def _readonly(self, tokens: list[str], digest: str, reason: str,
                  cwd: str | Path | None) -> CommandClassification:
        bad = self._has_dangerous_option(tokens)
        if bad is not None:
            return CommandClassification(
                'PRIVILEGED_MAINTENANCE', True, False, digest, tokens,
                f'Option {bad!r} makes {Path(tokens[0]).name} write, delete or execute: owner approval required.')
        if self._check_resolved_targets(tokens, cwd):
            return CommandClassification('READONLY', False, False, digest, tokens, reason)
        return CommandClassification(
            'PRIVILEGED_MAINTENANCE', True, False, digest, tokens,
            'Recursive search outside registered project roots (or with an unresolved path) requires owner approval.')

    def classify_command(self, command: str, cwd: str | Path | None = None) -> CommandClassification:
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
                    # Confinement check (D04, D14, C3):
                    # Automatic WRITE_RECOVERABLE is ONLY allowed if cwd and target are inside a registered root,
                    # without symlinks/junctions. Outside registered roots -> requires exact command confirmation.
                    exec_cwd = Path(cwd).resolve() if cwd else Path.cwd().resolve()
                    target_path = Path(target_filename)
                    if not target_path.is_absolute():
                        target_path = exec_cwd / target_path

                    # Check if target is a symlink or has symlink in path
                    is_symlink = target_path.is_symlink() or (hasattr(target_path, 'is_junction') and target_path.is_junction())
                    inside_root = self._is_within_registered_root(target_path) and self._is_within_registered_root(exec_cwd)

                    if inside_root and not is_symlink:
                        return CommandClassification(
                            category='WRITE_RECOVERABLE',
                            requires_approval=False,
                            is_unparseable_or_complex=False,
                            command_digest=command_digest,
                            tokens=tokens,
                            reason=f'File modification ({target_filename}) is recoverable with automatic checkpoint inside registered project root.',
                            target_file=target_filename,
                        )
                    else:
                        # Outside registered root or symlink: requires exact command confirmation!
                        return CommandClassification(
                            category='UNPARSEABLE_COMPLEX',
                            requires_approval=True,
                            is_unparseable_or_complex=True,
                            command_digest=command_digest,
                            tokens=tokens,
                            reason=f'File redirect ({target_filename}) outside registered project root or via symlink requires exact confirmation.',
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
            if (cmd_name in INSPECTION_SUBCOMMAND_PROGRAMS and len(tokens) > 1
                    and tokens[1] in {'status', 'is-active', 'is-enabled', 'ps', 'inspect', 'logs'}):
                return self._readonly(tokens, command_digest,
                                      f'Inspection command ({cmd_name} {tokens[1]}) is read-only.', cwd)
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
            return self._readonly(tokens, command_digest, f'Safe read-only command ({cmd_name}).', cwd)

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
        classification = self.classify_command(command, cwd=cwd)

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
            target_file_obj = None
            if classification.category == 'WRITE_RECOVERABLE' and classification.target_file:
                # Automatic recoverable write: NEVER shell=True.
                # Managed in Python: open target file descriptor and pass as stdout/append.
                redir_idx = classification.tokens.index('>') if '>' in classification.tokens else classification.tokens.index('>>')
                file_mode = 'a' if classification.tokens[redir_idx] == '>>' else 'w'
                target_path = (exec_cwd / classification.target_file).resolve()
                target_file_obj = open(target_path, file_mode, encoding='utf-8')
                cmd_tokens = classification.tokens[:redir_idx]
                popen_args: dict[str, Any] = {
                    'cwd': exec_cwd,
                    'stdout': target_file_obj,
                    'stderr': subprocess.PIPE,
                    'text': True,
                    'shell': False,
                }
                cmd_input = cmd_tokens
            elif classification.is_unparseable_or_complex:
                # Approved complex composition: executes with shell=True
                popen_args = {
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
            pgid = None
            if os.name != 'nt':
                try:
                    pgid = os.getpgid(proc.pid)
                except OSError:
                    pgid = proc.pid
            if job_id and self.job_store:
                try:
                    self.job_store.register_process(job_id=job_id, pid=proc.pid, pgid=pgid)
                except Exception:
                    # Job was already cancelled: terminate immediately
                    if target_file_obj:
                        target_file_obj.close()
                    proc.kill()
                    proc.wait()
                    raise

            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                # Terminate entire process group on timeout
                if os.name == 'nt':
                    try:
                        subprocess.run(['taskkill', '/F', '/T', '/PID', str(proc.pid)], capture_output=True, timeout=5)
                    except Exception:
                        pass
                else:
                    if pgid and hasattr(os, 'killpg'):
                        try:
                            import signal
                            os.killpg(pgid, signal.SIGKILL)
                        except OSError:
                            pass
                proc.kill()
                stdout_data, stderr_data = proc.communicate()
                if target_file_obj:
                    target_file_obj.close()
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
            finally:
                if target_file_obj:
                    target_file_obj.close()

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
