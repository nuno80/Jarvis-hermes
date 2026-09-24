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
async def connected(vault=None, *, approver_id=None, approval_action='accept', state_home=None, elicit=True):
    env = dict(os.environ)
    env.pop('JARVIS_VAULT_PATH', None)
    env.pop('JARVIS_APPROVER_ID', None)
    env['JARVIS_DEVICE_ID'] = 'test-node'
    if vault is not None:
        env['JARVIS_VAULT_PATH'] = str(vault)
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
                                if t.name not in ('simulate_with_approval', 'job_cancel')))

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

    async def test_job_lifecycle_mcp_tools(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            state_dir = Path(directory)
            from jarvis_hermes.jobs import JobStore
            # Pre-populate a job
            db_file = state_dir / 'jarvis-hermes' / 'jobs.sqlite3'
            store = JobStore(db_file)
            j = store.create_job(actor_id=123, target="test_task", description="test long task")
            jid = j['job_id']
            store.update_status(jid, 'running', step='executing phase 1', effects_count=1)

            async with connected(state_home=state_dir) as client:
                # 1. Query job status (note: on server startup, running jobs are reconciled to outcome_unknown)
                st = (await client.call_tool('job_status', {'job_id': jid})).structuredContent
                self.assertTrue(st['ok'])
                self.assertEqual(st['data']['status'], 'outcome_unknown')
                self.assertEqual(st['data']['last_step'], 'executing phase 1')
                self.assertEqual(st['data']['effects_count'], 1)
                self.assertIn('Node restarted during execution', st['data']['warnings'])

                # 2. Cancel non-existent job
                missing = (await client.call_tool('job_cancel', {'job_id': 'job-missing'})).structuredContent
                self.assertFalse(missing['ok'])
                self.assertEqual(missing['error']['code'], 'NOT_FOUND')

                # 3. Create a fresh running job through store and cancel via MCP
                j2 = store.create_job(actor_id=123, target="task2", description="second task")
                jid2 = j2['job_id']
                store.update_status(jid2, 'running', step='active step', effects_count=2)

                cancel_res = (await client.call_tool('job_cancel', {'job_id': jid2})).structuredContent
                self.assertTrue(cancel_res['ok'])
                self.assertEqual(cancel_res['data']['status'], 'cancelled')
                self.assertEqual(cancel_res['data']['last_completed_step'], 'active step')
                self.assertEqual(cancel_res['data']['unreverted_effects_count'], 2)
                self.assertTrue(cancel_res['data']['process_stopped'])

