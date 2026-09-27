"""Unit test per il backend Laya (issue #41): mock-only, nessun server reale.

TDD seam: parse_answers e decide(confidence_override) senza rete; LayaClient
e laya_model_call con fake transport iniettato via monkeypatching leggero.
"""
import json
import math
import unittest

from jarvis_hermes import laya
from jarvis_hermes.decision import build_v1_request, decide, pre_turn_dispatch
from jarvis_hermes.laya import (
    BINARY_QUESTIONS,
    INTENT_TABLE,
    LayaClient,
    LayaError,
    build_questions,
    decide_with_laya,
    laya_model_call,
    parse_answers,
)


def _payload(intent="disk_usage", conf=0.9, yes_probs=None, scores=None):
    yes_probs = yes_probs or {}
    scores = scores or {}
    answers = {
        "intent": {
            "type": "choice", "choice": intent,
            "probabilities": {intent: conf},
            "confidence": 0.5, "answer_confidence": conf,
        },
    }
    for key in BINARY_QUESTIONS:
        yes = yes_probs.get(key, 0.1)
        answers[key] = {
            "type": "choice", "choice": "yes" if yes > 0.5 else "no",
            "probabilities": {"no": 1.0 - yes, "yes": yes},
            "confidence": 0.5, "answer_confidence": 0.8,
        }
    for key in ("risk", "complexity"):
        answers[key] = {
            "type": "score", "score": scores.get(key, 0.0),
            "probabilities": {"0": 1.0}, "confidence": 0.5,
            "answer_confidence": 0.8,
        }
    return {"answers": answers, "usage": {"input_tokens": 42, "output_tokens": 0}}


class QuestionsTests(unittest.TestCase):
    def test_questions_use_choice_not_noul(self):
        qs = build_questions()
        for key in BINARY_QUESTIONS:
            self.assertEqual(qs[key]["type"], "choice")
            self.assertEqual(set(qs[key]["criteria"]), {"no", "yes"})
        self.assertEqual(qs["intent"]["type"], "choice")
        self.assertEqual(qs["risk"]["type"], "score")

    def test_intent_table_covers_all_v1_intents(self):
        from jarvis_hermes.decision import HANDLERS, INTENTS, SKILLS
        for intent in INTENTS:
            handler, skill = INTENT_TABLE[intent]
            self.assertIn(handler, HANDLERS)
            self.assertIn(skill, SKILLS)


class ParseTests(unittest.TestCase):
    def test_good_payload_maps_to_v1_raw(self):
        raw, override = parse_answers(_payload("disk_usage", 0.9))
        self.assertEqual(raw["intent"], "disk_usage")
        self.assertEqual(raw["handler"], "disk_usage")
        self.assertEqual(raw["skill_hint"], "system_status")
        self.assertFalse(raw["external_effect"])
        self.assertEqual(raw["risk"], 1)
        self.assertAlmostEqual(override["intent"], 0.9)

    def test_unknown_intent_raises(self):
        with self.assertRaises(LayaError):
            parse_answers(_payload("lancia_razzi", 0.9))

    def test_missing_confidence_raises(self):
        bad = _payload("disk_usage", 0.9)
        del bad["answers"]["intent"]["answer_confidence"]
        with self.assertRaises(LayaError):
            parse_answers(bad)

    def test_confidence_out_of_range_raises(self):
        with self.assertRaises(LayaError):
            parse_answers(_payload("disk_usage", 1.5))

    def test_binary_yes_wins_over_half(self):
        raw, _ = parse_answers(_payload("git_push", 0.9,
                                        yes_probs={"external_effect": 0.8}))
        self.assertTrue(raw["external_effect"])


GOOD_V1 = json.dumps({
    "intent": "disk_usage", "handler": "disk_usage",
    "skill_hint": "system_status", "skill_hint_2": "none",
    "skill_hint_3": "reasoning", "needs_clarification": False,
    "external_effect": False, "private_data": False,
    "risk": 1, "complexity": 1})


def _good_v1_call():
    return lambda p, f, m, e, t: (GOOD_V1, [], 0, 0)


