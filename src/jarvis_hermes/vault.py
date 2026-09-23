"""Bounded, read-only Markdown access on an owner-controlled local filesystem."""
import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath

MAX_NOTE_BYTES = 262144
MAX_ENTRIES = 5000
MAX_SEARCH_NOTES = 500


class VaultError(Exception):
    def __init__(self, code: str, message: str):
        self.code, self.message = code, message
        super().__init__(message)


class Vault:
    def __init__(self, configured: str | None):
        self.configured = configured

    def root(self) -> Path:
        if not self.configured:
            raise VaultError('NOT_CONFIGURED', 'Configure a local vault before reading notes.')
        root = Path(self.configured).expanduser()
        if not root.is_absolute() or not root.is_dir():
            raise VaultError('VAULT_UNAVAILABLE', 'The configured vault is unavailable.')
        return root.resolve()

    def load(self, note_id: str) -> tuple[str, str]:
        root = self.root()
        parts = PurePosixPath(note_id)
        if (not note_id or parts.is_absolute() or '\\' in note_id or ':' in note_id
                or any(not p or p.startswith('.') for p in note_id.split('/'))
                or parts.suffix.lower() != '.md'):
            raise VaultError('PERMISSION_DENIED', 'Only visible Markdown notes within the vault are accessible.')
        path = root
        for part in parts.parts:
            path /= part
            if path.is_symlink() or path.is_junction():
                raise VaultError('PERMISSION_DENIED', 'Linked files and directories are not accessible.')
        if not path.resolve().is_relative_to(root):
            raise VaultError('PERMISSION_DENIED', 'The note is outside the configured vault.')
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise VaultError('PERMISSION_DENIED', 'Only ordinary unlinked files are accessible.')
        if info.st_size > MAX_NOTE_BYTES:
            raise VaultError('NOTE_TOO_LARGE', 'The note exceeds the 256 KiB reading limit.')
        # O_NOFOLLOW protects the leaf on supporting platforms. This is not an OS
        # sandbox against a hostile process concurrently replacing parent folders.
        descriptor = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(descriptor, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or opened.st_nlink > 1:
                raise VaultError('PERMISSION_DENIED', 'The file changed during access.')
            raw = stream.read(MAX_NOTE_BYTES + 1)
        if len(raw) > MAX_NOTE_BYTES:
            raise VaultError('NOTE_TOO_LARGE', 'The note exceeds the 256 KiB reading limit.')
        return raw.decode('utf-8'), sha256(raw).hexdigest()

    def read(self, note_id: str, offset: int = 0, limit: int = 8000) -> dict:
        if not 0 <= offset or not 1 <= limit <= 8000:
            raise VaultError('INVALID_ARGUMENT', 'Offset must be nonnegative; limit must be 1..8000 characters.')
        text, version = self.load(note_id)
        if offset > len(text):
            raise VaultError('INVALID_ARGUMENT', 'Offset exceeds the note length.')
        return {'note_id': note_id, 'version': version,
                'content': text[offset:offset + limit], 'offset': offset,
                'next_offset': offset + limit if offset + limit < len(text) else None,
                'untrusted_content': True}

    def search(self, query: str, limit: int = 10) -> dict:
        if not query.strip() or len(query) > 200 or not 1 <= limit <= 20:
            raise VaultError('INVALID_ARGUMENT', 'Provide 1..200 query characters and a result limit of 1..20.')
        root = self.root()
        folders, candidates = [root], []
        entries = 0
        skipped = 0
        truncated = False
        while folders and entries < MAX_ENTRIES:
            folder = folders.pop()
            # Check again in case a folder changed since it was enumerated.
            if folder.is_symlink() or folder.is_junction() or not folder.resolve().is_relative_to(root):
                skipped += 1
                continue
            try:
                with os.scandir(folder) as listing:
                    for entry in listing:
                        entries += 1
                        if entries > MAX_ENTRIES:
                            truncated = True
                            break
                        path = Path(entry.path)
                        if entry.name.startswith('.') or entry.is_symlink() or path.is_junction():
                            continue
                        if entry.is_dir(follow_symlinks=False):
                            folders.append(path)
                        elif entry.is_file(follow_symlinks=False) and path.suffix.lower() == '.md':
                            candidates.append(path.relative_to(root).as_posix())
            except OSError:
                skipped += 1
        truncated = truncated or bool(folders) or len(candidates) > MAX_SEARCH_NOTES
        matches = []
        scanned = 0
        for note_id in sorted(candidates)[:MAX_SEARCH_NOTES]:
            try:
                text, version = self.load(note_id)
            except (VaultError, OSError, UnicodeError):
                skipped += 1
                continue
            scanned += 1
            # Line-based excerpt avoids casefold length differences affecting offsets.
            line = next((line for line in text.splitlines() if query.casefold() in line.casefold()), None)
            if line is not None:
                if len(matches) < limit:
                    matches.append({'note_id': note_id, 'version': version, 'excerpt': line[:300]})
                else:
                    truncated = True
        return {'matches': matches, 'truncated': truncated, 'scanned_notes': scanned,
                'skipped_entries': skipped, 'untrusted_content': True}
