"""Bounded, read-only Markdown access on an owner-controlled local filesystem."""
import os
import stat
from hashlib import sha256
from pathlib import Path, PurePosixPath
import yaml

MAX_NOTE_BYTES = 262144
MAX_ENTRIES = 5000
MAX_SEARCH_NOTES = 500
MAX_PREFERENCE_BYTES = 65536
FORBIDDEN_PREFERENCE_KEYS = frozenset({'permissions', 'security', 'approval', 'policy', 'approver', 'system_auth'})



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
        # Stable text offsets across Windows/Unix; version still identifies raw bytes.
        text = raw.decode('utf-8').replace('\r\n', '\n').replace('\r', '\n')
        return text, sha256(raw).hexdigest()

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

    def load_preferences(self) -> tuple[dict, str]:
        """Load typed explicit preferences from preferences.yaml in the vault root."""
        root = self.root()
        pref_path = root / 'preferences.yaml'
        if not pref_path.is_file():
            return {}, 'empty'
        info = pref_path.stat()
        if info.st_size > MAX_PREFERENCE_BYTES:
            raise VaultError('PREFERENCE_TOO_LARGE', 'preferences.yaml exceeds maximum size.')
        raw = pref_path.read_bytes()
        digest = sha256(raw).hexdigest()
        try:
            parsed = yaml.safe_load(raw.decode('utf-8')) or {}
        except Exception as exc:
            raise VaultError('INVALID_PREFERENCES', f'Failed to parse preferences.yaml: {exc}')
        if not isinstance(parsed, dict):
            raise VaultError('INVALID_PREFERENCES', 'preferences.yaml must be a key-value mapping.')

        # Strip any permission or security overrides (preferences cannot grant execution permissions)
        safe_prefs = {k: v for k, v in parsed.items() if k.lower() not in FORBIDDEN_PREFERENCE_KEYS}
        return safe_prefs, digest

    def update_preference(self, key_path: str, value: any, expected_version: str | None = None) -> dict:
        """Update a typed explicit preference in preferences.yaml with optimistic concurrency check."""
        root = self.root()
        pref_path = root / 'preferences.yaml'
        current_prefs, current_version = self.load_preferences()

        if expected_version is not None and expected_version != current_version:
            raise VaultError('CONFLICT', f'Preferences have changed concurrently (expected {expected_version}, current {current_version}).')

        # Check key path safety
        parts = key_path.strip().split('.')
        if not parts or any(not p for p in parts):
            raise VaultError('INVALID_ARGUMENT', 'Invalid preference key path.')
        if parts[0].lower() in FORBIDDEN_PREFERENCE_KEYS:
            raise VaultError('PERMISSION_DENIED', f'Preferences cannot define permissions or security policies: {parts[0]}')

        # Navigate and update dict
        cur = current_prefs
        for p in parts[:-1]:
            if p not in cur or not isinstance(cur[p], dict):
                cur[p] = {}
            cur = cur[p]
        cur[parts[-1]] = value

        # Atomic write to preferences.yaml
        new_content = yaml.safe_dump(current_prefs, sort_keys=False)
        temp_file = root / f'.preferences.yaml.tmp.{os.getpid()}'
        try:
            temp_file.write_text(new_content, encoding='utf-8')
            temp_file.replace(pref_path)
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink()
            raise VaultError('WRITE_FAILED', f'Failed to write preferences.yaml: {exc}')

        new_prefs, new_version = self.load_preferences()
        return {'key_path': key_path, 'value': value, 'version': new_version, 'preferences': new_prefs}

    def get_profile(self) -> dict:
        """Return the compiled user profile combining explicit preferences and scanned markdown frontmatter."""
        explicit, pref_version = self.load_preferences()
        root = self.root()

        # Scan vault notes for frontmatter preferences and notes (e.g. 00-System/Preferences.md or other notes)
        frontmatter_notes = []
        ambiguities = []

        try:
            for item in root.glob('**/*.md'):
                if item.name.startswith('.') or item.is_symlink():
                    continue
                try:
                    rel_id = item.relative_to(root).as_posix()
                    text, version = self.load(rel_id)
                except Exception:
                    continue

                if text.startswith('---\n'):
                    end_idx = text.find('\n---\n', 4)
                    if end_idx != -1:
                        raw_fm = text[4:end_idx]
                        try:
                            fm = yaml.safe_load(raw_fm)
                            if isinstance(fm, dict):
                                fm_type = fm.get('type')
                                domain = fm.get('domain')
                                source = fm.get('source', 'inferred')
                                if fm_type == 'preference' or domain:
                                    note_entry = {
                                        'note_id': rel_id,
                                        'type': fm_type,
                                        'domain': domain,
                                        'source': source,
                                        'confidence': fm.get('confidence', 1.0),
                                        'updated_at': fm.get('updated_at'),
                                        'version': version
                                    }
                                    frontmatter_notes.append(note_entry)

                                    # Check for contradictions / ambiguity with explicit preferences
                                    # Markdown frontmatter can define soft or inferred preferences or candidate preferences.
                                    # If frontmatter provides a preference key that conflicts with explicit preferences or has low confidence,
                                    # or contradicts an explicit preference, we surface an ambiguity.
                                    fm_prefs = fm.get('preferences')
                                    if isinstance(fm_prefs, dict):
                                        for pref_k, pref_v in fm_prefs.items():
                                            domain_prefs = explicit.get(domain, {}) if isinstance(explicit.get(domain), dict) else {}
                                            if pref_k in domain_prefs and domain_prefs[pref_k] != pref_v:
                                                ambiguities.append({
                                                    'domain': domain,
                                                    'key': pref_k,
                                                    'explicit_value': domain_prefs[pref_k],
                                                    'note_value': pref_v,
                                                    'note_id': rel_id,
                                                    'source': source,
                                                    'reason': f"Explicit preference '{domain}.{pref_k}={domain_prefs[pref_k]}' contradicts note '{rel_id}' value '{pref_v}'"
                                                })
                        except Exception:
                            pass
        except Exception:
            pass

        return {
            'explicit_preferences': explicit,
            'preferences_version': pref_version,
            'frontmatter_notes': frontmatter_notes,
            'ambiguities': ambiguities,
            'untrusted_content': True
        }

