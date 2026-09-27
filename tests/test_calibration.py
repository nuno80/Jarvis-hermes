"""Calibration harness tests (issue 40 / JARVIS-38, Appendix I2, AT13/AT14)."""
import json
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from jarvis_hermes.calibration import (
    DatasetError,
    expected_action_class,
    load_dataset,
    protected_fast_path_errors,
    run_calibration,
)
from jarvis_hermes.router import RoutingTarget

DATA = Path(__file__).parent / "data" / "routing_it.jsonl"


class LoadDatasetTests(unittest.TestCase):
    def test_dataset_has_at_least_200_examples(self):
        rows = load_dataset(DATA)
        self.assertGreaterEqual(len(rows), 200)

    def test_dataset_labels_cover_appendix_i1_questions(self):
        required = {
            "intent", "handler", "skill_hint", "needs_clarification",
            "external_effect", "private_data", "risk", "complexity",
        }
        for row in load_dataset(DATA):
            missing = required - row.keys()
            self.assertFalse(missing, f"missing I1 labels {missing} in {row['query']}")

    def test_rejects_rows_missing_query_or_expected_target(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "bad.jsonl"
            p.write_text(json.dumps({"query": "ciao"}) + "\n", encoding="utf-8")
            with self.assertRaises(DatasetError):
                load_dataset(p)

    def test_rejects_empty_dataset(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "empty.jsonl"
            p.touch()
            with self.assertRaises(DatasetError):
                load_dataset(p)


class SplitTests(unittest.TestCase):
    def test_split_is_deterministic_and_disjoint(self):
        from jarvis_hermes.calibration import split_calibration_test
        rows = load_dataset(DATA)
        cal, test = split_calibration_test(rows, test_fraction=0.25, seed=42)
        self.assertGreater(len(cal), 0)
        self.assertGreater(len(test), 0)
        self.assertEqual(len(cal) + len(test), len(rows))
        cal_keys = {(r["query"], r["intent"]) for r in cal}
        test_keys = {(r["query"], r["intent"]) for r in test}
        self.assertEqual(cal_keys & test_keys, set())
        # Deterministic: same seed -> same split
        cal2, test2 = split_calibration_test(rows, test_fraction=0.25, seed=42)
        self.assertEqual(cal_keys, {(r["query"], r["intent"]) for r in cal2})

    def test_split_has_all_action_classes_in_both_sets(self):
        from jarvis_hermes.calibration import split_calibration_test
        rows = load_dataset(DATA)
        cal, test = split_calibration_test(rows, test_fraction=0.25, seed=42)
        for subset in (cal, test):
            self.assertEqual(
                {expected_action_class(r) for r in subset},
                {"readonly", "protected", "reasoning", "clarify"},
            )


class ExpectedActionClassTests(unittest.TestCase):
    def test_classification_map(self):
        self.assertEqual(expected_action_class({"external_effect": True, "risk": 5}), "protected")
        self.assertEqual(expected_action_class({"external_effect": False, "risk": 1}), "readonly")
        self.assertEqual(expected_action_class({"expected_target": "reasoning_llm"}), "reasoning")
        self.assertEqual(expected_action_class({"expected_target": "clarification"}), "clarify")


class ProtectedFastPathTests(unittest.TestCase):
    """AT13: a protected action must never be fast-pathed by S1 alone."""

    def _rows(self):
        return [
            {"query": "fai push", "external_effect": True, "risk": 4, "expected_target": "tool_workflow", "can_fast_path": False},
            {"query": "spazio disco", "external_effect": False, "risk": 1, "expected_target": "deterministic", "can_fast_path": True},
        ]

    def test_counts_protected_rows_fast_pathed(self):
        # Pseudo-router fast-paths everything with confidence 1.0
        class FakeRouter:
            def route(self, text, job_id="calibration"):
                r = MagicMock()
                r.used_fast_path = True
                r.confidence = 1.0
                return r

        errors = protected_fast_path_errors(self._rows(), FakeRouter())
        self.assertEqual(len(errors), 1)
        self.assertEqual(errors[0]["query"], "fai push")

    def test_zero_on_correct_router(self):
        from jarvis_hermes.router import RequestRouter
        errors = protected_fast_path_errors(self._rows(), RequestRouter())
        self.assertEqual(errors, [])


class MetricsTests(unittest.TestCase):
    def _pseudo_router(self):
        """Router that answers the I1 questions deterministically from the query text."""

        def route(text, job_id="calibration"):
            r = MagicMock()
            r.used_fast_path = "spazio" in text or "uptime" in text
            r.confidence = 0.95 if r.used_fast_path else 0.60
            answers = {
                "intent": "disk_usage" if r.used_fast_path else "other",
                "handler": "disk_usage" if r.used_fast_path else "unknown",
                "skill_hint": "system_status" if r.used_fast_path else "none",
                "needs_clarification": not r.used_fast_path,
                "external_effect": False,
                "private_data": False,
                "risk": 1.0 if r.used_fast_path else 3.0,
                "complexity": 1.0 if r.used_fast_path else 3.0,
            }
            r.details = {"answers": answers}
            return r

        return route

    def test_accuracy_and_ece_computed_per_question(self):
        rows = load_dataset(DATA)
        rows = [r for r in rows if "spazio" in r["query"] or "push" in r["query"]][:10]
        result = run_calibration(rows, router=self._pseudo_router(), test_fraction=0.25, seed=1)
        acc = result["accuracy_per_question"]
        self.assertIn("intent", acc)
        # intent on fast-path rows is correct, on the rest wrong -> accuracy in (0,1)
        intent_acc = acc["intent"]["accuracy"]
        self.assertGreater(intent_acc, 0.0)
        self.assertLess(intent_acc, 1.0)
        self.assertIn("ece", acc["intent"])

    def test_reliability_curve_and_ece_bounds(self):
        rows = [{"query": f"domanda {i}", "external_effect": False, "risk": 1,
                 "expected_target": "reasoning_llm", "can_fast_path": False} for i in range(20)]
        def route(text, job_id="calibration"):
            r = MagicMock()
            r.used_fast_path = False
            r.confidence = 0.5 + (hash(text) % 10) / 20.0  # deterministic 0.5..0.95
            r.details = {"answers": {}}
            return r
        result = run_calibration(rows, router=route, test_fraction=0.25, seed=1)
        intent = result["accuracy_per_question"]["intent"]
        self.assertGreaterEqual(intent["ece"], 0.0)
        self.assertLessEqual(intent["ece"], 1.0)
        self.assertTrue(intent["reliability_curve"])

    def test_escalation_rate_counts_below_threshold_and_clarification(self):
        def route(text, job_id="calibration"):
            r = MagicMock()
            r.used_fast_path = False
            r.confidence = 0.3
            r.target = RoutingTarget.CLARIFICATION
            r.details = {"answers": {}}
            return r
        rows = [{"query": f"q{i}", "external_effect": False, "risk": 1,
                 "expected_target": "reasoning_llm", "can_fast_path": False} for i in range(12)]
        result = run_calibration(rows, router=route, test_fraction=0.25, seed=1)
        self.assertEqual(result["escalation_rate"], 1.0)

if __name__ == "__main__":
    unittest.main()
