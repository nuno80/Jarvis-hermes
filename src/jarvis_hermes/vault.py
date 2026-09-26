"""Bounded, read-only Markdown access on an owner-controlled local filesystem."""
import os
import stat
from datetime import datetime, timezone
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

    def write_note(self, note_id: str, content: str, expected_version: str | None = None) -> dict:
        """Safely write/overwrite a Markdown note in the vault with concurrency check and safety boundaries."""
        root = self.root()
        parts = PurePosixPath(note_id)
        if (not note_id or parts.is_absolute() or '\\' in note_id or ':' in note_id
                or any(not p or p.startswith('.') for p in note_id.split('/'))
                or parts.suffix.lower() != '.md'):
            raise VaultError('PERMISSION_DENIED', 'Only visible Markdown notes within the vault are accessible.')
        
        target = root
        for part in parts.parts:
            target /= part
            if target.exists() and (target.is_symlink() or target.is_junction()):
                raise VaultError('PERMISSION_DENIED', 'Linked files and directories are not accessible.')
        
        if not target.resolve().is_relative_to(root):
            raise VaultError('PERMISSION_DENIED', 'The note is outside the configured vault.')

        current_version = None
        if target.exists():
            _, current_version = self.load(note_id)
            if expected_version is not None and expected_version != current_version:
                raise VaultError('CONFLICT', f'Note has changed concurrently (expected {expected_version}, current {current_version}).')
        elif expected_version is not None and expected_version != 'new':
            raise VaultError('CONFLICT', f'Expected existing version {expected_version}, but note does not exist.')

        raw = content.encode('utf-8')
        if len(raw) > MAX_NOTE_BYTES:
            raise VaultError('NOTE_TOO_LARGE', 'The note exceeds the 256 KiB reading limit.')

        # Ensure parent dirs exist safely
        target.parent.mkdir(parents=True, exist_ok=True)
        temp_file = target.parent / f'.{target.name}.tmp.{os.getpid()}'
        try:
            temp_file.write_bytes(raw)
            temp_file.replace(target)
        except Exception as exc:
            if temp_file.exists():
                temp_file.unlink()
            raise VaultError('WRITE_FAILED', f'Failed to write note: {exc}')

        new_text, new_version = self.load(note_id)
        return {'note_id': note_id, 'version': new_version, 'size_bytes': len(raw)}

    def propose_memory(self, domain: str, key: str, value: any,
                       evidence: str, confidence: float = 0.6,
                       note_id: str | None = None) -> dict:
        """Register a candidate/soft inferred memory without promoting it to an explicit constraint."""
        if not domain or not key:
            raise VaultError('INVALID_ARGUMENT', 'Domain and key must be non-empty strings.')
        if not 0.0 <= confidence <= 1.0:
            raise VaultError('INVALID_ARGUMENT', 'Confidence must be between 0.0 and 1.0.')

        # Explicit preference check: explicit always takes precedence
        explicit_prefs, _ = self.load_preferences()
        domain_explicit = explicit_prefs.get(domain, {}) if isinstance(explicit_prefs.get(domain), dict) else {}
        is_conflicting_explicit = key in domain_explicit and domain_explicit[key] != value

        target_note_id = note_id or f"Knowledge/Inferred/{domain}.md"
        # Check if note already exists
        root = self.root()
        full_path = root / target_note_id

        existing_frontmatter = {}
        body = f"# Memory: {domain}\n\nCandidate inferred preference.\n"
        expected_ver = None

        if full_path.is_file():
            text, expected_ver = self.load(target_note_id)
            if text.startswith('---\n'):
                end_idx = text.find('\n---\n', 4)
                if end_idx != -1:
                    try:
                        existing_frontmatter = yaml.safe_load(text[4:end_idx]) or {}
                        body = text[end_idx + 5:]
                    except Exception:
                        pass

        # Update candidate frontmatter
        fm = existing_frontmatter if isinstance(existing_frontmatter, dict) else {}
        fm['type'] = 'candidate_preference'
        fm['domain'] = domain
        fm['source'] = 'inferred'
        fm['status'] = 'candidate'  # soft state, distinct from explicit
        fm['confidence'] = confidence
        fm['updated_at'] = datetime.now(timezone.utc).isoformat()
        
        prefs = fm.get('preferences', {})
        if not isinstance(prefs, dict):
            prefs = {}
        prefs[key] = value
        fm['preferences'] = prefs

        evidence_list = fm.get('evidence', [])
        if not isinstance(evidence_list, list):
            evidence_list = []
        evidence_list.append({
            'evidence': evidence,
            'timestamp': datetime.now(timezone.utc).isoformat()
        })
        fm['evidence'] = evidence_list

        new_fm_str = yaml.safe_dump(fm, sort_keys=False)
        new_content = f"---\n{new_fm_str}---\n{body}"

        write_res = self.write_note(target_note_id, new_content, expected_version=expected_ver)

        return {
            'note_id': target_note_id,
            'domain': domain,
            'key': key,
            'value': value,
            'status': 'candidate',
            'confidence': confidence,
            'evidence': evidence,
            'conflicts_with_explicit': is_conflicting_explicit,
            'explicit_value': domain_explicit.get(key),
            'version': write_res['version']
        }

    def forget_memory(self, note_id: str, key: str | None = None, expected_version: str | None = None) -> dict:
        """Remove or correct an inferred memory note/key, updating frontmatter and indices while preserving backups/unrelated notes."""
        text, current_version = self.load(note_id)
        if expected_version is not None and expected_version != current_version:
            raise VaultError('CONFLICT', f'Note has changed concurrently (expected {expected_version}, current {current_version}).')

        root = self.root()
        target = root / note_id

        # Declared backup retention before modification/deletion
        backup_dir = root / '.jarvis_backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        retention_info = "Local backup stored in .jarvis_backups; retention declared per backup policy."
        backup_file = backup_dir / f"{target.name}.{int(datetime.now(timezone.utc).timestamp())}.bak"
        backup_file.write_bytes(target.read_bytes())

        if key is None:
            # Full deletion of the inferred candidate note
            target.unlink()
            return {
                'note_id': note_id,
                'action': 'deleted',
                'backup_retained': str(backup_file.relative_to(root).as_posix()),
                'retention_policy': retention_info
            }

        # Partial removal of a specific key in the note
        if not text.startswith('---\n'):
            raise VaultError('INVALID_ARGUMENT', 'Note does not contain YAML frontmatter.')
        end_idx = text.find('\n---\n', 4)
        if end_idx == -1:
            raise VaultError('INVALID_ARGUMENT', 'Malformed YAML frontmatter.')

        fm = yaml.safe_load(text[4:end_idx]) or {}
        body = text[end_idx + 5:]

        if isinstance(fm, dict) and 'preferences' in fm and isinstance(fm['preferences'], dict):
            fm['preferences'].pop(key, None)
            fm['updated_at'] = datetime.now(timezone.utc).isoformat()
            new_fm_str = yaml.safe_dump(fm, sort_keys=False)
            new_content = f"---\n{new_fm_str}---\n{body}"
            res = self.write_note(note_id, new_content, expected_version=current_version)
            return {
                'note_id': note_id,
                'action': 'key_removed',
                'key': key,
                'version': res['version'],
                'backup_retained': str(backup_file.relative_to(root).as_posix()),
                'retention_policy': retention_info
            }
        else:
            return {
                'note_id': note_id,
                'action': 'key_not_found',
                'key': key,
                'version': current_version
            }

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

        # Backup preferences.yaml before update
        backup_dir = root / '.jarvis_backups'
        backup_dir.mkdir(parents=True, exist_ok=True)
        if pref_path.exists():
            backup_file = backup_dir / f"preferences.yaml.{int(datetime.now(timezone.utc).timestamp())}.bak"
            backup_file.write_bytes(pref_path.read_bytes())

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
                                status = fm.get('status', 'explicit' if source == 'explicit' else 'soft')
                                if fm_type in ('preference', 'candidate_preference') or domain:
                                    note_entry = {
                                        'note_id': rel_id,
                                        'type': fm_type,
                                        'domain': domain,
                                        'source': source,
                                        'status': status,
                                        'confidence': fm.get('confidence', 1.0),
                                        'updated_at': fm.get('updated_at'),
                                        'version': version,
                                        'evidence': fm.get('evidence', [])
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
                                                    'status': status,
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

