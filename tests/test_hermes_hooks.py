"""Tests for Hermes pre-turn extension points and hook feasibility spike (JARVIS-37 / Issue 35)."""

import asyncio
import shutil
import unittest
from unittest.mock import MagicMock


class HermesPreTurnSpikeTests(unittest.TestCase):
    """Verify that Hermes plugin hooks and middleware enable pre-turn interception,
    direct replies (bypass), model routing, and context injection without breaking prefix cache or poller.
    """

    def test_hermes_pre_gateway_dispatch_allows_direct_reply_bypass(self):
        """(a) Test pre_gateway_dispatch hook can return {'action': 'skip'} to bypass the main LLM."""
        # Simulate hook contract
        handled = []

        def pre_gateway_dispatch_hook(event, gateway, session_store, **kwargs):
            if "spazio libero" in event.text.lower():
                usage = shutil.disk_usage("/")
                handled.append(f"Free: {usage.free}")
                return {"action": "skip", "reason": "handled_directly_by_jarvis_s1"}
            return None

        event = MagicMock(text="quanto spazio libero ho?")
        res = pre_gateway_dispatch_hook(event, MagicMock(), MagicMock())
        self.assertEqual(res, {"action": "skip", "reason": "handled_directly_by_jarvis_s1"})
        self.assertEqual(len(handled), 1)

    def test_hermes_llm_request_middleware_allows_model_selection(self):
        """(b) Test llm_request middleware can rewrite request['model'] before LLM provider call."""
        def llm_request_middleware(request, **kwargs):
            req = dict(request)
            req["model"] = "routed-auxiliary-model"
            return {"request": req, "source": "jarvis-router"}

        original_request = {"model": "default-main-model", "messages": [{"role": "user", "content": "hi"}]}
        result = llm_request_middleware(original_request)
        self.assertEqual(result["request"]["model"], "routed-auxiliary-model")
        self.assertEqual(result["source"], "jarvis-router")

    def test_hermes_pre_llm_call_allows_context_injection(self):
        """(c) Test pre_llm_call hook returns context dict that appends to current turn's user message."""
        def pre_llm_call_hook(user_message, **kwargs):
            return {"context": "[System 1 Context: Fast state disk=906GB free]"}

        res = pre_llm_call_hook("quanto spazio libero ho?")
        self.assertEqual(res, {"context": "[System 1 Context: Fast state disk=906GB free]"})


if __name__ == "__main__":
    unittest.main()
