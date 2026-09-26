import unittest
from unittest.mock import patch, MagicMock
import os
import json
import tempfile
import urllib.error
from pathlib import Path

from jarvis_hermes.llm import (
    LLMClient,
    LLMConfig,
    LLMError,
    UsageRecord,
    BudgetTracker,
)


class GeminiProviderTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.state_dir = Path(self.tmp.name)
        self.budget_file = self.state_dir / "budget_usage.json"
        self.tracker = BudgetTracker(self.budget_file, job_limit_usd=0.05, daily_limit_usd=1.00)

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_credentials_raises_clear_error(self):
        """Missing API key produces actionable clear error without unhandled crash."""
        config = LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key=None)
        client = LLMClient(config=config, budget_tracker=self.tracker)
        with self.assertRaises(LLMError) as ctx:
            client.generate(prompt="Hello", job_id="job-1")
        self.assertEqual(ctx.exception.code, "CREDENTIALS_MISSING")
        self.assertIn("API key", ctx.exception.message)

    def test_invalid_model_id_rejected_without_fallback(self):
        """Model ID is verified; unknown or unconfigured provider has no implicit fallback."""
        config = LLMConfig(provider="gemini", model_id="unsupported-fake-model", api_key="test-key")
        client = LLMClient(config=config, budget_tracker=self.tracker)
        with self.assertRaises(LLMError) as ctx:
            client.generate(prompt="Hello", job_id="job-1")
        self.assertEqual(ctx.exception.code, "INVALID_MODEL")
        self.assertIn("unsupported-fake-model", ctx.exception.message)

    def test_unconfigured_provider_rejected_without_implicit_fallback(self):
        """No implicit fallback to unconfigured provider."""
        config = LLMConfig(provider="unconfigured_provider", model_id="gemini-2.5-flash", api_key="test-key")
        client = LLMClient(config=config, budget_tracker=self.tracker)
        with self.assertRaises(LLMError) as ctx:
            client.generate(prompt="Hello", job_id="job-1")
        self.assertEqual(ctx.exception.code, "PROVIDER_NOT_CONFIGURED")

    def test_timeout_and_network_error_handled_cleanly(self):
        """Timeout produces clear understandable error without leaking internals."""
        config = LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key="secret-key-12345", timeout_seconds=1)
        client = LLMClient(config=config, budget_tracker=self.tracker)

        with patch("urllib.request.urlopen") as mock_url:
            mock_url.side_effect = TimeoutError("Request timed out")
            with self.assertRaises(LLMError) as ctx:
                client.generate(prompt="Hello", job_id="job-1")
            self.assertEqual(ctx.exception.code, "TIMEOUT")
            self.assertIn("timed out", ctx.exception.message.lower())
            # Ensure api key is never in exception string
            self.assertNotIn("secret-key-12345", str(ctx.exception))

    def test_quota_exhausted_or_rate_limit_handled(self):
        """HTTP 429 quota exhaustion produces clear QUOTA_EXHAUSTED error."""
        config = LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key="secret-key-12345")
        client = LLMClient(config=config, budget_tracker=self.tracker)

        with patch("urllib.request.urlopen") as mock_url:
            err = urllib.error.HTTPError(
                url="https://generativelanguage.googleapis.com/v1beta/models/...",
                code=429,
                msg="Too Many Requests",
                hdrs={},
                fp=None,
            )
            mock_url.side_effect = err
            with self.assertRaises(LLMError) as ctx:
                client.generate(prompt="Hello", job_id="job-1")
            self.assertEqual(ctx.exception.code, "QUOTA_EXHAUSTED")
            self.assertNotIn("secret-key-12345", str(ctx.exception))

    def test_successful_gemini_call_records_usage_and_distinguishes_known_vs_estimated_cost(self):
        """Successful generation returns structured text and logs exact token counts and cost per job."""
        config = LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key="secret-key-12345")
        client = LLMClient(config=config, budget_tracker=self.tracker)

        gemini_response_payload = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "Ecco la risposta motivata."}],
                        "role": "model",
                    },
                    "finishReason": "STOP",
                }
            ],
            "usageMetadata": {
                "promptTokenCount": 120,
                "candidatesTokenCount": 45,
                "totalTokenCount": 165,
            },
        }

        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps(gemini_response_payload).encode("utf-8")
        mock_resp.__enter__.return_value = mock_resp

        with patch("urllib.request.urlopen", return_value=mock_resp):
            res = client.generate(prompt="Analizza i requisiti del progetto", job_id="job-101")
            self.assertEqual(res["text"], "Ecco la risposta motivata.")
            self.assertEqual(res["provider"], "gemini")
            self.assertEqual(res["model_id"], "gemini-2.5-flash")
            self.assertEqual(res["job_id"], "job-101")
            
            usage = res["usage"]
            self.assertEqual(usage["prompt_tokens"], 120)
            self.assertEqual(usage["completion_tokens"], 45)
            self.assertEqual(usage["total_tokens"], 165)
            self.assertFalse(usage["is_estimated"])
            self.assertGreater(usage["cost_usd"], 0.0)

            # Check audit and budget tracking
            job_usage = self.tracker.get_job_usage("job-101")
            self.assertEqual(job_usage["total_tokens"], 165)
            self.assertAlmostEqual(job_usage["total_cost_usd"], usage["cost_usd"])
            
            # Verify no secret leaked in tracker audit logs
            tracker_data = json.loads(self.budget_file.read_text(encoding="utf-8"))
            self.assertNotIn("secret-key-12345", json.dumps(tracker_data))

    def test_cli_gemini_demo(self):
        """CLI gemini-demo executes cleanly and demonstrates credentials check, model validation, and tracking."""
        import subprocess
        proc = subprocess.run(
            ["uv", "run", "jarvis", "gemini-demo"],
            capture_output=True,
            text=True,
            check=True
        )
        data = json.loads(proc.stdout)
        self.assertEqual(data["scope"], "isolated_gemini_demo")
        self.assertTrue(data["missing_credentials_handled"])
        self.assertTrue(data["invalid_model_no_fallback"])
        self.assertEqual(data["tokens_recorded"], 210)
        self.assertTrue(data["budget_tracked"])

    def test_job_budget_limit_enforced(self):
        """Exceeding job budget limit prevents new paid calls and raises BUDGET_EXCEEDED."""
        # job_limit_usd set to 0.0001 (very low)
        low_tracker = BudgetTracker(self.budget_file, job_limit_usd=0.00001)
        config = LLMConfig(provider="gemini", model_id="gemini-2.5-flash", api_key="secret-key-12345")
        client = LLMClient(config=config, budget_tracker=low_tracker)

        # Pre-record high usage on job
        low_tracker.record_usage(
            job_id="job-budget-test",
            provider="gemini",
            model_id="gemini-2.5-flash",
            prompt_tokens=5000,
            completion_tokens=2000,
            cost_usd=0.005,
            is_estimated=False,
        )

        with self.assertRaises(LLMError) as ctx:
            client.generate(prompt="Another query", job_id="job-budget-test")
        self.assertEqual(ctx.exception.code, "BUDGET_EXCEEDED")
        self.assertIn("Job budget limit", ctx.exception.message)


if __name__ == "__main__":
    unittest.main()
