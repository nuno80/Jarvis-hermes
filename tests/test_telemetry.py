import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis_hermes.telemetry import RoutingDecisionStore
from jarvis_hermes.router import (
    FAST_PATH_RULES,
    ITALIAN_ROUTING_DATASET,
    IntentRoute,
    JevClient,
    RequestRouter,
    RouterError,
    RoutingTarget,
)


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.db_path = self.state_dir / "routing_decisions.sqlite3"
        self.store = RoutingDecisionStore(self.db_path, retention_days=30)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_created_for_every_routing_path(self):
        """Test record is created for deterministic, fallback, and Jev routing paths."""
        router = RequestRouter(telemetry_store=self.store)

        # 1. Deterministic path
        r_det = router.route("quanto spazio libero ho sul disco?")
        self.assertEqual(r_det.target, RoutingTarget.DETERMINISTIC)

        # 2. Fallback reasoning path
        r_fall = router.route("spiegami la differenza tra REST e MCP")
        self.assertEqual(r_fall.target, RoutingTarget.REASONING_LLM)

        # 3. Fallback clarification path
        r_clar = router.route("cancella tutto")
        self.assertEqual(r_clar.target, RoutingTarget.CLARIFICATION)

        # Verify all 3 decisions were recorded in SQLite
        decisions = self.store.get_decisions()
        self.assertEqual(len(decisions), 3)

        percorsi = {d["percorso"] for d in decisions}
        self.assertIn("deterministic", percorsi)
        self.assertIn("reasoning_llm", percorsi)
        self.assertIn("clarification", percorsi)

    def test_secrets_are_redacted_in_telemetry_logs(self):
        """Test secrets (passwords, tokens, api keys) are redacted in stored telemetry records."""
        secret_request = "deploy con password=SuperSecretPassword123 and token=ghp_abcdef1234567890"
        self.store.record_decision(
            request=secret_request,
            domande=["intent", "risk"],
            valori={"api_key": "sk-1234567890abcdef1234567890"},
            confidenze={"confidence": 0.95},
            percorso="tool_workflow",
            correzione_utente="usa password=AnotherSecretKey instead",
        )

        decisions = self.store.get_decisions()
        self.assertEqual(len(decisions), 1)
        rec = decisions[0]

        # Ensure no cleartext secret exists in request, values, or correction
        self.assertNotIn("SuperSecretPassword123", rec["request"])
        self.assertIn("[REDACTED]", rec["request"])

        self.assertNotIn("sk-1234567890abcdef1234567890", rec["valori"])
        self.assertIn("[REDACTED]", rec["valori"])

        self.assertNotIn("AnotherSecretKey", rec["correzione_utente"])
        self.assertIn("[REDACTED]", rec["correzione_utente"])

    def test_configurable_retention_and_cleanup(self):
        """Test retention cleanup purges records older than configured threshold."""
        # Insert old record
        with self.store._get_connection() as conn:
            conn.execute("""
                INSERT INTO routing_decisions (
                    request, domande, valori, confidenze, percorso, modello,
                    latenza_system_1_ms, latenza_first_msg_ms, tokens, costo,
                    esito, correzione_utente, created_at_utc
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                "old query", "[]", "{}", "{}", "deterministic", "rule",
                5.0, 10.0, 10, 0.0, "success", None, "2020-01-01T00:00:00+00:00"
            ))

        # Insert fresh record
        self.store.record_decision(request="recent query")

        self.assertEqual(len(self.store.get_decisions()), 2)

        # Cleanup records older than 30 days
        deleted = self.store.cleanup_old_records(retention_days=30)
        self.assertEqual(deleted, 1)

        remaining = self.store.get_decisions()
        self.assertEqual(len(remaining), 1)
        self.assertEqual(remaining[0]["request"], "recent query")

    def test_routing_report_metrics_and_user_corrections(self):
        """Test p50/p95 latency, escalation rate, path distribution, and user corrections."""
        # Record various decisions with controlled latencies
        self.store.record_decision(
            request="query fast",
            percorso="deterministic",
            latenza_system_1_ms=0.0,
            latenza_first_msg_ms=10.0,
        )
        dec_id = self.store.record_decision(
            request="query ambiguous",
            percorso="clarification",
            latenza_system_1_ms=100.0,
            latenza_first_msg_ms=120.0,
        )
        self.store.record_decision(
            request="query reasoning",
            percorso="reasoning_llm",
            latenza_system_1_ms=250.0,
            latenza_first_msg_ms=400.0,
            tokens=150,
            costo=0.0001,
        )

        # Record user correction
        self.store.record_correction(dec_id, "In realtà volevo cercare il meteo")

        report = self.store.generate_report()
        self.assertEqual(report["total_decisions"], 3)
        self.assertIn("deterministic", report["path_distribution"])
        self.assertIn("clarification", report["path_distribution"])
        self.assertIn("reasoning_llm", report["path_distribution"])

        # Escalations: clarification (1) + reasoning_llm (1) = 2 out of 3 = 66.7%
        self.assertAlmostEqual(report["escalation_rate"], 0.667, places=2)

        # Corrections count
        self.assertEqual(report["corrections_count"], 1)
        self.assertEqual(report["corrections"][0]["id"], dec_id)
        self.assertEqual(report["corrections"][0]["correzione_utente"], "In realtà volevo cercare il meteo")

        # Percentiles
        self.assertGreaterEqual(report["latency_system_1_p95_ms"], report["latency_system_1_p50_ms"])
        self.assertGreaterEqual(report["latency_first_msg_p95_ms"], report["latency_first_msg_p50_ms"])


if __name__ == "__main__":
    unittest.main()
