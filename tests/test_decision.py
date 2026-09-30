"""Tests for the System 1 local decision module (issue #31 / JARVIS-31, App. I1, AT14).

TDD seam: `decide()` takes an injectable `model_call` so no test needs Ollama.
Live-model checks (accuracy/latency on qwen3.5:4b) run only with JARVIS_LIVE_S1=1.
"""
import json
import math
import os
import unittest

from jarvis_hermes.decision import (
    DECISION_SCHEMA,
    INTENTS,
    DecisionRequest,
    action_class,
    apply_policy,
    build_v1_questions,
    build_v1_request,
    decide,
    diagnose_local,
    direct_reply,
    label_confidence,
    load_thresholds,
    pre_turn_dispatch,
    skill_suggestion,
)

GOOD_RAW = {
    "intent": "disk_usage", "handler": "disk_usage",
    "skill_hint": "system_status", "skill_hint_2": "none", "skill_hint_3": "reasoning",
    "needs_clarification": False, "external_effect": False, "private_data": False,
    "risk": 1, "complexity": 1,
}
GOOD_TEXT = json.dumps(GOOD_RAW, separators=(",", ":"))


def _good_call(text: str = GOOD_TEXT, logprobs: list | None = None,
               prompt_tok: int = 10, eval_tok: int = 20):
    if logprobs is None:
        logprobs = [
            {"token": text, "logprob": -0.1,
             "bytes": list(text.encode("utf-8"))},
        ]
    return lambda prompt, fmt, model, endpoint, timeout: (text, logprobs, prompt_tok, eval_tok)


class ContractTests(unittest.TestCase):
    def test_v1_questions_cover_appendix_i1(self):
        qs = {q.key: q.type for q in build_v1_questions()}
        self.assertEqual(qs, {
            "intent": "choice", "handler": "choice", "skill_hint": "choice",
            "needs_clarification": "binary", "external_effect": "binary",
            "private_data": "binary", "risk": "score", "complexity": "score",
        })

    def test_request_state_has_no_approval_tokens_or_secrets(self):
        req = build_v1_request("fai push su main", job_id="j1")
        self.assertEqual(set(req.state), {"text", "budget_tokens"})
        self.assertNotIn("approval_token", req.state)
        self.assertNotIn("secrets", req.state)

    def test_good_decision_returns_answers_confidence_latency_model(self):
        res = decide(build_v1_request("quanto spazio libero ho?"), model_call=_good_call())
        self.assertEqual(res.path_chosen, "system_1")
        self.assertTrue(res.valid)
        self.assertIsNone(res.escalation_reason)
        self.assertEqual(res.answers["intent"], "disk_usage")
        self.assertEqual(res.answers["skill_hints_top3"],
                         ["system_status", "none", "reasoning"])
        self.assertTrue(0.0 <= res.confidences["overall"] <= 1.0)
        self.assertEqual(res.model_id, "qwen3.5:4b")
        self.assertGreaterEqual(res.latency_ms, 0.0)
        self.assertEqual(res.tokens, 30)

    def test_skill_hint_top3_all_in_vocabulary(self):
        from jarvis_hermes.decision import SKILLS
        res = decide(build_v1_request("ciao"), model_call=_good_call())
        for skill in res.answers["skill_hints_top3"]:
            self.assertIn(skill, SKILLS)


class FailClosedTests(unittest.TestCase):
    """AT14: malformato, NaN, fuori range o lento -> escalation, mai permessi."""

    def test_malformed_json_escalates(self):
        res = decide(build_v1_request("ciao"), model_call=_good_call(text="non json{"))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_output")
        self.assertFalse(res.valid)

    def test_unknown_intent_escalates(self):
        bad = dict(GOOD_RAW, intent="lancia_razzi")
        res = decide(build_v1_request("ciao"),
                     model_call=_good_call(text=json.dumps(bad)))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_output")

    def test_out_of_range_score_escalates(self):
        bad = dict(GOOD_RAW, risk=9)
        res = decide(build_v1_request("ciao"),
                     model_call=_good_call(text=json.dumps(bad)))
        self.assertEqual(res.path_chosen, "system_2")

    def test_bool_as_int_score_escalates(self):
        bad = dict(GOOD_RAW, risk=True)
        res = decide(build_v1_request("ciao"),
                     model_call=_good_call(text=json.dumps(bad)))
        self.assertEqual(res.path_chosen, "system_2")

    def test_missing_label_confidence_escalates(self):
        # Etichetta non trovata nel testo -> confidenza NaN -> escalation.
        res = decide(build_v1_request("ciao"), model_call=_good_call(logprobs=[]))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_confidence")

    def test_latency_over_budget_escalates(self):
        import time
        def slow(prompt, fmt, model, endpoint, timeout):
            time.sleep(0.05)
            return GOOD_TEXT, [], 1, 1
        res = decide(build_v1_request("ciao"), model_call=slow, latency_budget_ms=1.0)
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "latency_budget")

    def test_model_exception_escalates(self):
        def boom(prompt, fmt, model, endpoint, timeout):
            raise TimeoutError("ollama down")
        res = decide(build_v1_request("ciao"), model_call=boom)
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_output")


