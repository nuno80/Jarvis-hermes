"""LLM Provider Client and Budget Tracker.

Supports configured Gemini models (e.g. gemini-2.5-flash, gemini-1.5-flash, gemini-1.5-pro)
with strict error handling, cost/token accounting per job and daily limits,
and no implicit fallback to unconfigured providers.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import urllib.error
import urllib.request
from typing import Any

SUPPORTED_GEMINI_MODELS = {
    # Rates: (prompt_cost_per_million_tokens_usd, candidate_cost_per_million_tokens_usd)
    "gemini-2.5-flash": (0.075, 0.30),
    "gemini-1.5-flash": (0.075, 0.30),
    "gemini-1.5-pro": (1.25, 5.00),
    "gemini-3.8-flash": (0.075, 0.30),
}


class LLMError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass
class UsageRecord:
    job_id: str
    provider: str
    model_id: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    cost_usd: float
    is_estimated: bool
    timestamp: str


class BudgetTracker:
    def __init__(self, storage_path: Path, job_limit_usd: float = 0.50, daily_limit_usd: float = 5.00):
        self.storage_path = Path(storage_path)
        self.job_limit_usd = float(job_limit_usd)
        self.daily_limit_usd = float(daily_limit_usd)
        self.storage_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.storage_path.exists():
            self._save_data({"records": [], "job_totals": {}, "daily_totals": {}})

    def _load_data(self) -> dict[str, Any]:
        try:
            return json.loads(self.storage_path.read_text(encoding="utf-8"))
        except Exception:
            return {"records": [], "job_totals": {}, "daily_totals": {}}

    def _save_data(self, data: dict[str, Any]) -> None:
        self.storage_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def check_budget_available(self, job_id: str | None = None) -> None:
        data = self._load_data()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        daily_used = data.get("daily_totals", {}).get(today, 0.0)
        if daily_used >= self.daily_limit_usd:
            raise LLMError(
                "DAILY_BUDGET_EXCEEDED",
                f"Daily LLM budget limit reached (${daily_used:.4f} >= ${self.daily_limit_usd:.4f})."
            )

        if job_id:
            job_used = data.get("job_totals", {}).get(job_id, {}).get("total_cost_usd", 0.0)
            if job_used >= self.job_limit_usd:
                raise LLMError(
                    "BUDGET_EXCEEDED",
                    f"Job budget limit reached for {job_id} (${job_used:.4f} >= ${self.job_limit_usd:.4f})."
                )

    def record_usage(
        self,
        job_id: str,
        provider: str,
        model_id: str,
        prompt_tokens: int,
        completion_tokens: int,
        cost_usd: float,
        is_estimated: bool,
    ) -> UsageRecord:
        now_iso = datetime.now(timezone.utc).isoformat()
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        total_tokens = prompt_tokens + completion_tokens

        record = UsageRecord(
            job_id=job_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=round(cost_usd, 6),
            is_estimated=is_estimated,
            timestamp=now_iso,
        )

        data = self._load_data()
        records = data.setdefault("records", [])
        records.append({
            "job_id": record.job_id,
            "provider": record.provider,
            "model_id": record.model_id,
            "prompt_tokens": record.prompt_tokens,
            "completion_tokens": record.completion_tokens,
            "total_tokens": record.total_tokens,
            "cost_usd": record.cost_usd,
            "is_estimated": record.is_estimated,
            "timestamp": record.timestamp,
        })

        daily_totals = data.setdefault("daily_totals", {})
        daily_totals[today] = round(daily_totals.get(today, 0.0) + cost_usd, 6)

        job_totals = data.setdefault("job_totals", {})
        current_job = job_totals.get(job_id, {
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "calls_count": 0,
        })
        current_job["total_tokens"] += total_tokens
        current_job["total_cost_usd"] = round(current_job["total_cost_usd"] + cost_usd, 6)
        current_job["calls_count"] += 1
        job_totals[job_id] = current_job

        self._save_data(data)
        return record

    def get_job_usage(self, job_id: str) -> dict[str, Any]:
        data = self._load_data()
        return data.get("job_totals", {}).get(job_id, {
            "total_tokens": 0,
            "total_cost_usd": 0.0,
            "calls_count": 0,
        })


@dataclass
class LLMConfig:
    provider: str
    model_id: str
    api_key: str | None = None
    timeout_seconds: int = 30


class LLMClient:
    def __init__(self, config: LLMConfig, budget_tracker: BudgetTracker):
        self.config = config
        self.budget_tracker = budget_tracker

    def _calculate_gemini_cost(self, model_id: str, prompt_tokens: int, completion_tokens: int) -> tuple[float, bool]:
        if model_id in SUPPORTED_GEMINI_MODELS:
            prompt_rate, completion_rate = SUPPORTED_GEMINI_MODELS[model_id]
            cost = (prompt_tokens * prompt_rate / 1_000_000.0) + (completion_tokens * completion_rate / 1_000_000.0)
            return cost, False
        # If rate unknown, mark as estimated
        est_cost = (prompt_tokens + completion_tokens) * 0.000001
        return est_cost, True

    def generate(self, prompt: str, job_id: str = "default-job") -> dict[str, Any]:
        # 1. Verify provider
        if self.config.provider != "gemini":
            raise LLMError(
                "PROVIDER_NOT_CONFIGURED",
                f"Provider {self.config.provider!r} is not configured. No implicit fallback allowed."
            )

        # 2. Verify model ID
        if self.config.model_id not in SUPPORTED_GEMINI_MODELS:
            raise LLMError(
                "INVALID_MODEL",
                f"Model ID {self.config.model_id!r} is invalid or unsupported for provider {self.config.provider!r}."
            )

        # 3. Check credentials
        api_key = self.config.api_key or os.environ.get("GEMINI_API_KEY")
        if not api_key:
            raise LLMError(
                "CREDENTIALS_MISSING",
                "Google Gemini API key is missing. Set GEMINI_API_KEY in environment."
            )

        # 4. Check budget availability
        self.budget_tracker.check_budget_available(job_id)

        # 5. Execute request
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.model_id}:generateContent?key={api_key}"
        payload = {
            "contents": [
                {
                    "parts": [{"text": prompt}]
                }
            ]
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url,
            data=data_bytes,
            headers={"Content-Type": "application/json"},
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw_response = resp.read().decode("utf-8")
                resp_json = json.loads(raw_response)
        except urllib.error.HTTPError as exc:
            # Mask API key from error
            clean_reason = re.sub(r"key=[a-zA-Z0-9_\-]+", "key=[REDACTED]", str(exc))
            if exc.code == 429:
                raise LLMError("QUOTA_EXHAUSTED", f"Gemini quota exhausted / rate limit reached: {clean_reason}") from None
            if exc.code in (401, 403):
                raise LLMError("AUTHENTICATION_FAILED", f"Gemini authentication failed: {clean_reason}") from None
            raise LLMError("API_ERROR", f"Gemini API returned HTTP {exc.code}: {clean_reason}") from None
        except (TimeoutError, urllib.error.URLError) as exc:
            clean_str = re.sub(r"key=[a-zA-Z0-9_\-]+", "key=[REDACTED]", str(exc))
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise LLMError("TIMEOUT", f"Gemini request timed out: {clean_str}") from None
            raise LLMError("NETWORK_ERROR", f"Network connection error to Gemini: {clean_str}") from None
        except Exception as exc:
            clean_str = re.sub(r"key=[a-zA-Z0-9_\-]+", "key=[REDACTED]", str(exc))
            raise LLMError("UNEXPECTED_ERROR", f"Unexpected error during Gemini call: {clean_str}") from None

        # 6. Parse response & extract text and token metrics
        candidates = resp_json.get("candidates", [])
        if not candidates:
            raise LLMError("EMPTY_RESPONSE", "Gemini returned no candidates.")
        
        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts)

        usage_meta = resp_json.get("usageMetadata", {})
        prompt_tokens = int(usage_meta.get("promptTokenCount", len(prompt) // 4))
        completion_tokens = int(usage_meta.get("candidatesTokenCount", len(text) // 4))
        is_estimated = "promptTokenCount" not in usage_meta

        cost_usd, cost_estimated = self._calculate_gemini_cost(
            self.config.model_id, prompt_tokens, completion_tokens
        )
        if cost_estimated:
            is_estimated = True

        usage_rec = self.budget_tracker.record_usage(
            job_id=job_id,
            provider="gemini",
            model_id=self.config.model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            cost_usd=cost_usd,
            is_estimated=is_estimated,
        )

        return {
            "text": text,
            "provider": "gemini",
            "model_id": self.config.model_id,
            "job_id": job_id,
            "usage": {
                "prompt_tokens": usage_rec.prompt_tokens,
                "completion_tokens": usage_rec.completion_tokens,
                "total_tokens": usage_rec.total_tokens,
                "cost_usd": usage_rec.cost_usd,
                "is_estimated": usage_rec.is_estimated,
            },
        }
