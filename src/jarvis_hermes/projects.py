"""Registry and safe reading of project files and logs across devices/environments."""
import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
import sqlite3
import subprocess
import time
from typing import Any
import uuid

from jarvis_hermes.approval import ApprovalStore, ApprovalError

MAX_PROJECT_FILE_BYTES = 524288  # 512 KiB
DEFAULT_PAGE_SIZE = 8000

SECRET_PATTERNS = [
    re.compile(r'(?i)(api[_-]?key|secret|password|token|bearer|auth|authorization|credential)[\s:=]+([\'"]?)([a-zA-Z0-9_\-\.\+/]{8,})\2'),
    re.compile(r'ghp_[a-zA-Z0-9]{36}'),
    re.compile(r'github_pat_[a-zA-Z0-9]{22}_[a-zA-Z0-9]{59}'),
    re.compile(r'xox[baprs]-[a-zA-Z0-9]{10,48}'),
    re.compile(r'ey[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}\.[a-zA-Z0-9_-]{10,}'),
]


def redact_secrets(text: str) -> str:
    redacted = text
    for pat in SECRET_PATTERNS:
        def repl(match: re.Match) -> str:
            if match.re.groups >= 3:
                prefix = match.group(1)
                quote = match.group(2)
                return f"{prefix}={quote}[REDACTED]{quote}"
            val = match.group(0)
            return val[:4] + "[REDACTED]" if len(val) > 8 else "[REDACTED]"
        redacted = pat.sub(repl, redacted)
    return redacted


class ProjectError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(message)


