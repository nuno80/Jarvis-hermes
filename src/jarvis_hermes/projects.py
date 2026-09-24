"""Registry and safe reading of project files and logs across devices/environments."""
import json
import os
import re
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
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