class OverrideTests(unittest.TestCase):
    def test_valid_override_reaches_system_1(self):
        res = decide(build_v1_request("quanto spazio libero ho?"),
                     model_call=lambda p, f, m, e, t: (json.dumps({
                         "intent": "disk_usage", "handler": "disk_usage",
                         "skill_hint": "system_status", "skill_hint_2": "none",
                         "skill_hint_3": "reasoning", "needs_clarification": False,
                         "external_effect": False, "private_data": False,
                         "risk": 1, "complexity": 1}), [], 10, 5),
                     model_id="laya:multilingual",
                     confidence_override={"intent": 0.9, "handler": 0.9, "skill_hint": 0.9})
        self.assertEqual(res.path_chosen, "system_1")
        self.assertEqual(res.model_id, "laya:multilingual")

    def test_nan_override_escalates(self):
        res = decide(build_v1_request("ciao"),
                     model_call=_good_v1_call(),
                     confidence_override={"intent": float("nan"),
                                          "handler": 0.9, "skill_hint": 0.9})
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_confidence")

    def test_out_of_range_override_escalates(self):
        res = decide(build_v1_request("ciao"),
                     model_call=_good_v1_call(),
                     confidence_override={"intent": 2.0,
                                          "handler": 0.9, "skill_hint": 0.9})
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_confidence")

    def test_bool_override_rejected(self):
        res = decide(build_v1_request("ciao"),
                     model_call=_good_v1_call(),
                     confidence_override={"intent": True,
                                          "handler": 0.9, "skill_hint": 0.9})
        self.assertEqual(res.path_chosen, "system_2")
        self.assertEqual(res.escalation_reason, "invalid_confidence")

    def test_legacy_path_without_override_unchanged(self):
        text = json.dumps({
            "intent": "disk_usage", "handler": "disk_usage",
            "skill_hint": "system_status", "skill_hint_2": "none",
            "skill_hint_3": "reasoning", "needs_clarification": False,
            "external_effect": False, "private_data": False,
            "risk": 1, "complexity": 1})
        toks = [{"token": text, "logprob": -0.1,
                 "bytes": list(text.encode("utf-8"))}]
        res = decide(build_v1_request("quanto spazio libero ho?"),
                     model_call=lambda p, f, m, e, t: (text, toks, 1, 1))
        self.assertEqual(res.path_chosen, "system_1")
        self.assertTrue(math.isfinite(res.confidences["overall"]))


class ClientTests(unittest.TestCase):
    def test_decide_with_laya_uses_override(self):
        client = LayaClient(endpoint="http://mock/systemone")
        client.system_one = lambda state, questions, model="multilingual": \
            _payload("disk_usage", 0.95)
        res = decide_with_laya("quanto spazio libero ho?", client=client)
        self.assertEqual(res.answers["intent"], "disk_usage")
        self.assertAlmostEqual(res.confidences["overall"], 0.95)
        self.assertEqual(res.model_id, "laya:multilingual")

    def test_laya_model_call_shape(self):
        client = LayaClient(endpoint="http://mock/systemone")
        client.system_one = lambda state, questions, model="multilingual": \
            _payload("repo_status", 0.8)
        call = laya_model_call(client)
        text, lp, prompt_tok, eval_tok = call(
            "x\nRichiesta: git status", {}, "m", "e", 5.0)
        self.assertEqual(lp, [])
        self.assertEqual(json.loads(text)["intent"], "repo_status")
        self.assertEqual(prompt_tok, 42)

    def test_pre_turn_dispatch_with_laya_call(self):
        client = LayaClient(endpoint="http://mock/systemone")
        client.system_one = lambda state, questions, model="multilingual": \
            _payload("repo_status", 0.8)
        out = pre_turn_dispatch("stato repo XYZ", model_call=laya_model_call(client),
                                confidence_override={"intent": 0.8, "handler": 0.8,
                                                     "skill_hint": 0.8})
        self.assertEqual(out["intent"], "repo_status")

    def test_unreachable_server_raises_laya_error(self):
        client = LayaClient(endpoint="http://127.0.0.1:1/nope", timeout_s=0.5)
        with self.assertRaises(LayaError):
            client.system_one({"text": "ciao"}, build_questions())


if __name__ == "__main__":
    unittest.main()
