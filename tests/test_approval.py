"""Acceptance tests for the simulated effect at the trusted gateway seam."""
import concurrent.futures
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from jarvis_hermes.approval import ApprovalError, ApprovalStore


class ApprovalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = 1_800_000_000
        self.path = Path(self.temp.name) / 'approvals.sqlite3'
        self.store = ApprovalStore(self.path, clock=lambda: self.now)
        self.action = dict(actor_id=123456, target='demo-target', arguments={'message': 'hello'})

    def request(self, **changes):
        return self.store.request(**(self.action | changes), ttl_seconds=90)

    def decide(self, token, **changes):
        return self.store.decide(token=token, approve=True, **(self.action | changes))

    def test_exact_confirmation_persists_and_executes_once(self):
        pending = self.request()
        self.assertEqual(pending['status'], 'pending')
        self.assertEqual(pending['expires_at'], self.now + 90)
        self.assertEqual(len(pending['digest']), 64)
        self.assertIn('demo-target', pending['summary'])
        self.assertIn('hello', pending['summary'])
        reopened = ApprovalStore(self.path, clock=lambda: self.now)
        self.assertEqual(reopened.decide(token=pending['token'], approve=True, **self.action)['status'], 'executed')
        with self.assertRaises(ApprovalError) as caught:
            self.decide(pending['token'])
        self.assertEqual(caught.exception.code, 'APPROVAL_USED')
        self.assertEqual(reopened.effects(), [{'target': 'demo-target', 'arguments': {'message': 'hello'}}])

    def test_wrong_actor_or_changed_parameters_cannot_execute(self):
        pending = self.request()
        for changes in ({'actor_id': 999}, {'target': 'another-target'}, {'arguments': {'message': 'changed'}},
                        {'arguments': {'message': 'hello', 'approved': True}}):
            with self.assertRaises(ApprovalError) as caught:
                self.decide(pending['token'], **changes)
            self.assertEqual(caught.exception.code, 'APPROVAL_DENIED')
        self.assertEqual(self.store.effects(), [])
        self.assertEqual(self.decide(pending['token'])['status'], 'executed')

    def test_cancel_and_expiry_are_terminal(self):
        cancelled = self.request()
        self.assertEqual(self.store.decide(token=cancelled['token'], approve=False, **self.action)['status'], 'cancelled')
        with self.assertRaises(ApprovalError) as caught:
            self.decide(cancelled['token'])
        self.assertEqual(caught.exception.code, 'APPROVAL_USED')
        expired = self.request(target='expires')
        self.now += 90
        with self.assertRaises(ApprovalError) as caught:
            self.decide(expired['token'], target='expires')
        self.assertEqual(caught.exception.code, 'APPROVAL_EXPIRED')
        self.assertEqual(self.store.effects(), [])

    def test_concurrent_duplicate_callback_records_one_effect(self):
        pending = self.request()
        def confirm(_):
            try:
                return self.decide(pending['token'])['status']
            except ApprovalError as exc:
                return exc.code
        with concurrent.futures.ThreadPoolExecutor(max_workers=8) as pool:
            results = list(pool.map(confirm, range(8)))
        self.assertEqual(results.count('executed'), 1)
        self.assertEqual(results.count('APPROVAL_USED'), 7)
        self.assertEqual(len(self.store.effects()), 1)

    def test_invalid_token_and_invalid_requests(self):
        with self.assertRaises(ApprovalError):
            self.decide('not-a-valid-token')
        for changes in ({'actor_id': 'username'}, {'actor_id': 0}, {'target': ''},
                        {'arguments': {'bad': float('nan')}}):
            with self.assertRaises((ApprovalError, ValueError, TypeError)):
                self.request(**changes)
        with self.assertRaises(ApprovalError):
            self.store.request(**self.action, ttl_seconds=0)
        self.assertEqual(self.store.effects(), [])

    def test_public_demo_has_one_simulated_effect_and_no_token(self):
        run = subprocess.run([sys.executable, '-m', 'jarvis_hermes', 'approval-demo'],
                             capture_output=True, text=True, check=True)
        result = json.loads(run.stdout)
        self.assertEqual(result['first'], 'executed')
        self.assertEqual(result['replay'], 'APPROVAL_USED')
        self.assertEqual(result['effects_recorded'], 1)
        self.assertEqual(result['telegram'], 'not_connected')
        self.assertNotIn('token', run.stdout)


if __name__ == '__main__':
    unittest.main()
