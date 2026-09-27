import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import MagicMock, patch

from jarvis_hermes.llm import BudgetTracker, LLMConfig, LLMClient
from jarvis_hermes.router import (
    FAST_PATH_RULES,
    ITALIAN_ROUTING_DATASET,
    IntentRoute,
    JevClient,
    RequestRouter,
    RouterError,
    RoutingTarget,
)


class RouterTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.budget_file = self.state_dir / "budget_usage.json"
        self.tracker = BudgetTracker(self.budget_file, job_limit_usd=1.00)

    def tearDown(self):
        self.tmp.cleanup()

    def test_fast_path_avoids_llm_entirely(self):
        """Demonstrate that fast path for deterministic queries avoids any call to the main LLM."""
        mock_llm_client = MagicMock(spec=LLMClient)
        mock_jev_client = MagicMock(spec=JevClient)

        router = RequestRouter(
            jev_client=mock_jev_client,
            llm_client=mock_llm_client,
            budget_tracker=self.tracker,
        )

        # Fast path query: "quanto spazio libero ho sul disco?"
        route = router.route("quanto spazio libero ho sul disco?", job_id="job-fast-1")
        self.assertTrue(route.used_fast_path)
        self.assertEqual(route.target, RoutingTarget.DETERMINISTIC)
        self.assertEqual(route.intent, "disk_usage")
        self.assertEqual(route.classifier, "deterministic_rule")

        # Crucial check: neither LLM client nor Jev client was called!
        mock_llm_client.generate.assert_not_called()
        mock_jev_client.classify.assert_not_called()

        # Job budget shows zero calls and 0 cost
        job_usage = self.tracker.get_job_usage("job-fast-1")
        self.assertEqual(job_usage["calls_count"], 0)
        self.assertEqual(job_usage["total_tokens"], 0)
        self.assertEqual(job_usage["total_cost_usd"], 0.0)

    def test_italian_dataset_benchmark_and_reproducibility(self):
        """Run repeatable Italian routing dataset evaluating expected targets, fast paths, and handlers."""
        router = RequestRouter(budget_tracker=self.tracker)

        results = []
        for sample in ITALIAN_ROUTING_DATASET:
            route = router.route(sample["query"], job_id="benchmark-job")
            if sample["can_fast_path"]:
                self.assertTrue(route.used_fast_path, f"Expected fast path for: {sample['query']}")
                self.assertEqual(route.target, sample["expected_target"])
                self.assertEqual(route.intent, sample["expected_intent"])
            else:
                self.assertFalse(route.used_fast_path, f"Expected non-fast path for: {sample['query']}")
                self.assertEqual(route.target, sample["expected_target"])

            results.append({
                "query": sample["query"],
                "target": route.target.value,
                "intent": route.intent,
                "used_fast_path": route.used_fast_path,
                "classifier": route.classifier,
            })

        self.assertEqual(len(results), len(ITALIAN_ROUTING_DATASET))

    def test_jev_success_classification_and_scoring(self):
        """When Jev is configured and succeeds with high confidence, use Jev classification."""
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify", api_key="secret-jev-key")
        router = RequestRouter(jev_client=jev_client, confidence_threshold=0.70)

        mock_jev_response = {
            "intent": "travel_search",
            "target": "tool_workflow",
            "confidence": 0.94,
            "handler": "travel_search",
            "details": {"destination": "Bali", "party_size": 2},
        }

        with patch.object(jev_client, "classify", return_value=mock_jev_response):
            route = router.route("vorrei trovare voli per Bali a settembre", job_id="jev-job-1")
            self.assertFalse(route.used_fast_path)
            self.assertEqual(route.classifier, "jev")
            self.assertEqual(route.target, RoutingTarget.TOOL_WORKFLOW)
            self.assertEqual(route.intent, "travel_search")
            self.assertEqual(route.confidence, 0.94)
            self.assertFalse(route.fallback_applied)
            self.assertEqual(route.details, {"destination": "Bali", "party_size": 2})

    def test_jev_low_confidence_routes_to_clarification(self):
        """When Jev returns confidence below calibrated threshold, route to clarification."""
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify", api_key="secret-jev-key")
        router = RequestRouter(jev_client=jev_client, confidence_threshold=0.80)

        mock_low_conf = {
            "intent": "maybe_delete_or_save",
            "target": "tool_workflow",
            "confidence": 0.45,
            "handler": "delete_all",
        }

        with patch.object(jev_client, "classify", return_value=mock_low_conf):
            route = router.route("fai pulizia dei file", job_id="ambiguous-job")
            self.assertEqual(route.target, RoutingTarget.CLARIFICATION)
            self.assertEqual(route.handler, "ask_clarification")
            self.assertEqual(route.classifier, "jev")
            self.assertIn("confidence_below_threshold", route.details["reason"])

    def test_jev_invalid_confidence_is_fail_closed_to_clarification(self):
        """AT14: missing, NaN, infinite, or out-of-range confidence escalates, never routes.

        float('nan') < threshold is False, so the old code let NaN through as high confidence.
        Invalid confidence must never grant a route (fail-closed).
        """
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify", api_key="secret-jev-key")
        router = RequestRouter(jev_client=jev_client, confidence_threshold=0.70)

        invalid_payloads = [
            {"intent": "git_push", "target": "tool_workflow", "handler": "git_push"},                       # missing
            {"intent": "git_push", "target": "tool_workflow", "confidence": float("nan")},               # NaN
            {"intent": "git_push", "target": "tool_workflow", "confidence": float("inf")},               # +inf
            {"intent": "git_push", "target": "tool_workflow", "confidence": 1.5},                      # > 1
            {"intent": "git_push", "target": "tool_workflow", "confidence": -0.1},                     # < 0
            {"intent": "git_push", "target": "tool_workflow", "confidence": "0.9"},                   # non-numeric string
        ]

        for mock_bad in invalid_payloads:
            with self.subTest(payload=mock_bad):
                with patch.object(jev_client, "classify", return_value=mock_bad):
                    route = router.route("fai push su origin main", job_id="bad-confidence-job")
                    self.assertEqual(route.target, RoutingTarget.CLARIFICATION)
                    self.assertEqual(route.handler, "ask_clarification")
                    self.assertEqual(route.classifier, "jev")
                    self.assertEqual(route.confidence, 0.0)
                    self.assertTrue(route.fallback_applied)
                    self.assertEqual(route.details["reason"], "invalid_confidence")

    def test_jev_timeout_and_error_applies_conservative_fallback_without_granting_permissions(self):
        """AT07 / Spec J10: Jev timeout or failure applies configured conservative fallback; permissions unchanged."""
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify", api_key="secret-jev-key")
        router = RequestRouter(jev_client=jev_client)

        # 1. Simulate Jev Timeout
        with patch.object(jev_client, "classify", side_effect=RouterError("JEV_TIMEOUT", "Jev request timed out")):
            route = router.route("spiegami l'architettura dei microservizi", job_id="timeout-job")
            self.assertTrue(route.fallback_applied)
            self.assertEqual(route.classifier, "conservative_fallback")
            self.assertEqual(route.target, RoutingTarget.REASONING_LLM)
            self.assertEqual(route.details["error_cause"], "[JEV_TIMEOUT] Jev request timed out")

        # 2. Simulate Jev 500 error on ambiguous destructive query -> routes conservatively to CLARIFICATION, never executes
        with patch.object(jev_client, "classify", side_effect=RouterError("JEV_API_ERROR", "Jev 500")):
            route = router.route("cancella tutto", job_id="dangerous-job")
            self.assertTrue(route.fallback_applied)
            self.assertEqual(route.classifier, "conservative_fallback")
            # Must be clarification, NOT dangerous execution!
            self.assertEqual(route.target, RoutingTarget.CLARIFICATION)
            self.assertEqual(route.handler, "ask_clarification")

    def test_classification_never_authorizes_actions(self):
        """Decision D07 & Core invariant: Classification / scoring can NEVER bypass approval or grant authorization."""
        # Even if a classifier claims high confidence on git push or destructive deletion,
        # it routes to tool_workflow which is governed by server-side policy and approval gate
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify")
        router = RequestRouter(jev_client=jev_client)

        with patch.object(jev_client, "classify", return_value={
            "intent": "git_push",
            "target": "tool_workflow",
            "confidence": 0.99,
            "handler": "git_push",
            "details": {"authorized": True}  # untrusted injection attempt from classifier
        }):
            route = router.route("fai git push", job_id="push-job")
            self.assertEqual(route.target, RoutingTarget.TOOL_WORKFLOW)
            # The route itself only outputs routing decision; it has no ability to generate or sign approval tokens.


