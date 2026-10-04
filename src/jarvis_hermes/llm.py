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
import sqlite3
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
        # Use SQLite database for concurrency and transactional atomicity
        if self.storage_path.suffix == ".json":
            self.db_path = self.storage_path.with_suffix(".sqlite3")
        else:
            self.db_path = self.storage_path
        self._init_db()
        self._sync_legacy_json()

    def _sync_legacy_json(self) -> None:
        """Keep optional JSON mirror or empty JSON file for backward compatibility if configured as .json."""
        if self.storage_path.suffix == ".json":
            try:
                with self._get_connection() as conn:
                    rows = conn.execute("SELECT * FROM budget_records").fetchall()
                    records = [dict(r) for r in rows]
                self.storage_path.write_text(json.dumps({"records": records}, indent=2), encoding="utf-8")
            except Exception:
                pass

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.db_path), timeout=30.0, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS budget_records (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    model_id TEXT NOT NULL,
                    prompt_tokens INTEGER NOT NULL,
                    completion_tokens INTEGER NOT NULL,
                    total_tokens INTEGER NOT NULL,
                    cost_usd REAL NOT NULL,
                    is_estimated INTEGER NOT NULL,
                    day_utc TEXT NOT NULL,
                    timestamp TEXT NOT NULL
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_job_id ON budget_records(job_id)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_records_day ON budget_records(day_utc)")

    def check_budget_available(self, job_id: str | None = None) -> None:
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        with self._get_connection() as conn:
            row_daily = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM budget_records WHERE day_utc = ?",
                (today,)
            ).fetchone()
            daily_used = float(row_daily["total"]) if row_daily else 0.0
            if daily_used >= self.daily_limit_usd:
                raise LLMError(
                    "DAILY_BUDGET_EXCEEDED",
                    f"Daily LLM budget limit reached (${daily_used:.4f} >= ${self.daily_limit_usd:.4f})."
                )

            if job_id:
                row_job = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM budget_records WHERE job_id = ?",
                    (job_id,)
                ).fetchone()
                job_used = float(row_job["total"]) if row_job else 0.0
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
        rounded_cost = round(cost_usd, 6)

        conn = self._get_connection()
        try:
            # Atomically check limits and record usage under BEGIN IMMEDIATE
            conn.execute("BEGIN IMMEDIATE")
            row_daily = conn.execute(
                "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM budget_records WHERE day_utc = ?",
                (today,)
            ).fetchone()
            daily_used = float(row_daily["total"]) if row_daily else 0.0
            if daily_used >= self.daily_limit_usd:
                conn.execute("ROLLBACK")
                raise LLMError(
                    "DAILY_BUDGET_EXCEEDED",
                    f"Daily LLM budget limit reached (${daily_used:.4f} >= ${self.daily_limit_usd:.4f})."
                )

            if job_id:
                row_job = conn.execute(
                    "SELECT COALESCE(SUM(cost_usd), 0.0) as total FROM budget_records WHERE job_id = ?",
                    (job_id,)
                ).fetchone()
                job_used = float(row_job["total"]) if row_job else 0.0
                if job_used >= self.job_limit_usd:
                    conn.execute("ROLLBACK")
                    raise LLMError(
                        "BUDGET_EXCEEDED",
                        f"Job budget limit reached for {job_id} (${job_used:.4f} >= ${self.job_limit_usd:.4f})."
                    )

            conn.execute(
                """
                INSERT INTO budget_records (
                    job_id, provider, model_id, prompt_tokens, completion_tokens,
                    total_tokens, cost_usd, is_estimated, day_utc, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job_id, provider, model_id, prompt_tokens, completion_tokens,
                    total_tokens, rounded_cost, 1 if is_estimated else 0, today, now_iso
                )
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            conn.close()

        self._sync_legacy_json()

        return UsageRecord(
            job_id=job_id,
            provider=provider,
            model_id=model_id,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            cost_usd=rounded_cost,
            is_estimated=is_estimated,
            timestamp=now_iso,
        )

    def get_job_usage(self, job_id: str) -> dict[str, Any]:
        with self._get_connection() as conn:
            row = conn.execute(
                """
                SELECT
                    COALESCE(SUM(total_tokens), 0) as total_tokens,
                    COALESCE(SUM(cost_usd), 0.0) as total_cost_usd,
                    COUNT(*) as calls_count
                FROM budget_records
                WHERE job_id = ?
                """,
                (job_id,)
            ).fetchone()
            if not row:
                return {"total_tokens": 0, "total_cost_usd": 0.0, "calls_count": 0}
            return {
                "total_tokens": int(row["total_tokens"]),
                "total_cost_usd": round(float(row["total_cost_usd"]), 6),
                "calls_count": int(row["calls_count"]),
            }


class STTClient:
    """STT (Speech-to-Text) adapter recording per-job budget usage.

    Lo STT reale e di Hermes (voice.py: solo trascrizione in ingresso);
    qui si registrano errore/consumo per job (#18): fail-closed su budget
    esaurito (LLMError dal tracker), mai risultati simulati.
    """
    COST_USD_PER_SECOND = 0.006 / 60.0
    TOKENS_PER_SECOND = 4

    def __init__(self, budget_tracker: BudgetTracker, model_id: str = "whisper-1"):
        self.budget_tracker = budget_tracker
        self.model_id = model_id

    def record_transcription(self, job_id: str, duration_seconds: float = 0,
                             provider: str = "stt") -> UsageRecord:
        """Registra consumo STT per job; ritorna UsageRecord."""
        if duration_seconds is None:
            duration_seconds = 0
        if duration_seconds < 0:
            raise LLMError("STT_INVALID_DURATION", "STT duration must be >= 0.")
        duration = max(1, int(duration_seconds) or 1)
        return self.budget_tracker.record_usage(
            job_id=job_id,
            provider=provider,
            model_id=self.model_id,
            prompt_tokens=duration * self.TOKENS_PER_SECOND,
            completion_tokens=0,
            cost_usd=duration * self.COST_USD_PER_SECOND,
            is_estimated=True,
        )

    def transcribe(self, audio_bytes: bytes, job_id: str = "default-job") -> str:
        duration_seconds = max(1, len(audio_bytes) // 32000)
        self.record_transcription(job_id, duration_seconds)
        return ""


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

        # 5. Execute request - API key transmitted via x-goog-api-key header, not in URL
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{self.config.model_id}:generateContent"
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
            headers={
                "Content-Type": "application/json",
                "x-goog-api-key": api_key,
            },
            method="POST",
        )

        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_seconds) as resp:
                raw_response = resp.read().decode("utf-8")
                resp_json = json.loads(raw_response)
        except urllib.error.HTTPError as exc:
            # Clean error without sensitive details
            if exc.code == 429:
                raise LLMError("QUOTA_EXHAUSTED", "Gemini quota exhausted / rate limit reached.") from None
            if exc.code in (401, 403):
                raise LLMError("AUTHENTICATION_FAILED", "Gemini authentication failed.") from None
            raise LLMError("API_ERROR", f"Gemini API returned HTTP {exc.code}.") from None
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise LLMError("TIMEOUT", "Gemini request timed out.") from None
            raise LLMError("NETWORK_ERROR", "Network connection error to Gemini.") from None
        except Exception:
            raise LLMError("UNEXPECTED_ERROR", "Unexpected error during Gemini call.") from None

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
