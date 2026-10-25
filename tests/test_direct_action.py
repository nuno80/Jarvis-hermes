"""Slice list_create: eligibility, validazione argomenti LLM, rewrite, MCP (issue #64)."""

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from jarvis_hermes.decision import pre_turn_dispatch
from jarvis_hermes.direct_action import (
    build_list_create_action,
    constrain_llm_request_for_list_create,
    execute_direct_action,
    is_list_create_eligible,
    validate_list_create_arguments,
)
from jarvis_hermes.jobs import JobStore


def _regression_dispatch():
    text = "crea una lista della spesa con melanzane cetrioli latte e vino"
    return text, pre_turn_dispatch(text, model_call=MagicMock())


class EligibilityTests(unittest.TestCase):
    def test_regression_phrase_is_eligible_but_produces_no_arguments(self):
        text, dispatch = _regression_dispatch()

        self.assertTrue(is_list_create_eligible(dispatch))
        # Il routing non produce argomenti: li deve produrre il System 2/LLM.
        self.assertNotIn("tasks", dispatch.get("answers", {}))

    def test_non_deterministic_dispatch_is_not_eligible(self):
        self.assertFalse(is_list_create_eligible({"path": "system_2"}))
        self.assertFalse(is_list_create_eligible(None))
        _, dispatch = _regression_dispatch()
        tampered = dict(dispatch, target="reasoning_llm")
        self.assertFalse(is_list_create_eligible(tampered))


class ArgumentValidationTests(unittest.TestCase):
    def test_llm_arguments_are_validated_without_language_heuristics(self):
        validated = validate_list_create_arguments({
            "name": "Spesa",
            "tasks": ["latte di mandorla", "pane integrale", "uova", "2 kg di mele"],
        })

        self.assertEqual(validated, {
            "name": "Spesa",
            "tasks": ["latte di mandorla", "pane integrale", "uova", "2 kg di mele"],
        })

    def test_ambiguous_or_invalid_arguments_fall_back(self):
        for candidate in [
            None, "Spesa", {"name": "Lavoro", "tasks": ["a"]},
            {"name": "Spesa", "tasks": []},
            {"name": "Spesa", "tasks": ["latte", "Latte"]},
            {"name": "Spesa", "tasks": ["  "]},
            {"name": "Spesa", "tasks": [f"item-{i}" for i in range(51)]},
            {"name": "Spesa", "tasks": ["ok"], "extra": True},
        ]:
            with self.subTest(candidate=str(candidate)[:40]):
                self.assertIsNone(validate_list_create_arguments(candidate))
        with self.assertRaises(ValueError):
            build_list_create_action({"name": "Spesa", "tasks": []})


class LlmRequestRewriteTests(unittest.TestCase):
    def _request(self):
        other = {"type": "function", "function": {"name": "mcp__jarvis__email_search"}}
        target = {"type": "function",
                  "function": {"name": "mcp__jarvis__todoist_create_task_list"}}
        return {"model": "m", "messages": [{"role": "user", "content": "x"}],
                "tools": [other, target]}

    def test_eligible_turn_forces_the_single_allowlisted_tool(self):
        _, dispatch = _regression_dispatch()

        rewritten = constrain_llm_request_for_list_create(self._request(), dispatch)

        self.assertIsNotNone(rewritten)
        self.assertEqual(
            [t["function"]["name"] for t in rewritten["tools"]],
            ["mcp__jarvis__todoist_create_task_list"])
        self.assertEqual(rewritten["tool_choice"], "required")

    def test_no_rewrite_without_eligibility_or_tool(self):
        _, dispatch = _regression_dispatch()
        self.assertIsNone(
            constrain_llm_request_for_list_create(self._request(), {"path": "system_2"}))
        missing = {"model": "m", "messages": [], "tools": [
            {"type": "function", "function": {"name": "mcp__jarvis__email_search"}}]}
        self.assertIsNone(constrain_llm_request_for_list_create(missing, dispatch))


class DirectActionExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.store = JobStore(Path(self.tempdir.name) / "jobs.sqlite3")
        self.job_id = self.store.create_job(42, "voice_message", "lista spesa")["job_id"]

    def tearDown(self):
        self.tempdir.cleanup()

    def test_verified_mcp_success_closes_job_and_reports_actual_tasks(self):
        action = build_list_create_action({"name": "Spesa", "tasks": ["latte", "uova"]})
        envelope = {
            "ok": True,
            "data": {
                "project": {"id": "project-1", "name": "Spesa"},
                "tasks": [
                    {"id": "task-1", "content": "latte"},
                    {"id": "task-2", "content": "uova"},
                ],
                "tasks_created": 2,
            },
            "error": None,
        }
        dispatch = MagicMock(return_value=json.dumps({"structuredContent": envelope}))

        outcome = execute_direct_action(
            action, job_id=self.job_id, job_store=self.store, dispatch_fn=dispatch)

        self.assertEqual(outcome["status"], "succeeded")
        self.assertIn("Spesa", outcome["reply"])
        self.assertIn("2 task", outcome["reply"])
        dispatch.assert_called_once_with(
            "mcp__jarvis__todoist_create_task_list",
            {"name": "Spesa", "tasks": ["latte", "uova"], "request_id": self.job_id},
        )
        job = self.store.get_job(self.job_id)
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["effects_count"], 3)
        self.assertEqual(job["result"]["request_id"], self.job_id)

    def test_mcp_error_is_outcome_unknown_and_never_retried(self):
        action = build_list_create_action({"name": "Spesa", "tasks": ["latte"]})
        envelope = {
            "ok": False,
            "data": None,
            "error": {"code": "PROVIDER_ERROR", "message": "timeout", "retryable": True},
        }
        dispatch = MagicMock(return_value=json.dumps({"structuredContent": envelope}))

        outcome = execute_direct_action(
            action, job_id=self.job_id, job_store=self.store, dispatch_fn=dispatch)

        self.assertEqual(outcome["status"], "outcome_unknown")
        self.assertEqual(outcome["error"]["code"], "PROVIDER_ERROR")
        self.assertNotIn("✅", outcome["reply"])
        self.assertIn("Non ho ripetuto", outcome["reply"])
        self.assertEqual(dispatch.call_count, 1)
        self.assertEqual(self.store.get_job(self.job_id)["status"], "outcome_unknown")


if __name__ == "__main__":
    unittest.main()
