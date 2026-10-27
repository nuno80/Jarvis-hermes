"""Tests for Hermes pre-turn extension points and hook feasibility spike (JARVIS-37 / Issue 35)."""

import asyncio
import shutil
import unittest
from unittest.mock import MagicMock


"""Tests for Hermes pre-turn extension points and contract verification (JARVIS-37 / Issue 35 / Issue 61).

Unit behavior tests verify contract shape and logic with isolated surrogates.
Host-dependent integration tests verify the contract against the real Hermes installation
(PluginManager, discover_plugins, hooks, middleware, and PluginContext) without starting pollers.
"""

import asyncio
import os
import shutil
import subprocess
from pathlib import Path
import unittest
from unittest.mock import MagicMock


HERMES_ROOT = Path.home() / ".hermes" / "hermes-agent"
HERMES_PYTHON = HERMES_ROOT / "venv" / "bin" / "python"
HERMES_AVAILABLE = HERMES_ROOT.is_dir() and HERMES_PYTHON.is_file()


class HermesPreTurnUnitBehaviorTests(unittest.TestCase):
    """Unit tests of behavior logic for pre-turn interception, direct replies (bypass),
    model routing, and context injection.
    
    NOTE: These are unit tests of behavior only and do not serve as proof of Hermes integration.
    Integration with the real Hermes installation is tested in HermesRealContractIntegrationTests.
    """

    def test_pre_gateway_dispatch_behavior_direct_reply_bypass(self):
        """Unit behavior: pre_gateway_dispatch can return {'action': 'skip'} to bypass the main LLM."""
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

    def test_llm_request_middleware_behavior_model_selection(self):
        """Unit behavior: llm_request middleware can rewrite request['model'] before LLM provider call."""
        def llm_request_middleware(request, **kwargs):
            req = dict(request)
            req["model"] = "routed-auxiliary-model"
            return {"request": req, "source": "jarvis-router"}

        original_request = {"model": "default-main-model", "messages": [{"role": "user", "content": "hi"}]}
        result = llm_request_middleware(original_request)
        self.assertEqual(result["request"]["model"], "routed-auxiliary-model")
        self.assertEqual(result["source"], "jarvis-router")

    def test_pre_llm_call_behavior_context_injection(self):
        """Unit behavior: pre_llm_call hook returns context dict that appends to current turn's user message."""
        def pre_llm_call_hook(user_message, **kwargs):
            return {"context": "[System 1 Context: Fast state disk=906GB free]"}

        res = pre_llm_call_hook("quanto spazio libero ho?")
        self.assertEqual(res, {"context": "[System 1 Context: Fast state disk=906GB free]"})