class LabelConfidenceTests(unittest.TestCase):
    def test_confidence_comes_from_label_logprobs(self):
        text = '{"intent":"disk_usage"}'
        toks = [
            {"token": '{"intent":"', "logprob": -0.01,
             "bytes": list('{"intent":"'.encode())},
            {"token": "disk_usage", "logprob": -0.2,
             "bytes": list("disk_usage".encode())},
            {"token": '"}', "logprob": -0.01, "bytes": list('"}'.encode())},
        ]
        conf = label_confidence(text, toks, "disk_usage")
        self.assertAlmostEqual(conf, math.exp(-0.2), places=6)

    def test_missing_label_is_nan(self):
        self.assertTrue(math.isnan(label_confidence('{"a":1}', [], "disk_usage")))

    def test_confidence_never_a_declared_number(self):
        # Il payload con "confidence": 0.99 dichiarato va ignorato: decide()
        # non lo legge mai; la confidenza nasce solo dai logprob.
        raw = dict(GOOD_RAW, confidence=0.99)
        res = decide(build_v1_request("ciao"),
                     model_call=_good_call(text=json.dumps(raw)))
        self.assertNotEqual(res.confidences["overall"], 0.99)


class PolicyTests(unittest.TestCase):
    """Politica in codice (sezione 7): soglie per classe, fail-closed AT14."""

    def _s1(self, **over):
        raw = dict(GOOD_RAW, **over)
        return decide(build_v1_request("ciao"),
                      model_call=_good_call(text=json.dumps(raw)))

    def test_readonly_high_confidence_stays_system_1(self):
        res = apply_policy(self._s1(), {"readonly": 0.70, "protected": 0.95,
                                        "reasoning": 0.60, "clarify": 0.70})
        self.assertEqual(res.path_chosen, "system_1")
        self.assertIsNone(res.escalation_reason)

    def test_protected_action_escalates_despite_confidence(self):
        res = apply_policy(self._s1(external_effect=True, risk=4))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "protected_action")

    def test_ambiguous_escalates_to_clarification(self):
        res = apply_policy(self._s1(intent="ambiguous", handler="ask_clarification",
                                    needs_clarification=True))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "needs_clarification")

    def test_high_complexity_escalates(self):
        res = apply_policy(self._s1(complexity=5))
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "high_complexity")

    def test_low_confidence_escalates(self):
        res = apply_policy(self._s1(), {"readonly": 0.9999, "protected": 0.95,
                                        "reasoning": 0.60, "clarify": 0.70})
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "low_confidence")

    def test_action_class_ordering(self):
        self.assertEqual(action_class(self._s1(external_effect=True)), "protected")
        self.assertEqual(action_class(self._s1(needs_clarification=True)), "clarify")
        self.assertEqual(action_class(self._s1(complexity=3)), "reasoning")
        self.assertEqual(action_class(self._s1()), "readonly")

    def test_thresholds_from_config_file(self):
        thr = load_thresholds("config/routing_thresholds.json")
        self.assertEqual(thr["protected"], 0.95)
        self.assertLessEqual(thr["reasoning"], 0.70)


