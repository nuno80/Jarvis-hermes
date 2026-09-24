import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from datetime import timedelta

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult


@asynccontextmanager
async def connected(vault=None, *, approver_id=None, approval_action='accept', state_home=None, elicit=True, projects_config=None):
    env = dict(os.environ)
    env.pop('JARVIS_VAULT_PATH', None)
    env.pop('JARVIS_APPROVER_ID', None)
    env.pop('JARVIS_PROJECTS_CONFIG', None)
    env['JARVIS_DEVICE_ID'] = 'test-node'
    if vault is not None:
        env['JARVIS_VAULT_PATH'] = str(vault)
    if projects_config is not None:
        env['JARVIS_PROJECTS_CONFIG'] = str(projects_config)
    if approver_id is not None:
        env['JARVIS_APPROVER_ID'] = str(approver_id)
    if state_home is not None:
        env['XDG_STATE_HOME'] = str(state_home)
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jarvis_hermes', 'serve'], env=env)
    with tempfile.TemporaryFile(mode='w+') as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            kwargs = {}
            if elicit:
                async def callback(context, request):
                    if 'Simulated effect' not in request.message:
                        raise AssertionError('approval prompt must include action summary')
                    return ElicitResult(action=approval_action,
                                        content={} if approval_action == 'accept' else None)
                kwargs['elicitation_callback'] = callback
            async with ClientSession(read, write, read_timeout_seconds=timedelta(seconds=15),
                                     **kwargs) as session:
                await session.initialize()
                yield session