@unittest.skipUnless(
    HERMES_AVAILABLE,
    "Real Hermes installation not found at ~/.hermes/hermes-agent; host-dependent activity remains open"
)
class HermesRealContractIntegrationTests(unittest.TestCase):
    """Host-dependent integration contract tests against the real Hermes installation.

    Verifies:
    1. Inspection of the real Hermes runtime, version, and registration mechanism.
    2. Plugin discovery and hook/middleware registration via real Hermes PluginManager.
    3. Dispatch and bypass via real `ainvoke_hook('pre_gateway_dispatch', ...)`.
    4. Model selection via real `apply_llm_request_middleware(...)`.
    5. Context injection via real `ainvoke_hook('pre_llm_call', ...)`.
    6. Contract registration of repo plugin `plugins/jarvis-s1` into real `hermes_cli.plugins.PluginContext`.
    """

    def test_real_hermes_installation_contract(self):
        repo_root = Path.cwd().resolve()
        script = """
import sys
import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch
import importlib.util

from hermes_cli import plugins
from hermes_cli.middleware import apply_llm_request_middleware

# 1. Verify inspection of Hermes PluginManager and real plugin discovery
plugins.discover_plugins()
mgr = plugins.get_plugin_manager()

assert "jarvis-s1" in mgr._plugins, "jarvis-s1 plugin not discovered by Hermes"
loaded_plugin = mgr._plugins["jarvis-s1"]
assert "pre_gateway_dispatch" in loaded_plugin.hooks_registered, "pre_gateway_dispatch hook not registered"
assert "pre_llm_call" in loaded_plugin.hooks_registered, "pre_llm_call hook not registered"
assert "llm_request" in loaded_plugin.middleware_registered, "llm_request middleware not registered"

plugin_mod = sys.modules.get("hermes_plugins.jarvis_s1")
assert plugin_mod is not None, "hermes_plugins.jarvis_s1 module not loaded in Hermes runtime"

# 2. Dispatch pre_gateway_dispatch (direct reply / skip) through Hermes real ainvoke_hook
event = MagicMock(text="quanto spazio libero ho sul disco?", message_type="text")
gateway = MagicMock()
adapter = MagicMock()
gateway.adapters = {event.source.platform: adapter}
dispatch_results = asyncio.run(mgr.ainvoke_hook("pre_gateway_dispatch", event=event, gateway=gateway, session_store=MagicMock()))
assert dispatch_results == [{"action": "skip", "reason": "jarvis_s1_direct"}], f"Unexpected dispatch result: {dispatch_results}"
assert adapter.send.called, "Adapter send was not called for direct reply"

# 3. Context injection via pre_llm_call through Hermes real ainvoke_hook
with patch.object(plugin_mod, "pre_turn_dispatch", return_value={"skill_context": "Suggested skill: disk_status"}):
    ctx_results = asyncio.run(mgr.ainvoke_hook("pre_llm_call", user_message="quanto spazio ho?"))
    assert ctx_results == [{"context": "Suggested skill: disk_status"}], f"Unexpected context result: {ctx_results}"

# 4. Model selection through Hermes real apply_llm_request_middleware
with patch.object(plugin_mod, "pre_turn_dispatch", return_value={"model_override": "qwen-fast-model"}):
    req = {"model": "base-model", "messages": [{"role": "user", "content": "check space"}]}
    res = apply_llm_request_middleware(req)
    assert res.payload["model"] == "qwen-fast-model", f"Model not rewritten by Hermes middleware: {res.payload}"

# 5. Verify registration contract of repo plugin code against Hermes PluginContext
repo_plugin_path = Path.cwd() / "plugins" / "jarvis-s1" / "__init__.py"
assert repo_plugin_path.is_file(), f"Repo plugin file not found at {repo_plugin_path}"
spec = importlib.util.spec_from_file_location("repo_jarvis_s1", repo_plugin_path)
repo_mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(repo_mod)

repo_mgr = plugins.PluginManager()
manifest = plugins.PluginManifest(
    name="jarvis-s1",
    path=str(repo_plugin_path.parent),
    provides_hooks=["pre_gateway_dispatch", "post_stt_enrichment", "pre_llm_call", "pre_tool_call"],
)
ctx = plugins.PluginContext(manifest, repo_mgr)
repo_mod.register(ctx)

assert "pre_gateway_dispatch" in repo_mgr._hooks, "pre_gateway_dispatch missing in PluginContext registration"
assert "pre_llm_call" in repo_mgr._hooks, "pre_llm_call missing in PluginContext registration"
assert "llm_request" in repo_mgr._middleware, "llm_request missing in PluginContext registration"

print("OK: Real Hermes contract verified successfully.")
"""
        cmd = [
            str(HERMES_PYTHON),
            "-c",
            script,
        ]
        env = dict(os.environ)
        env["PYTHONPATH"] = f"{HERMES_ROOT}:{repo_root}:{repo_root / 'src'}"
        res = subprocess.run(cmd, capture_output=True, text=True, env=env, cwd=str(repo_root))
        if res.returncode != 0:
            print("STDOUT:", res.stdout)
            print("STDERR:", res.stderr)
        self.assertEqual(res.returncode, 0, f"Hermes contract test failed:\n{res.stderr}\n{res.stdout}")
        self.assertIn("OK: Real Hermes contract verified successfully.", res.stdout)


if __name__ == "__main__":
    unittest.main()