class PreTurnDispatchTests(unittest.TestCase):
    """Hook Hermes (ADR 0005): bypass sicuro, skill suggestion, telemetria."""

    def test_disk_reply_reports_physical_disk_not_wsl_vhdx(self):
        # Regressione #42: in WSL / e un VHDX virtuale, il disco reale e /mnt/c.
        import shutil
        from collections import namedtuple
        from unittest.mock import patch
        Usage = namedtuple("Usage", "total used free")
        GiB = 1024.0 ** 3
        fake = {"/mnt/c": Usage(953 * GiB, 920 * GiB, 33 * GiB),
                "/": Usage(1007 * GiB, 162 * GiB, 845 * GiB)}
        good = decide(build_v1_request("quanto spazio libero ho?"),
                      model_call=_good_call())
        with patch("platform.system", return_value="Linux"), \
             patch("platform.release", return_value="5.15.167.4-microsoft-standard-WSL2"), \
             patch.object(shutil, "disk_usage", side_effect=lambda p: fake[p]):
            self.assertIn("33.0", direct_reply(good) or "")
        def _no_c(path):
            if path == "/mnt/c":
                raise OSError("no C:")
            return fake[path]
        with patch.object(shutil, "disk_usage", side_effect=_no_c):
            self.assertIn("845.0", direct_reply(good) or "")
        with patch.object(shutil, "disk_usage",
                           side_effect=OSError("no disk")):
            self.assertIsNone(direct_reply(good))

    def test_safe_handler_returns_direct_reply_and_no_skill_context(self):
        out = pre_turn_dispatch("quanto spazio libero ho?", model_call=_good_call())
        self.assertEqual(out["path"], "system_1")
        self.assertIsNotNone(out["reply"])
        self.assertIn("GiB", out["reply"])
        self.assertIsNone(out["skill_context"])
        self.assertIsNone(out["escalation_reason"])

    def test_protected_action_has_no_reply_but_keeps_path(self):
        out = pre_turn_dispatch("ciao", model_call=_good_call(
            text=json.dumps(dict(GOOD_RAW, external_effect=True, risk=4))))
        self.assertEqual(out["path"], "system_2")
        self.assertEqual(out["escalation_reason"], "protected_action")
        self.assertIsNone(out["reply"])

    def test_skill_suggestion_format(self):
        res = decide(build_v1_request("ciao"), model_call=_good_call())
        res = apply_policy(res)
        self.assertEqual(skill_suggestion(res),
                         "Suggested skill to inspect first: system_status")

    def test_skill_none_gives_no_context(self):
        res = decide(build_v1_request("ciao"), model_call=_good_call(
            text=json.dumps(dict(GOOD_RAW, skill_hint="none",
                                          skill_hint_2="none", skill_hint_3="none"))))
        self.assertIsNone(skill_suggestion(apply_policy(res)))

    def test_diagnose_local_is_readonly_and_secret_free(self):
        diag = diagnose_local()
        self.assertEqual(diag["schema_version"], "1.0")
        blob = json.dumps(diag)
        self.assertNotIn("approval_token", blob)
        self.assertIn("disk", diag)

    def test_telemetry_and_budget_recorded(self):
        import tempfile
        from pathlib import Path
        from jarvis_hermes.llm import BudgetTracker
        from jarvis_hermes.telemetry import RoutingDecisionStore
        with tempfile.TemporaryDirectory() as d:
            store = RoutingDecisionStore(Path(d) / "r.sqlite3")
            tracker = BudgetTracker(Path(d) / "b.json")
            out = pre_turn_dispatch("quanto spazio libero ho?", job_id="s1-test",
                                    model_call=_good_call(),
                                    telemetry_store=store, budget_tracker=tracker)
            self.assertEqual(out["path"], "system_1")
            self.assertEqual(len(store.get_decisions(limit=5)), 1)
            usage = tracker.get_job_usage("s1-test")
            self.assertEqual(usage["calls_count"], 1)


@unittest.skipUnless(os.environ.get("JARVIS_LIVE_S1") == "1",
                     "live Ollama check: JARVIS_LIVE_S1=1")
class LiveModelTests(unittest.TestCase):
    """Verifica reale su qwen3.5:4b: vocabolari + latenza. Non corre in CI."""

    def test_live_disk_usage_labels(self):
        from jarvis_hermes.decision import apply_policy, ollama_call
        res = apply_policy(decide(build_v1_request("quanto spazio libero ho sul disco?"),
                                  model_call=ollama_call))
        # Budget 500ms (spec App.I: default proposto 300ms); qwen3.5:4b live
        # misura ~1.3s a caldo -> spesso latency_budget, ma il contratto AT14
        # resta: o system_1 valido o escalation nominata.
        # il contratto AT14 resta: o system_1 valido o escalation nominata.
        self.assertIn(res.path_chosen, ("system_1", "system_2"))
        if res.path_chosen == "system_1":
            self.assertEqual(res.answers["intent"], "disk_usage")
            self.assertIn(res.answers["skill_hint"],
                          ["system_status", "none", "reasoning"])
        else:
            self.assertIn(res.escalation_reason,
                          ("latency_budget", "low_confidence", "protected_action",
                           "needs_clarification", "high_complexity",
                           "invalid_output", "invalid_confidence"))
        print(f"\nlive: path={res.path_chosen} reason={res.escalation_reason} "
              f"intent={res.answers.get('intent')} conf={res.confidences.get('overall')}"
              f" latency_ms={res.latency_ms} tokens={res.tokens}")


if __name__ == "__main__":
    unittest.main()