class ProjectRegistry:
    def __init__(self, config_path: str | Path | None = None, current_device: str = "local",
                 current_environment: str = "wsl", approval_store: ApprovalStore | None = None,
                 state_dir: str | Path | None = None, job_store: Any | None = None):
        self.config_path = Path(config_path) if config_path else None
        self.current_device = current_device
        self.current_environment = current_environment
        self.approval_store = approval_store
        self.job_store = job_store
        self._config: dict[str, Any] | None = None

        if state_dir is None:
            state_home = Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local' / 'state'))
            self.state_dir = state_home / 'jarvis-hermes'
        else:
            self.state_dir = Path(state_dir)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        try:
            self.state_dir.chmod(0o700)
        except OSError:
            pass

        self.db_path = self.state_dir / 'verifications.sqlite3'
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=10.0)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute('''
                CREATE TABLE IF NOT EXISTS verification_runs (
                    verification_run_id TEXT PRIMARY KEY,
                    job_id TEXT,
                    project_id TEXT NOT NULL,
                    workflow_name TEXT NOT NULL,
                    file_hashes TEXT NOT NULL,
                    passed INTEGER NOT NULL,
                    exit_code INTEGER NOT NULL,
                    output TEXT NOT NULL,
                    created_at REAL NOT NULL
                )
            ''')
            conn.commit()

    def _load_config(self) -> dict[str, Any]:
        if self._config is not None:
            return self._config
        if not self.config_path or not self.config_path.exists():
            raise ProjectError('NOT_CONFIGURED', 'Project registry configuration not found.')
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                self._config = json.load(f)
        except Exception as exc:
            raise ProjectError('NOT_CONFIGURED', f'Invalid project registry configuration: {exc}')
        return self._config

    def get_registered_roots(self) -> list[Path]:
        """Return list of all registered project root directories on the current device."""
        roots: list[Path] = []
        try:
            cfg = self._load_config()
        except Exception:
            return roots
        projects = cfg.get('projects', {})
        for proj_info in projects.values():
            paths = proj_info.get('paths', {})
            p_str = paths.get(self.current_device)
            if p_str:
                p = Path(p_str).expanduser()
                if p.is_absolute() and p.is_dir():
                    roots.append(p.resolve())
        return roots

    def resolve_project_root(self, project_id: str, device_id: str | None = None) -> tuple[Path, str, str]:
        cfg = self._load_config()
        projects = cfg.get('projects', {})
        if project_id not in projects:
            raise ProjectError('PROJECT_NOT_FOUND', f'Project {project_id!r} is not registered.')

        target_device = device_id or self.current_device
        devices = cfg.get('devices', {})
        if target_device not in devices:
            raise ProjectError('DEVICE_NOT_FOUND', f'Device {target_device!r} is not registered.')

        target_env = devices[target_device].get('environment', 'unknown')

        # Check WSL availability / cross-device dispatch
        if target_device != self.current_device:
            if target_env == 'wsl' and self.current_environment != 'wsl':
                raise ProjectError('WSL_UNAVAILABLE', f'WSL environment on device {target_device!r} is unavailable from current host.')
            raise ProjectError('DEVICE_OFFLINE', f'Device {target_device!r} is not reachable from current host.')

        proj = projects[project_id]
        paths = proj.get('paths', {})
        if target_device not in paths:
            raise ProjectError('PROJECT_PATH_NOT_CONFIGURED', f'Project {project_id!r} has no configured path on device {target_device!r}.')

        root = Path(paths[target_device]).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise ProjectError('RESOURCE_UNAVAILABLE', f'Configured project root is not an accessible directory: {root}')

        return root.resolve(), target_device, target_env

    def read_project_file(self, project_id: str, relative_path: str, device_id: str | None = None,
                          offset: int = 0, limit: int = DEFAULT_PAGE_SIZE) -> dict[str, Any]:
        if not 0 <= offset or not 1 <= limit <= 65536:
            raise ProjectError('INVALID_ARGUMENT', 'Offset must be >= 0 and limit must be 1..65536.')

        root, resolved_device, resolved_env = self.resolve_project_root(project_id, device_id)

        clean_rel = relative_path.replace('\\', '/')
        parts = PurePosixPath(clean_rel)
        if not clean_rel or parts.is_absolute() or any(p in ('..', '.') for p in parts.parts) or clean_rel.startswith('/'):
            raise ProjectError('PERMISSION_DENIED', 'Invalid file path traversal.')

        target = root
        for part in parts.parts:
            target = target / part
            if target.is_symlink() or (hasattr(target, 'is_junction') and target.is_junction()):
                raise ProjectError('PERMISSION_DENIED', 'Linked files and directories are not accessible.')

        resolved_target = target.resolve()
        if not resolved_target.is_relative_to(root):
            raise ProjectError('PERMISSION_DENIED', 'Path traverses outside the project directory.')

        if not target.exists():
            raise ProjectError('FILE_NOT_FOUND', f'File {relative_path!r} not found in project.')

        try:
            info = target.stat()
        except PermissionError:
            raise ProjectError('PERMISSION_DENIED', 'Access denied to target file.')

        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ProjectError('PERMISSION_DENIED', 'Only ordinary unlinked regular files are accessible.')

        is_large = info.st_size > MAX_PROJECT_FILE_BYTES

        try:
            descriptor = os.open(target, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        except PermissionError:
            raise ProjectError('PERMISSION_DENIED', 'Access denied to target file.')
        except OSError:
            raise ProjectError('RESOURCE_UNAVAILABLE', 'Could not open file.')

        try:
            with os.fdopen(descriptor, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1:
                    raise ProjectError('PERMISSION_DENIED', 'File changed during access.')
                raw = stream.read(MAX_PROJECT_FILE_BYTES + 1)
        except PermissionError:
            raise ProjectError('PERMISSION_DENIED', 'Access denied to target file.')

        truncated_file = len(raw) > MAX_PROJECT_FILE_BYTES
        version = sha256(raw).hexdigest()

        try:
            text = raw.decode('utf-8', errors='replace')
        except Exception:
            text = ''

        text = text.replace('\r\n', '\n').replace('\r', '\n')
        text = redact_secrets(text)

        total_chars = len(text)
        page = text[offset:offset + limit]
        next_offset = (offset + limit) if (offset + limit < total_chars) else None

        result: dict[str, Any] = {
            'project_id': project_id,
            'device_id': resolved_device,
            'environment': resolved_env,
            'relative_path': relative_path,
            'version': version,
            'content': page,
            'offset': offset,
            'limit': limit,
            'total_characters': total_chars,
            'next_offset': next_offset,
            'is_truncated': truncated_file or (next_offset is not None),
            'untrusted_content': True,
        }

        if truncated_file:
            result['artifact'] = {
                'size_bytes': info.st_size,
                'truncated_at_bytes': MAX_PROJECT_FILE_BYTES,
                'note': 'File exceeds maximum inlined size and was truncated as an artifact slice.'
            }

        return result

    def _verify_git_hooks(self, root: Path, allowed_hook_hashes: dict[str, str]) -> None:
        """Verify that git hooks are not modified or unexpectedly installed."""
        hooks_dir = root / '.git' / 'hooks'
        if not hooks_dir.is_dir():
            return
        for hook_file in hooks_dir.iterdir():
            if hook_file.name.endswith('.sample') or not hook_file.is_file():
                continue
            h = sha256(hook_file.read_bytes()).hexdigest()
            if hook_file.name not in allowed_hook_hashes or allowed_hook_hashes[hook_file.name] != h:
                raise ProjectError('HOOK_MODIFIED', f'Git hook {hook_file.name!r} has unexpected or untrusted hash.')

    def _snapshot_file_hashes(self, root: Path, file_list: list[str] | None = None) -> dict[str, str]:
        """Compute sha256 of tracked files or specific file list in root."""
        hashes: dict[str, str] = {}
        if file_list is not None:
            for rel in file_list:
                p = (root / rel).resolve()
                if p.is_relative_to(root) and p.is_file():
                    hashes[rel.replace('\\', '/')] = sha256(p.read_bytes()).hexdigest()
            return hashes

        # If file_list is None, snapshot all files changed or tracked via git
        try:
            res = subprocess.run(['git', 'ls-files'], cwd=root, capture_output=True, text=True, check=True)
            for line in res.stdout.splitlines():
                rel = line.strip()
                if not rel:
                    continue
                p = (root / rel).resolve()
                if p.is_relative_to(root) and p.is_file():
                    hashes[rel.replace('\\', '/')] = sha256(p.read_bytes()).hexdigest()
        except Exception:
            # Fallback if git fails: walk root excluding .git
            for p in root.rglob('*'):
                if '.git' in p.parts:
                    continue
                if p.is_file():
                    rel = p.relative_to(root).as_posix()
                    hashes[rel] = sha256(p.read_bytes()).hexdigest()
        return hashes

    def run_project_workflow(self, project_id: str, workflow_name: str,
                             device_id: str | None = None, timeout_seconds: int = 120,
                             job_id: str | None = None, files: list[str] | None = None) -> dict[str, Any]:
        """Run an allowed project workflow (e.g. test, build) checking script and hook integrity."""
        root, resolved_device, resolved_env = self.resolve_project_root(project_id, device_id)
        cfg = self._load_config()
        proj = cfg.get('projects', {}).get(project_id, {})
        workflows = proj.get('workflows', {})

        if workflow_name not in workflows:
            raise ProjectError('WORKFLOW_NOT_ALLOWED', f'Workflow {workflow_name!r} is not allowed for project {project_id!r}.')

        wf = workflows[workflow_name]
        command = wf.get('command')
        if not isinstance(command, list) or not command:
            raise ProjectError('INVALID_WORKFLOW', f'Workflow {workflow_name!r} has invalid command specification.')

        # Check git hooks
        allowed_hooks = wf.get('allowed_hook_hashes', {})
        self._verify_git_hooks(root, allowed_hooks)

        # Check expected file/script hashes
        expected_hashes = wf.get('expected_hashes', {})
        for rel_script, expected_h in expected_hashes.items():
            script_path = (root / rel_script).resolve()
            if not script_path.is_relative_to(root) or not script_path.is_file():
                raise ProjectError('SCRIPT_MODIFIED', f'Script {rel_script!r} is missing or outside root.')
            current_h = sha256(script_path.read_bytes()).hexdigest()
            if current_h != expected_h:
                raise ProjectError('SCRIPT_MODIFIED', f'Script {rel_script!r} has been altered (hash mismatch).')

        if job_id and self.job_store:
            self.job_store.check_not_cancelled(job_id)

        start_time = time.time()
        try:
            popen_args: dict[str, Any] = {
                'cwd': root,
                'stdout': subprocess.PIPE,
                'stderr': subprocess.PIPE,
                'text': True,
            }
            if os.name != 'nt':
                popen_args['start_new_session'] = True

            proc = subprocess.Popen(
                command,
                **popen_args
            )

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
                    proc.kill()
                    proc.wait()
                    raise

            try:
                stdout_data, stderr_data = proc.communicate(timeout=timeout_seconds)
            except subprocess.TimeoutExpired:
                proc.kill()
                stdout_data, stderr_data = proc.communicate()
                elapsed = time.time() - start_time
                passed = False
                exit_code = -1
                redacted_out = 'Workflow execution timed out.'
            else:
                elapsed = time.time() - start_time
                raw_output = (stdout_data or '') + (stderr_data or '')
                redacted_out = redact_secrets(raw_output)
                passed = (proc.returncode == 0)
                exit_code = proc.returncode
        except Exception as exc:
            if isinstance(exc, ProjectError):
                raise
            raise ProjectError('EXECUTION_FAILED', f'Failed to run workflow: {exc}')

        # Snapshot file hashes involved in verification
        file_hashes = self._snapshot_file_hashes(root, files)
        run_id = f"vr-{uuid.uuid4().hex[:16]}"
        now = time.time()

        with self._get_connection() as conn:
            conn.execute('''
                INSERT INTO verification_runs (
                    verification_run_id, job_id, project_id, workflow_name,
                    file_hashes, passed, exit_code, output, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                run_id, job_id, project_id, workflow_name,
                json.dumps(file_hashes), 1 if passed else 0, exit_code, redacted_out, now
            ))
            conn.commit()

        return {
            'ok': passed,
            'exit_code': exit_code,
            'output': redacted_out,
            'elapsed_seconds': round(elapsed, 3),
            'workflow': workflow_name,
            'project_id': project_id,
            'device_id': resolved_device,
            'verification_run_id': run_id
        }

    def commit_project_changes(self, project_id: str, files: list[str],
                               commit_message: str, verification_run_id: str,
                               device_id: str | None = None,
                               max_age_seconds: int = 3600,
                               no_verify: bool = False,
                               job_id: str | None = None) -> dict[str, Any]:
        """Commit only relevant files changed by the job after verifying verification_run_id."""
        if job_id and self.job_store:
            self.job_store.check_not_cancelled(job_id)
        if isinstance(verification_run_id, dict):
            raise ProjectError('VERIFICATION_REQUIRED', 'verification_run_id must be a string ID referencing a recorded verification run, not a dictionary.')
        if not verification_run_id or not isinstance(verification_run_id, str):
            raise ProjectError('VERIFICATION_REQUIRED', 'A valid verification_run_id is required.')

        if not files:
            raise ProjectError('INVALID_ARGUMENT', 'No files specified to commit.')
        if not commit_message or not commit_message.strip():
            raise ProjectError('INVALID_ARGUMENT', 'Commit message cannot be empty.')

        root, resolved_device, _ = self.resolve_project_root(project_id, device_id)
        if not (root / '.git').exists():
            raise ProjectError('NOT_A_GIT_REPO', f'Project root {root} is not a git repository.')

        # Validate verification run from SQLite storage
        with self._get_connection() as conn:
            row = conn.execute('SELECT * FROM verification_runs WHERE verification_run_id = ?',
                               (verification_run_id,)).fetchone()
            if not row:
                raise ProjectError('VERIFICATION_REQUIRED', f'Verification run {verification_run_id!r} not found.')

            if row['project_id'] != project_id:
                raise ProjectError('VERIFICATION_REQUIRED', f'Verification run {verification_run_id!r} belongs to project {row["project_id"]!r}, not {project_id!r}.')

            if not row['passed']:
                raise ProjectError('VERIFICATION_REQUIRED', f'Verification run {verification_run_id!r} failed.')

            if max_age_seconds > 0 and (time.time() - row['created_at']) > max_age_seconds:
                raise ProjectError('VERIFICATION_REQUIRED', f'Verification run {verification_run_id!r} has expired.')

            run_hashes = json.loads(row['file_hashes'])

        # Validate that all files are inside the repo and exist
        for rel_file in files:
            target = (root / rel_file).resolve()
            if not target.is_relative_to(root):
                raise ProjectError('PERMISSION_DENIED', f'File path {rel_file!r} traverses outside repository.')
            if not target.is_file():
                raise ProjectError('FILE_NOT_FOUND', f'File {rel_file!r} does not exist.')

            norm_rel = rel_file.replace('\\', '/')
            current_h = sha256(target.read_bytes()).hexdigest()
            # If the verification run recorded hashes for these files, they must match!
            if norm_rel in run_hashes and run_hashes[norm_rel] != current_h:
                raise ProjectError('VERIFICATION_REQUIRED', f'File {rel_file!r} was modified after verification run.')
            elif norm_rel not in run_hashes:
                # If specific files were tracked in verification and this file wasn't verified
                raise ProjectError('VERIFICATION_REQUIRED', f'File {rel_file!r} was not verified in verification run {verification_run_id!r}.')

        # Check git hooks before git commit
        cfg = self._load_config()
        proj = cfg.get('projects', {}).get(project_id, {})
        allowed_hook_hashes = proj.get('allowed_hook_hashes', {})
        # If any workflow defines allowed_hook_hashes, combine them
        for wf in proj.get('workflows', {}).values():
            if 'allowed_hook_hashes' in wf:
                allowed_hook_hashes.update(wf['allowed_hook_hashes'])

        commit_flags = []
        if no_verify:
            # no_verify must be explicitly allowed by project configuration
            if not proj.get('allow_no_verify', False):
                raise ProjectError('PERMISSION_DENIED', 'Commit with --no-verify is not permitted by project configuration.')
            commit_flags.append('--no-verify')
        else:
            self._verify_git_hooks(root, allowed_hook_hashes)

        try:
            # Stage only the specified files
            subprocess.run(['git', 'add', '--'] + files, cwd=root, check=True, capture_output=True)

            # Check if there is anything staged for commit
            diff_cached = subprocess.run(['git', 'diff', '--cached', '--name-only'],
                                         cwd=root, capture_output=True, text=True, check=True)
            staged_files = [f.strip() for f in diff_cached.stdout.splitlines() if f.strip()]
            if not staged_files:
                raise ProjectError('NOTHING_TO_COMMIT', 'None of the specified files have changes to commit.')

            # Commit staged changes
            commit_cmd = ['git', 'commit', '-m', commit_message] + commit_flags
            commit_proc = subprocess.run(
                commit_cmd,
                cwd=root, capture_output=True, text=True, check=True
            )

            # Retrieve commit hash
            rev_proc = subprocess.run(
                ['git', 'rev-parse', 'HEAD'],
                cwd=root, capture_output=True, text=True, check=True
            )
            commit_hash = rev_proc.stdout.strip()

            return {
                'committed': True,
                'commit_hash': commit_hash,
                'files': staged_files,
                'message': commit_message,
                'verification_run_id': verification_run_id,
                'project_id': project_id,
                'device_id': resolved_device
            }
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else str(exc.stderr)
            raise ProjectError('GIT_ERROR', f'Git operation failed: {stderr}')

    def verify_remote_commit(self, project_id: str, remote: str, branch: str,
                             commit_hash: str, device_id: str | None = None) -> bool:
        """Check whether commit_hash is already present at the remote branch."""
        root, _, _ = self.resolve_project_root(project_id, device_id)
        try:
            res = subprocess.run(
                ['git', 'ls-remote', remote, f'refs/heads/{branch}'],
                cwd=root, capture_output=True, text=True, check=True
            )
            for line in res.stdout.splitlines():
                parts = line.strip().split()
                if len(parts) >= 2 and parts[0] == commit_hash and parts[1] == f'refs/heads/{branch}':
                    return True
            return False
        except subprocess.CalledProcessError:
            return False

    def push_project_commit(self, project_id: str, remote: str, branch: str,
                            commit_hash: str, approval_token: str | None,
                            actor_id: int, device_id: str | None = None) -> dict[str, Any]:
        """Push an authorized commit to remote after validating single-use approval."""
        if not approval_token:
            raise ProjectError('APPROVAL_REQUIRED', 'Push requires an explicit approval token.')

        root, resolved_device, _ = self.resolve_project_root(project_id, device_id)
        if not (root / '.git').exists():
            raise ProjectError('NOT_A_GIT_REPO', f'Project root {root} is not a git repository.')

        cfg = self._load_config()
        proj = cfg.get('projects', {}).get(project_id, {})
        allowed_remotes = proj.get('allowed_remotes', [])
        allowed_branches = proj.get('allowed_branches', [])

        if not allowed_remotes or remote not in allowed_remotes:
            raise ProjectError('REMOTE_NOT_ALLOWED', f'Remote {remote!r} is not allowed for project {project_id!r}.')
        if not allowed_branches or branch not in allowed_branches:
            raise ProjectError('BRANCH_NOT_ALLOWED', f'Branch {branch!r} is not allowed for project {project_id!r}.')

        # Check commit hash format
        if not re.fullmatch(r'[0-9a-fA-F]{40}', commit_hash):
            raise ProjectError('INVALID_ARGUMENT', 'Invalid commit hash format.')

        # Verify commit exists locally
        rev_proc = subprocess.run(
            ['git', 'cat-file', '-t', commit_hash],
            cwd=root, capture_output=True, text=True
        )
        if rev_proc.returncode != 0 or rev_proc.stdout.strip() != 'commit':
            raise ProjectError('COMMIT_NOT_FOUND', f'Commit {commit_hash!r} does not exist in local repository.')

        # Validate single-use approval bound to exact parameters
        if not self.approval_store:
            raise ProjectError('APPROVAL_UNAVAILABLE', 'Approval store not configured.')

        target = f'git_push:{project_id}'
        arguments = {'remote': remote, 'branch': branch, 'commit_hash': commit_hash}

        try:
            decision = self.approval_store.decide(
                token=approval_token,
                actor_id=actor_id,
                target=target,
                arguments=arguments,
                approve=True
            )
        except ApprovalError as exc:
            raise ProjectError(exc.code, f'Approval failed: {exc.code}')

        if decision.get('status') != 'executed':
            raise ProjectError('APPROVAL_DENIED', 'Push approval was not executed.')

        # Perform git push
        try:
            push_proc = subprocess.run(
                ['git', 'push', remote, f'{commit_hash}:refs/heads/{branch}'],
                cwd=root, capture_output=True, text=True, check=True
            )
        except subprocess.CalledProcessError as exc:
            # Check if commit made it despite error
            remote_verified = self.verify_remote_commit(project_id, remote, branch, commit_hash, device_id)
            stderr = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else str(exc.stderr)
            raise ProjectError('GIT_PUSH_FAILED', f'Git push failed (remote_verified={remote_verified}): {stderr}')

        # Verify on remote
        remote_verified = self.verify_remote_commit(project_id, remote, branch, commit_hash, device_id)

        if not remote_verified:
            return {
                'pushed': False,
                'status': 'OUTCOME_UNKNOWN',
                'remote_verified': False,
                'project_id': project_id,
                'remote': remote,
                'branch': branch,
                'commit_hash': commit_hash,
                'device_id': resolved_device,
                'warning': 'Push command returned 0 but commit ref could not be verified on remote.',
            }

        return {
            'pushed': True,
            'status': 'SUCCEEDED',
            'remote_verified': True,
            'project_id': project_id,
            'remote': remote,
            'branch': branch,
            'commit_hash': commit_hash,
            'device_id': resolved_device
        }

