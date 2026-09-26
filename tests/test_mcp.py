import os
import sqlite3
import sys
import tempfile
import unittest
from contextlib import asynccontextmanager, closing
from datetime import timedelta
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
from mcp.types import ElicitResult


@asynccontextmanager
async def connected(vault=None, *, approver_id=None, approval_action='accept', state_home=None, elicit=True, projects_config=None, extra_env=None):
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
    if extra_env:
        env.update(extra_env)
    params = StdioServerParameters(command=sys.executable, args=['-m', 'jarvis_hermes', 'serve'], env=env)
    with tempfile.TemporaryFile(mode='w+') as errors:
        async with stdio_client(params, errlog=errors) as (read, write):
            kwargs = {}
            if elicit:
                async def callback(context, request):
                    if 'Simulated effect' not in request.message and 'autorizzi il push' not in request.message and 'autorizzi l\'esecuzione del comando' not in request.message and 'autorizzi l\'invio esterno' not in request.message:
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
                                if t.name not in ('simulate_with_approval', 'git_push', 'job_cancel', 'create_checkpoint', 'write_project_file', 'restore_checkpoint', 'commit_project_changes', 'execute_gui_action', 'run_command', 'submit_web_form', 'update_preference', 'propose_memory', 'forget_memory', 'write_note')))

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
                    'job_id': 'job-42',
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
                    'job_id': 'job-43',
                    'relative_path': 'calc.py',
                    'expected_initial_hash': init_hash2,
                    'content': "def add(a, b): return 0\n",
                    'device_id': 'test-node'
                })).structuredContent
                self.assertFalse(conflict_res['ok'])
                self.assertEqual(conflict_res['error']['code'], 'CONFLICT')
                self.assertEqual(source_file.read_text(encoding="utf-8"), "def add(a, b): return manual_edit(a, b)\n")

                # 5. MCP write with invalid checkpoint -> CHECKPOINT_REQUIRED
                invalid_cp_res = (await client.call_tool('write_project_file', {
                    'checkpoint_id': 'cp-fake',
                    'project_id': 'my-calc',
                    'job_id': 'job-43',
                    'relative_path': 'calc.py',
                    'expected_initial_hash': init_hash2,
                    'content': "def add(a, b): return -1\n",
                    'device_id': 'test-node'
                })).structuredContent
                self.assertFalse(invalid_cp_res['ok'])
                self.assertEqual(invalid_cp_res['error']['code'], 'CHECKPOINT_REQUIRED')

    async def test_gemini_tools_via_mcp(self):
        from unittest.mock import patch
        async with connected(extra_env={"GEMINI_API_KEY": "test-key-mock"}) as client:
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            self.assertIn("ask_gemini", names)
            self.assertIn("get_job_budget_usage", names)

            # Test invalid model rejected
            bad_model_res = await client.call_tool("ask_gemini", {
                "prompt": "Valuta opzioni",
                "model_id": "unsupported-model-x",
                "job_id": "mcp-test-job"
            })
            self.assertFalse(bad_model_res.structuredContent["ok"])
            self.assertEqual(bad_model_res.structuredContent["error"]["code"], "INVALID_MODEL")

    async def test_route_request_via_mcp(self):
        async with connected() as client:
            tools = await client.list_tools()
            names = [t.name for t in tools.tools]
            self.assertIn("route_request", names)

            # Test fast-path routing
            res_fast = await client.call_tool("route_request", {
                "query": "quanto spazio libero ho sul disco?",
                "job_id": "route-job-1"
            })
            self.assertTrue(res_fast.structuredContent["ok"])
            data_fast = res_fast.structuredContent["data"]
            self.assertTrue(data_fast["used_fast_path"])
            self.assertEqual(data_fast["target"], "deterministic")
            self.assertEqual(data_fast["intent"], "disk_usage")

            # Test fallback routing for reasoning query
            res_fallback = await client.call_tool("route_request", {
                "query": "spiegami la differenza tra modelli SLM e LLM",
                "job_id": "route-job-2"
            })
            self.assertTrue(res_fallback.structuredContent["ok"])
            data_fallback = res_fallback.structuredContent["data"]
            self.assertFalse(data_fallback["used_fast_path"])
            self.assertEqual(data_fallback["target"], "reasoning_llm")
            self.assertTrue(data_fallback["fallback_applied"])

    async def test_workflow_and_commit_via_mcp(self):
        import subprocess
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_dir = base / "state"
            proj_dir = base / "repo"
            proj_dir.mkdir(parents=True)
            subprocess.run(["git", "init"], cwd=proj_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=proj_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=proj_dir, check=True)

            code_file = proj_dir / "calc.py"
            code_file.write_text("def add(a, b): return a + b\n", encoding="utf-8")
            test_file = proj_dir / "test_calc.py"
            test_file.write_text("import unittest\nfrom calc import add\nclass T(unittest.TestCase):\n    def test_add(self): self.assertEqual(add(1, 2), 3)\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=proj_dir, check=True)
            subprocess.run(["git", "commit", "-m", "init"], cwd=proj_dir, check=True)

            cfg_file = base / "projects.json"
            cfg_file.write_text(f'''{{
                "devices": {{"test-node": {{"environment": "wsl"}}}},
                "projects": {{
                    "my-calc": {{
                        "paths": {{"test-node": "{proj_dir.as_posix()}"}},
                        "workflows": {{
                            "test": {{
                                "command": ["python3", "-m", "unittest", "discover", "-s", "."]
                            }}
                        }}
                    }}
                }}
            }}''', encoding="utf-8")

            async with connected(projects_config=cfg_file, state_home=state_dir) as client:
                # 1. Run workflow -> success
                wf_res = (await client.call_tool('run_project_workflow', {
                    'project_id': 'my-calc',
                    'workflow_name': 'test',
                    'device_id': 'test-node'
                })).structuredContent
                self.assertTrue(wf_res['ok'])
                self.assertTrue(wf_res['data']['ok'])
                self.assertEqual(wf_res['data']['exit_code'], 0)

                # 2. Modify calc.py
                code_file.write_text("def add(a, b): return a + b  # updated\n", encoding="utf-8")

                # 3. Commit changes via commit_project_changes tool
                commit_res = (await client.call_tool('commit_project_changes', {
                    'project_id': 'my-calc',
                    'files': ['calc.py'],
                    'commit_message': 'Add comment in calc.py',
                    'verification': {'workflow': 'test', 'passed': True},
                    'device_id': 'test-node'
                })).structuredContent
                self.assertTrue(commit_res['ok'])
                self.assertTrue(commit_res['data']['committed'])
                self.assertTrue(commit_res['data']['commit_hash'])
                self.assertEqual(commit_res['data']['files'], ['calc.py'])

    async def test_git_push_via_mcp_with_approval(self):
        import subprocess
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_dir = base / "state"
            remote_dir = base / "remote.git"
            subprocess.run(["git", "init", "--bare", str(remote_dir)], check=True, capture_output=True)

            proj_dir = base / "repo"
            proj_dir.mkdir(parents=True)
            subprocess.run(["git", "init", "-b", "main"], cwd=proj_dir, check=True, capture_output=True)
            subprocess.run(["git", "config", "user.name", "Tester"], cwd=proj_dir, check=True)
            subprocess.run(["git", "config", "user.email", "tester@example.com"], cwd=proj_dir, check=True)
            subprocess.run(["git", "remote", "add", "origin", str(remote_dir)], cwd=proj_dir, check=True)

            file1 = proj_dir / "app.py"
            file1.write_text("print('hello')\n", encoding="utf-8")
            subprocess.run(["git", "add", "."], cwd=proj_dir, check=True)
            subprocess.run(["git", "commit", "-m", "first commit"], cwd=proj_dir, check=True)
            commit_hash = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj_dir, capture_output=True, text=True, check=True).stdout.strip()

            cfg_file = base / "projects.json"
            cfg_file.write_text(f'''{{
                "devices": {{"test-node": {{"environment": "wsl"}}}},
                "projects": {{
                    "my-app": {{
                        "paths": {{"test-node": "{proj_dir.as_posix()}"}},
                        "allowed_remotes": ["origin"],
                        "allowed_branches": ["main"]
                    }}
                }}
            }}''', encoding="utf-8")

            # 1. Successful push with approval
            async with connected(approver_id=123456, approval_action='accept',
                                 projects_config=cfg_file, state_home=state_dir) as client:
                catalog = await client.list_tools()
                self.assertIn('git_push', [t.name for t in catalog.tools])

                res = (await client.call_tool('git_push', {
                    'project_id': 'my-app',
                    'remote': 'origin',
                    'branch': 'main',
                    'commit_hash': commit_hash,
                    'device_id': 'test-node'
                })).structuredContent

                self.assertTrue(res['ok'])
                self.assertTrue(res['data']['pushed'])
                self.assertTrue(res['data']['remote_verified'])
                self.assertEqual(res['data']['commit_hash'], commit_hash)

                # Verify bare remote has commit
                rev = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=remote_dir, capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(rev, commit_hash)

            # 2. Denied push does not push new commit
            file1.write_text("print('hello world 2')\n", encoding="utf-8")
            subprocess.run(["git", "commit", "-am", "second commit"], cwd=proj_dir, check=True)
            commit_hash2 = subprocess.run(["git", "rev-parse", "HEAD"], cwd=proj_dir, capture_output=True, text=True, check=True).stdout.strip()

            async with connected(approver_id=123456, approval_action='decline',
                                 projects_config=cfg_file, state_home=state_dir) as client:
                res_deny = (await client.call_tool('git_push', {
                    'project_id': 'my-app',
                    'remote': 'origin',
                    'branch': 'main',
                    'commit_hash': commit_hash2,
                    'device_id': 'test-node'
                })).structuredContent

                self.assertFalse(res_deny['ok'])
                self.assertEqual(res_deny['error']['code'], 'APPROVAL_DECLINED')

                # Remote still at commit 1
                rev = subprocess.run(["git", "rev-parse", "refs/heads/main"], cwd=remote_dir, capture_output=True, text=True, check=True).stdout.strip()
                self.assertEqual(rev, commit_hash)

    async def test_gui_tools_via_mcp(self):
        async with connected() as client:
            catalog = await client.list_tools()
            names = {t.name for t in catalog.tools}
            self.assertIn('gui_status', names)
            self.assertIn('execute_gui_action', names)

            # gui_status check
            status_res = (await client.call_tool('gui_status', {})).structuredContent
            self.assertTrue(status_res['ok'])
            self.assertIn('session_state', status_res['data'])

            # execute_gui_action with unauthorized app rejected
            bad_res = (await client.call_tool('execute_gui_action', {'app_name': 'cmd.exe'})).structuredContent
            self.assertFalse(bad_res['ok'])
            self.assertEqual(bad_res['error']['code'], 'PERMISSION_DENIED')

    async def test_run_command_via_mcp(self):
        from pathlib import Path
        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_dir = base / "state"
            async with connected(approver_id=424242, state_home=state_dir) as client:
                catalog = await client.list_tools()
                names = {t.name for t in catalog.tools}
                self.assertIn('run_command', names)

                # 1. Read-only command executes automatically
                res_ro = (await client.call_tool('run_command', {'command': 'echo hello-jarvis'})).structuredContent
                self.assertTrue(res_ro['ok'])
                self.assertEqual(res_ro['data']['category'], 'READONLY')
                self.assertFalse(res_ro['data']['requires_approval'])
                self.assertIn('hello-jarvis', res_ro['data']['output'])

                # 2. Protected target (e.g. credentials, secret) is rejected unconditionally
                res_sec = (await client.call_tool('run_command', {'command': 'cat /etc/shadow'})).structuredContent
                self.assertFalse(res_sec['ok'])
                self.assertEqual(res_sec['error']['code'], 'PROTECTED_TARGET_DENIED')

                # 3. Privileged maintenance or unparseable composition triggers elicitation
                # Mock elicitation accept
                client.elicit_response = 'accept'
                res_maint = (await client.call_tool('run_command', {'command': 'echo restart > /dev/null; echo test'})).structuredContent
                self.assertTrue(res_maint['ok'])
                self.assertEqual(res_maint['data']['category'], 'UNPARSEABLE_COMPLEX')
                self.assertTrue(res_maint['data']['requires_approval'])

    async def test_web_tools_via_mcp(self):
        import http.server
        import socketserver
        import threading
        from pathlib import Path
        from urllib.parse import parse_qs

        posts = []
        class WebHandler(http.server.BaseHTTPRequestHandler):
            def log_message(self, format, *args):
                pass
            def do_GET(self):
                body = """<!DOCTYPE html><html><head><title>Test Page</title></head><body>
                    <h1>Test Page</h1>
                    <form action="/submit" method="POST">
                        <input type="text" name="title" value="Hello" />
                    </form>
                </body></html>"""
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body.encode("utf-8"))))
                self.end_headers()
                self.wfile.write(body.encode("utf-8"))
            def do_POST(self):
                clen = int(self.headers.get("Content-Length", 0))
                posts.append(self.rfile.read(clen).decode("utf-8"))
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"OK")

        class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
            daemon_threads = True

        server = ThreadedServer(("127.0.0.1", 0), WebHandler)
        port = server.server_address[1]
        t = threading.Thread(target=server.serve_forever, daemon=True)
        t.start()

        with tempfile.TemporaryDirectory() as directory:
            base = Path(directory)
            state_dir = base / "state"

            # 1. Read web page via MCP tool
            async with connected(approver_id=12345, state_home=state_dir) as client:
                catalog = await client.list_tools()
                names = {t.name for t in catalog.tools}
                self.assertIn('read_web_page', names)
                self.assertIn('submit_web_form', names)

                read_res = (await client.call_tool('read_web_page', {
                    'url': f'http://127.0.0.1:{port}/'
                })).structuredContent
                self.assertTrue(read_res['ok'])
                self.assertIn('Test Page', read_res['data']['title'])
                self.assertEqual(len(read_res['data']['forms']), 1)

            # 2. Submit web form with approval
            async with connected(approver_id=12345, approval_action='accept', state_home=state_dir) as client:
                submit_res = (await client.call_tool('submit_web_form', {
                    'action_url': f'http://127.0.0.1:{port}/submit',
                    'method': 'POST',
                    'fields': {'title': 'Updated Title'}
                })).structuredContent
                self.assertTrue(submit_res['ok'])
                self.assertTrue(submit_res['data']['submitted'])
                self.assertEqual(len(posts), 1)
                self.assertIn('title=Updated+Title', posts[0])

            # 3. Submit web form with decline
            async with connected(approver_id=12345, approval_action='decline', state_home=state_dir) as client:
                deny_res = (await client.call_tool('submit_web_form', {
                    'action_url': f'http://127.0.0.1:{port}/submit',
                    'method': 'POST',
                    'fields': {'title': 'Should Not Arrive'}
                })).structuredContent
                self.assertFalse(deny_res['ok'])
                self.assertEqual(deny_res['error']['code'], 'APPROVAL_DECLINED')
                self.assertEqual(len(posts), 1)

        server.shutdown()
        server.server_close()

    async def test_preferences_and_profile_mcp_tools(self):
        with tempfile.TemporaryDirectory() as directory:
            vault_dir = Path(directory)
            pref_file = vault_dir / "preferences.yaml"
            pref_file.write_text("travel:\n  max_budget: 800\n", encoding="utf-8")

            async with connected(vault=vault_dir) as client:
                catalog = await client.list_tools()
                names = {t.name for t in catalog.tools}
                self.assertIn('get_profile', names)
                self.assertIn('update_preference', names)

                # 1. Read profile
                p1 = (await client.call_tool('get_profile', {})).structuredContent
                self.assertTrue(p1['ok'])
                self.assertEqual(p1['data']['explicit_preferences']['travel']['max_budget'], 800)
                v1 = p1['data']['preferences_version']

                # 2. Modify preferences.yaml externally (e.g. user edits file during open session)
                pref_file.write_text("travel:\n  max_budget: 950\n", encoding="utf-8")

                # Fresh get_profile immediately reflects the update
                p2 = (await client.call_tool('get_profile', {})).structuredContent
                self.assertTrue(p2['ok'])
                self.assertEqual(p2['data']['explicit_preferences']['travel']['max_budget'], 950)
                v2 = p2['data']['preferences_version']
                self.assertNotEqual(v1, v2)

                # 3. Update preference via MCP tool with expected_version conflict check
                # Using outdated version v1 must fail
                conflict = (await client.call_tool('update_preference', {
                    'key_path': 'travel.max_budget',
                    'value': 1000,
                    'expected_version': v1
                })).structuredContent
                self.assertFalse(conflict['ok'])
                self.assertEqual(conflict['error']['code'], 'CONFLICT')

                # Using current version v2 succeeds
                upd = (await client.call_tool('update_preference', {
                    'key_path': 'travel.max_budget',
                    'value': 1000,
                    'expected_version': v2
                })).structuredContent
                self.assertTrue(upd['ok'])
                self.assertEqual(upd['data']['value'], 1000)

                # 4. Attempting to set permissions/security fails closed
                sec = (await client.call_tool('update_preference', {
                    'key_path': 'permissions.allow_all',
                    'value': True
                })).structuredContent
                self.assertFalse(sec['ok'])
                self.assertEqual(sec['error']['code'], 'PERMISSION_DENIED')

                # 5. Inferred candidate memory tools: propose_memory, forget_memory, write_note
                self.assertIn('propose_memory', names)
                self.assertIn('forget_memory', names)
                self.assertIn('write_note', names)

                # Propose candidate memory
                prop = (await client.call_tool('propose_memory', {
                    'domain': 'travel',
                    'key': 'departure_city',
                    'value': 'FCO',
                    'evidence': 'User booked flight departing from FCO',
                    'confidence': 0.75
                })).structuredContent
                self.assertTrue(prop['ok'])
                self.assertEqual(prop['data']['status'], 'candidate')
                self.assertEqual(prop['data']['value'], 'FCO')
                note_id = prop['data']['note_id']

                # Profile now includes candidate note
                p3 = (await client.call_tool('get_profile', {})).structuredContent
                self.assertTrue(p3['ok'])
                cand_notes = [n for n in p3['data']['frontmatter_notes'] if n['note_id'] == note_id]
                self.assertEqual(len(cand_notes), 1)
                self.assertEqual(cand_notes[0]['status'], 'candidate')

                # Concurrent write_note conflict check
                note_v1 = prop['data']['version']
                wn_conflict = (await client.call_tool('write_note', {
                    'note_id': note_id,
                    'content': '# Custom text',
                    'expected_version': 'stale_version'
                })).structuredContent
                self.assertFalse(wn_conflict['ok'])
                self.assertEqual(wn_conflict['error']['code'], 'CONFLICT')

                # Forget memory removes note and creates backup
                del_res = (await client.call_tool('forget_memory', {
                    'note_id': note_id,
                    'expected_version': note_v1
                })).structuredContent
                self.assertTrue(del_res['ok'])
                self.assertEqual(del_res['data']['action'], 'deleted')
                self.assertTrue((vault_dir / del_res['data']['backup_retained']).is_file())





