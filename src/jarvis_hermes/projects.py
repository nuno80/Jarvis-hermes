"""Registry and safe reading of project files and logs across devices/environments."""
import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
import subprocess
import time
from typing import Any

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
    def __init__(self, config_path: str | Path | None = None, current_device: str = "local", current_environment: str = "wsl"):
        self.config_path = Path(config_path) if config_path else None
        self.current_device = current_device
        self.current_environment = current_environment
        self._config: dict[str, Any] | None = None

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

    def run_project_workflow(self, project_id: str, workflow_name: str,
                             device_id: str | None = None, timeout_seconds: int = 120) -> dict[str, Any]:
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

        start_time = time.time()
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                timeout=timeout_seconds
            )
            elapsed = time.time() - start_time
            raw_output = (proc.stdout or '') + (proc.stderr or '')
            redacted_out = redact_secrets(raw_output)
            return {
                'ok': proc.returncode == 0,
                'exit_code': proc.returncode,
                'output': redacted_out,
                'elapsed_seconds': round(elapsed, 3),
                'workflow': workflow_name,
                'project_id': project_id,
                'device_id': resolved_device
            }
        except subprocess.TimeoutExpired as exc:
            elapsed = time.time() - start_time
            return {
                'ok': False,
                'exit_code': -1,
                'output': 'Workflow execution timed out.',
                'elapsed_seconds': round(elapsed, 3),
                'workflow': workflow_name,
                'project_id': project_id,
                'device_id': resolved_device
            }
        except Exception as exc:
            raise ProjectError('EXECUTION_FAILED', f'Failed to run workflow: {exc}')

    def commit_project_changes(self, project_id: str, files: list[str],
                               commit_message: str, verification: dict[str, Any],
                               device_id: str | None = None) -> dict[str, Any]:
        """Commit only relevant files changed by the job, preserving unrelated worktree changes."""
        if not files:
            raise ProjectError('INVALID_ARGUMENT', 'No files specified to commit.')
        if not commit_message or not commit_message.strip():
            raise ProjectError('INVALID_ARGUMENT', 'Commit message cannot be empty.')
        if not verification or not verification.get('passed'):
            raise ProjectError('VERIFICATION_FAILED', 'Cannot commit changes without a passing verification.')

        root, resolved_device, _ = self.resolve_project_root(project_id, device_id)
        if not (root / '.git').exists():
            raise ProjectError('NOT_A_GIT_REPO', f'Project root {root} is not a git repository.')

        # Validate that all files are inside the repo and exist
        for rel_file in files:
            target = (root / rel_file).resolve()
            if not target.is_relative_to(root):
                raise ProjectError('PERMISSION_DENIED', f'File path {rel_file!r} traverses outside repository.')

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
            commit_proc = subprocess.run(
                ['git', 'commit', '-m', commit_message],
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
                'verification': verification,
                'project_id': project_id,
                'device_id': resolved_device
            }
        except subprocess.CalledProcessError as exc:
            stderr = exc.stderr.decode('utf-8', errors='replace') if isinstance(exc.stderr, bytes) else str(exc.stderr)
            raise ProjectError('GIT_ERROR', f'Git operation failed: {stderr}')