class MCPTests(unittest.IsolatedAsyncioTestCase):
    async def test_machine_status_through_real_mcp_transport(self):
        async with connected() as client:
            catalog = await client.list_tools()
            self.assertIn('device_status', [t.name for t in catalog.tools])
            result = await client.call_tool('device_status', {'device_id': 'test-node'})
            self.assertFalse(result.isError)
            data = result.structuredContent
            self.assertTrue(data['ok'])
            self.assertEqual(data['device_id'], 'test-node')
            self.assertIn('observed_at', data)
            self.assertGreater(data['data']['disk']['total_bytes'], 0)
            self.assertEqual(data['data']['integrations']['telegram'], 'not_verified')

    async def test_find_read_and_refresh_note_in_same_connection(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            note = vault / 'Viaggio.md'
            note.write_bytes(b'# Bali\r\nBudget 800 euro')
            async with connected(vault) as client:
                result = await client.call_tool('search_notes', {'query': 'BALI'})
                data = result.structuredContent
                self.assertTrue(data['ok'])
                self.assertEqual([n['note_id'] for n in data['data']['matches']], ['Viaggio.md'])
                first = await client.call_tool('read_note', {'note_id': 'Viaggio.md'})
                self.assertEqual(first.structuredContent['data']['content'], '# Bali\nBudget 800 euro')
                revision = first.structuredContent['data']['version']
                note.write_text('# Bali\nBudget 950 euro', encoding='utf-8')
                second = await client.call_tool('read_note', {'note_id': 'Viaggio.md'})
                self.assertIn('950', second.structuredContent['data']['content'])
                self.assertNotEqual(revision, second.structuredContent['data']['version'])
                self.assertNotIn(str(vault), str(second.structuredContent))
                self.assertEqual(sorted(p.name for p in vault.iterdir()), ['Viaggio.md'])

    async def test_note_access_cannot_escape_or_read_hidden_files(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vault = base / 'vault'
            vault.mkdir()
            (base / 'outside.md').write_text('OUTSIDE_CANARY', encoding='utf-8')
            (vault / '.secret.md').write_text('HIDDEN_CANARY', encoding='utf-8')
            (vault / 'plain.txt').write_text('NOT_A_NOTE', encoding='utf-8')
            async with connected(vault) as client:
                for note_id in ('../outside.md', str(base / 'outside.md'), '.secret.md', 'plain.txt', 'a\\outside.md', 'C:outside.md'):
                    result = await client.call_tool('read_note', {'note_id': note_id})
                    self.assertFalse(result.structuredContent['ok'], note_id)
                    self.assertNotIn('CANARY', str(result.structuredContent))
                result = await client.call_tool('search_notes', {'query': 'CANARY'})
                self.assertEqual(result.structuredContent['data']['matches'], [])

    async def test_pagination_limits_and_empty_search(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / 'Long.md').write_text('a' * 9000 + '\nneedle', encoding='utf-8')
            async with connected(vault) as client:
                first = (await client.call_tool('read_note', {'note_id': 'Long.md'})).structuredContent
                self.assertEqual(len(first['data']['content']), 8000)
                self.assertEqual(first['data']['next_offset'], 8000)
                second = (await client.call_tool('read_note', {'note_id': 'Long.md', 'offset': 8000})).structuredContent
                self.assertTrue(second['data']['content'].endswith('needle'))
                search = (await client.call_tool('search_notes', {'query': 'needle'})).structuredContent
                self.assertEqual(search['data']['matches'][0]['note_id'], 'Long.md')
                empty = (await client.call_tool('search_notes', {'query': 'absent'})).structuredContent
                self.assertEqual(empty['data']['matches'], [])
                for name, args in [('read_note', {'note_id': 'Long.md', 'offset': -1}), ('read_note', {'note_id': 'Long.md', 'limit': 9000}), ('search_notes', {'query': ''}), ('search_notes', {'query': 'a', 'limit': 30})]:
                    result = (await client.call_tool(name, args)).structuredContent
                    self.assertFalse(result['ok'])
                    self.assertEqual(result['error']['code'], 'INVALID_ARGUMENT')

    async def test_symlink_does_not_expose_external_note(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            vault = base / 'vault'
            vault.mkdir()
            (base / 'outside.md').write_text('OUTSIDE_CANARY', encoding='utf-8')
            try:
                (vault / 'link.md').symlink_to(base / 'outside.md')
            except OSError:
                self.skipTest('OS does not permit symlinks for this account')
            async with connected(vault) as client:
                result = (await client.call_tool('read_note', {'note_id': 'link.md'})).structuredContent
                self.assertFalse(result['ok'])
                result = (await client.call_tool('search_notes', {'query': 'CANARY'})).structuredContent
                self.assertEqual(result['data']['matches'], [])

    async def test_missing_configuration_and_wrong_device_are_explicit(self):
        async with connected() as client:
            note = (await client.call_tool('read_note', {'note_id': 'test.md'})).structuredContent
            self.assertEqual(note['error']['code'], 'NOT_CONFIGURED')
            machine = (await client.call_tool('device_status', {'device_id': 'another-node'})).structuredContent
            self.assertEqual(machine['error']['code'], 'DEVICE_NOT_FOUND')
            catalog = await client.list_tools()
            names = {t.name for t in catalog.tools}
            self.assertIn('simulate_with_approval', names)
            self.assertTrue(all(t.annotations.readOnlyHint for t in catalog.tools
                                if t.name not in ('simulate_with_approval', 'create_checkpoint', 'write_project_file', 'restore_checkpoint')))

    async def test_simulated_approval_records_once_and_counts_only_this_decision(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with connected(approver_id=424242, state_home=root) as client:
                tool = next(t for t in (await client.list_tools()).tools if t.name == 'simulate_with_approval')
                self.assertNotIn('actor_id', tool.inputSchema['properties'])
                first = (await client.call_tool('simulate_with_approval', {
                    'target': 'demo-target', 'arguments': {'message': 'hello'}})).structuredContent
                self.assertTrue(first['ok'], first)
                self.assertEqual(first['data']['status'], 'executed')
                self.assertEqual(first['data']['effects_recorded'], 1)
                self.assertEqual(first['data']['decision'], 'accept')
                self.assertNotIn('token', str(first))
                # A second confirmation is its own decision: it must not report the first
                # effect as if this call had produced it (#3 regression).
                second = (await client.call_tool('simulate_with_approval', {
                    'target': 'demo-target', 'arguments': {'message': 'hello'}})).structuredContent
                self.assertEqual(second['data']['effects_recorded'], 1)
            with closing(sqlite3.connect(root / 'jarvis-hermes' / 'approvals.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM simulated_effects').fetchone()[0], 2)

    async def test_declined_mcp_elicitation_records_no_simulated_effect(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with connected(approver_id=424242, approval_action='decline', state_home=root) as client:
                data = (await client.call_tool('simulate_with_approval', {
                    'target': 'demo-target', 'arguments': {'message': 'hello'}})).structuredContent
                self.assertTrue(data['ok'], data)
                self.assertEqual(data['data']['status'], 'cancelled')
                self.assertEqual(data['data']['effects_recorded'], 0)
                self.assertEqual(data['data']['decision'], 'decline')
            with closing(sqlite3.connect(root / 'jarvis-hermes' / 'approvals.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM simulated_effects').fetchone()[0], 0)

    async def test_client_without_elicitation_support_cannot_execute(self):
        # No callback means no consent surface: the server must fail closed, not assume
        # an affirmative answer because it could not ask (#3).
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            async with connected(approver_id=424242, state_home=root, elicit=False) as client:
                data = (await client.call_tool('simulate_with_approval', {
                    'target': 'demo-target', 'arguments': {'message': 'hello'}})).structuredContent
                self.assertTrue(data['ok'], data)
                self.assertEqual(data['data']['status'], 'cancelled')
                self.assertEqual(data['data']['effects_recorded'], 0)
                self.assertEqual(data['data']['decision'], 'unavailable')
            with closing(sqlite3.connect(root / 'jarvis-hermes' / 'approvals.sqlite3')) as db:
                self.assertEqual(db.execute('SELECT COUNT(*) FROM simulated_effects').fetchone()[0], 0)

    async def test_simulated_approval_fails_closed_without_configured_approver(self):
        async with connected() as client:
            data = (await client.call_tool('simulate_with_approval', {
                'target': 'demo-target', 'arguments': {'message': 'hello'}})).structuredContent
            self.assertFalse(data['ok'])
            self.assertEqual(data['error']['code'], 'APPROVER_NOT_CONFIGURED')

    async def test_disk_usage_is_timestamped_and_device_scoped(self):
        async with connected() as client:
            result = (await client.call_tool('disk_usage', {'device_id': 'test-node', 'request_id': 'disk-1'})).structuredContent
            self.assertTrue(result['ok'])
            self.assertEqual(result['request_id'], 'disk-1')
            self.assertGreater(result['data']['total_bytes'], 0)
            self.assertIn('observed_at', result)

    async def test_large_and_non_utf8_notes_are_reported_without_payload(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            vault = Path(directory)
            (vault / 'Huge.md').write_bytes(b'x' * 262145)
            (vault / 'Binary.md').write_bytes(b'\xff\xfe\xff')
            async with connected(vault) as client:
                huge = (await client.call_tool('read_note', {'note_id': 'Huge.md'})).structuredContent
                self.assertEqual(huge['error']['code'], 'NOTE_TOO_LARGE')
                binary = (await client.call_tool('read_note', {'note_id': 'Binary.md'})).structuredContent
                self.assertFalse(binary['ok'])
                search = (await client.call_tool('search_notes', {'query': 'x'})).structuredContent
                self.assertEqual(search['data']['matches'], [])
                self.assertEqual(search['data']['skipped_entries'], 2)

    async def test_read_project_file_mcp_tool(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            proj_dir = base / "my projects" / "fantavega"
            proj_dir.mkdir(parents=True)
            log_file = proj_dir / "service.log"
            log_file.write_text("info: started\nsecret_token: 'secret123456'\n", encoding="utf-8")
            
            cfg_file = base / "projects.json"
            cfg_file.write_text(f'''{{
                "devices": {{
                    "test-node": {{"environment": "wsl"}},
                    "other-node": {{"environment": "windows"}}
                }},
                "projects": {{
                    "fantavega": {{
                        "paths": {{
                            "test-node": "{proj_dir.as_posix()}"
                        }}
                    }}
                }}
            }}''', encoding="utf-8")

            async with connected(projects_config=cfg_file) as client:
                # 1. Success read with secret redaction
                res = (await client.call_tool('read_project_file', {
                    'project_id': 'fantavega',
                    'relative_path': 'service.log',
                    'device_id': 'test-node'
                })).structuredContent
                self.assertTrue(res['ok'])
                self.assertEqual(res['data']['project_id'], 'fantavega')
                self.assertIn("info: started", res['data']['content'])
                self.assertNotIn("secret123456", res['data']['content'])
                self.assertIn("[REDACTED]", res['data']['content'])

                # 2. File not found
                missing = (await client.call_tool('read_project_file', {
                    'project_id': 'fantavega',
                    'relative_path': 'nonexistent.log',
                    'device_id': 'test-node'
                })).structuredContent
                self.assertFalse(missing['ok'])
                self.assertEqual(missing['error']['code'], 'FILE_NOT_FOUND')

                # 3. Path traversal blocked
                traversal = (await client.call_tool('read_project_file', {
                    'project_id': 'fantavega',
                    'relative_path': '../projects.json',
                    'device_id': 'test-node'
                })).structuredContent
                self.assertFalse(traversal['ok'])
                self.assertEqual(traversal['error']['code'], 'PERMISSION_DENIED')

                # 4. Offline/unreachable device
                offline = (await client.call_tool('read_project_file', {
                    'project_id': 'fantavega',
                    'relative_path': 'service.log',
                    'device_id': 'other-node'
                })).structuredContent
                self.assertFalse(offline['ok'])
                self.assertEqual(offline['error']['code'], 'DEVICE_OFFLINE')

    async def test_checkpoint_write_and_restore_via_mcp(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_dir = base / "state"
            proj_dir = base / "proj"
            proj_dir.mkdir(parents=True)
            source_file = proj_dir / "calc.py"
            source_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")

            cfg_file = base / "projects.json"
            cfg_file.write_text(f'''{{
                "devices": {{"test-node": {{"environment": "wsl"}}}},
                "projects": {{
                    "my-calc": {{
                        "paths": {{"test-node": "{proj_dir.as_posix()}"}}
                    }}
                }}
            }}''', encoding="utf-8")

            async with connected(projects_config=cfg_file, state_home=state_dir) as client:
                # 1. Create checkpoint
                cp_res = (await client.call_tool('create_checkpoint', {
                    'job_id': 'job-42',
                    'project_id': 'my-calc',
                    'relative_path': 'calc.py',
                    'device_id': 'test-node'
                })).structuredContent
                self.assertTrue(cp_res['ok'])
                cp_id = cp_res['data']['checkpoint_id']
                init_hash = cp_res['data']['initial_hash']

                # 2. Safe write
                write_res = (await client.call_tool('write_project_file', {
                    'checkpoint_id': cp_id,
                    'project_id': 'my-calc',
                    'relative_path': 'calc.py',
                    'expected_initial_hash': init_hash,
                    'content': "def add(a, b): return a + b + 1\n",
                    'device_id': 'test-node'
                })).structuredContent
                self.assertTrue(write_res['ok'])
                self.assertEqual(source_file.read_text(encoding="utf-8"), "def add(a, b): return a + b + 1\n")

                # 3. Restore checkpoint
                restore_res = (await client.call_tool('restore_checkpoint', {
                    'checkpoint_id': cp_id,
                    'expected_job_id': 'job-42'
                })).structuredContent
                self.assertTrue(restore_res['ok'])
                self.assertEqual(source_file.read_text(encoding="utf-8"), "def add(a, b): return a + b\n")
                self.assertIn("-def add(a, b): return a + b + 1", restore_res['data']['diff'])

                # 4. Conflict detection on external edit
                cp_res2 = (await client.call_tool('create_checkpoint', {
                    'job_id': 'job-43',
                    'project_id': 'my-calc',
                    'relative_path': 'calc.py',
                    'device_id': 'test-node'
                })).structuredContent
                cp_id2 = cp_res2['data']['checkpoint_id']
                init_hash2 = cp_res2['data']['initial_hash']

                # External edit happens
                source_file.write_text("def add(a, b): return manual_edit(a, b)\n", encoding="utf-8")

                # Write fails with CONFLICT
                conflict_res = (await client.call_tool('write_project_file', {
                    'checkpoint_id': cp_id2,
                    'project_id': 'my-calc',
                    'relative_path': 'calc.py',
                    'expected_initial_hash': init_hash2,
                    'content': "def add(a, b): return 0\n",
                    'device_id': 'test-node'
                })).structuredContent
                self.assertFalse(conflict_res['ok'])
                self.assertEqual(conflict_res['error']['code'], 'CONFLICT')
                self.assertEqual(source_file.read_text(encoding="utf-8"), "def add(a, b): return manual_edit(a, b)\n")