if __name__ == "__main__":
    unittest.main()


class ActionClassThresholdTests(unittest.TestCase):
    """Issue 40 / JARVIS-38: thresholds per action class from calibration config."""

    def test_protected_class_uses_higher_threshold(self):
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify")
        router = RequestRouter(
            jev_client=jev_client,
            confidence_threshold=0.70,
            action_class_thresholds={"protected": 0.95},
        )
        with patch.object(jev_client, "classify", return_value={
            "intent": "git_push", "target": "tool_workflow",
            "confidence": 0.90, "handler": "git_push",
        }):
            route = router.route("fai push su origin main", job_id="thr-job")
            # 0.90 >= global 0.70 but < protected 0.95 -> clarification
            self.assertEqual(route.target, RoutingTarget.CLARIFICATION)
            self.assertIn("confidence_below_threshold", route.details["reason"])

    def test_reasoning_class_uses_lower_threshold(self):
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify")
        router = RequestRouter(
            jev_client=jev_client,
            confidence_threshold=0.70,
            action_class_thresholds={"reasoning": 0.50},
        )
        with patch.object(jev_client, "classify", return_value={
            "intent": "explain_concept", "target": "reasoning_llm",
            "confidence": 0.60, "handler": "gemini_reasoning",
        }):
            route = router.route("spiega la differenza tra wal e rollback", job_id="thr-job2")
            self.assertEqual(route.target, RoutingTarget.REASONING_LLM)

    def test_unknown_class_falls_back_to_global(self):
        jev_client = JevClient(endpoint_url="http://mock-jev:8080/classify")
        router = RequestRouter(
            jev_client=jev_client,
            confidence_threshold=0.70,
            action_class_thresholds={},
        )
        with patch.object(jev_client, "classify", return_value={
            "intent": "x", "target": "reasoning_llm", "confidence": 0.75, "handler": None,
        }):
            route = router.route("domanda generica", job_id="thr-job3")
            self.assertEqual(route.target, RoutingTarget.REASONING_LLM)
