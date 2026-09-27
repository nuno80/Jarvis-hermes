"""Router and intent classifier with deterministic fast-path, Jev classifier, and fallback.

Traceability:
- Issue 16 / JARVIS-16 / J10
- Decision D01, D09, AT07
- Rule: Deterministic fast-path first (bypasses main LLM).
- Rule: Jev classification / scoring next; classification never grants permissions or authorization.
- Rule: Timeout / failure of Jev applies configured conservative fallback (Gemini/reasoning or deterministic error) with unchanged permissions.
- Rule: Usage and costs recorded per job.
"""
from dataclasses import dataclass
from enum import Enum
import json
import os
import re
import urllib.error
import urllib.request
from typing import Any, Callable

from .llm import BudgetTracker, LLMClient, LLMConfig, LLMError
from .telemetry import RoutingDecisionStore


class RoutingTarget(str, Enum):
    DETERMINISTIC = "deterministic"
    SYSTEM_1 = "system_1"
    SYSTEM_2 = "system_2"
    REASONING_LLM = "reasoning_llm"
    TOOL_WORKFLOW = "tool_workflow"
    CLARIFICATION = "clarification"


class RouterError(Exception):
    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"[{code}] {message}")


@dataclass
class IntentRoute:
    intent: str
    target: RoutingTarget
    confidence: float
    handler: str | None
    used_fast_path: bool
    classifier: str  # "deterministic_rule", "jev", "conservative_fallback"
    fallback_applied: bool = False
    details: dict[str, Any] | None = None


# Sample dataset of Italian user requests with expected classification / routing
# Used for repeatable testing, calibration, and benchmarks
ITALIAN_ROUTING_DATASET = [
    # 1. Deterministic fast-paths (system status, disk, git status readonly commands)
    {
        "query": "quanto spazio libero ho sul disco?",
        "expected_intent": "disk_usage",
        "expected_target": RoutingTarget.DETERMINISTIC,
        "can_fast_path": True,
        "handler": "disk_usage",
    },
    {
        "query": "stato della macchina e memoria",
        "expected_intent": "device_status",
        "expected_target": RoutingTarget.DETERMINISTIC,
        "can_fast_path": True,
        "handler": "device_status",
    },
    {
        "query": "qual è il git status del progetto?",
        "expected_intent": "repo_status",
        "expected_target": RoutingTarget.DETERMINISTIC,
        "can_fast_path": True,
        "handler": "repo_status",
    },
    # 2. Tool workflows (destructive or multi-step protected operations)
    {
        "query": "fai push su origin main del commit approvato",
        "expected_intent": "git_push",
        "expected_target": RoutingTarget.TOOL_WORKFLOW,
        "can_fast_path": False,
        "handler": "git_push",
    },
    {
        "query": "cerca voli per Bali per 2 persone con date flessibili",
        "expected_intent": "travel_search",
        "expected_target": RoutingTarget.TOOL_WORKFLOW,
        "can_fast_path": False,
        "handler": "travel_search",
    },
    # 3. Reasoning / LLM queries (explanations, architecture, open questions)
    {
        "query": "spiegami la differenza architetturale tra MCP stdio e HTTP",
        "expected_intent": "explain_concept",
        "expected_target": RoutingTarget.REASONING_LLM,
        "can_fast_path": False,
        "handler": "gemini_reasoning",
    },
    {
        "query": "scrivi una proposta di refactoring per separare il client dal server",
        "expected_intent": "code_architecture",
        "expected_target": RoutingTarget.REASONING_LLM,
        "can_fast_path": False,
        "handler": "gemini_reasoning",
    },
    # 4. Ambiguous queries requiring clarification or calibrated thresholds
    {
        "query": "fai quella cosa",
        "expected_intent": "ambiguous",
        "expected_target": RoutingTarget.CLARIFICATION,
        "can_fast_path": False,
        "handler": "ask_clarification",
    },
    {
        "query": "cancella tutto",
        "expected_intent": "dangerous_ambiguous",
        "expected_target": RoutingTarget.CLARIFICATION,
        "can_fast_path": False,
        "handler": "ask_clarification",
    },
]


# Deterministic pattern matchers for fast-path
FAST_PATH_RULES = [
    (re.compile(r"(quanto\s+spazio|spazio\s+libero|disk\s+usage|\bdf\b)", re.IGNORECASE), "disk_usage", "disk_usage"),
    (re.compile(r"(stato\s+(della\s+)?macchina|device\s+status|\buptime\b|memoria\s+libera)", re.IGNORECASE), "device_status", "device_status"),
    (re.compile(r"(git\s+status|stato\s+(del\s+)?(repo|progetto))", re.IGNORECASE), "repo_status", "repo_status"),
]


class JevClient:
    """Client for Jev classification and scoring API."""
    def __init__(self, endpoint_url: str | None = None, api_key: str | None = None, timeout_seconds: float = 3.0):
        self.endpoint_url = endpoint_url or os.environ.get("JEV_ENDPOINT_URL")
        self.api_key = api_key or os.environ.get("JEV_API_KEY")
        self.timeout_seconds = timeout_seconds

    def classify(self, text: str, job_id: str = "default-job") -> dict[str, Any]:
        """Call Jev classification service.
        
        Returns dict with:
        - intent: str
        - target: str (deterministic, reasoning_llm, tool_workflow, clarification)
        - confidence: float (0.0 to 1.0)
        - handler: str | None
        """
        if not self.endpoint_url:
            raise RouterError("JEV_NOT_CONFIGURED", "Jev endpoint URL is not configured.")

        payload = {
            "text": text,
            "job_id": job_id,
            "language": "it",
        }
        data_bytes = json.dumps(payload).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        req = urllib.request.Request(self.endpoint_url, data=data_bytes, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout_seconds) as resp:
                resp_data = json.loads(resp.read().decode("utf-8"))
                return resp_data
        except urllib.error.HTTPError as exc:
            if exc.code == 429:
                raise RouterError("JEV_RATE_LIMIT", "Jev rate limit exceeded.") from None
            raise RouterError("JEV_API_ERROR", f"Jev returned HTTP {exc.code}.") from None
        except (TimeoutError, urllib.error.URLError) as exc:
            if isinstance(exc, TimeoutError) or "timed out" in str(exc).lower():
                raise RouterError("JEV_TIMEOUT", "Jev classification request timed out.") from None
            raise RouterError("JEV_NETWORK_ERROR", f"Failed to reach Jev: {exc}") from None
        except Exception as exc:
            raise RouterError("JEV_ERROR", f"Unexpected Jev error: {exc}") from None


class RequestRouter:
    """Jarvis Request Router.
    
    1. Checks deterministic fast path. If matched, returns immediately avoiding the main LLM.
    2. If Jev is configured, invokes Jev classifier and evaluates calibrated confidence thresholds.
    3. If Jev fails, times out, or is not configured, applies conservative fallback without lowering permissions.
    """
    def __init__(
        self,
        jev_client: JevClient | None = None,
        llm_client: LLMClient | None = None,
        budget_tracker: BudgetTracker | None = None,
        telemetry_store: RoutingDecisionStore | None = None,
        confidence_threshold: float = 0.70,
    ):
        self.jev_client = jev_client
        self.llm_client = llm_client
        self.budget_tracker = budget_tracker
        self.telemetry_store = telemetry_store
        self.confidence_threshold = confidence_threshold

    def route(self, text: str, job_id: str = "default-job") -> IntentRoute:
        import time
        start_time = time.perf_counter()

        def _record(route_obj: IntentRoute, latency_s1: float = 0.0, latency_first_msg: float = 0.0, tokens: int = 0, cost: float = 0.0):
            if self.telemetry_store:
                try:
                    self.telemetry_store.record_decision(
                        request=text,
                        domande=["intent", "target", "confidence", "handler"],
                        valori={"intent": route_obj.intent, "target": route_obj.target.value, "handler": route_obj.handler},
                        confidenze={"confidence": route_obj.confidence},
                        percorso=route_obj.target.value,
                        modello=route_obj.classifier,
                        latenza_system_1_ms=latency_s1,
                        latenza_first_msg_ms=latency_first_msg,
                        tokens=tokens,
                        costo=cost,
                        esito="success",
                    )
                except Exception:
                    pass

        # Step 1: Deterministic fast-path check
        clean_text = text.strip()

        for pattern, intent, handler in FAST_PATH_RULES:
            if pattern.search(clean_text):
                res = IntentRoute(
                    intent=intent,
                    target=RoutingTarget.DETERMINISTIC,
                    confidence=1.0,
                    handler=handler,
                    used_fast_path=True,
                    classifier="deterministic_rule",
                    fallback_applied=False,
                )
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                _record(res, latency_s1=0.0, latency_first_msg=elapsed_ms, tokens=0, cost=0.0)
                return res

        # Record non-deterministic routing / System 1 call in budget tracker per job
        prompt_tokens = max(1, len(clean_text) // 4)
        if self.budget_tracker:
            self.budget_tracker.record_usage(
                job_id=job_id,
                provider="routing_system_1",
                model_id="system_1_classifier",
                prompt_tokens=prompt_tokens,
                completion_tokens=0,
                cost_usd=0.0,
                is_estimated=True,
            )

        # Step 2: Try Jev if configured
        if self.jev_client and self.jev_client.endpoint_url:
            try:
                jev_res = self.jev_client.classify(clean_text, job_id=job_id)
                # Fail-closed: missing/NaN/inf/out-of-range/non-numeric confidence never grants fast path (AT14)
                raw_conf = jev_res.get("confidence")
                if isinstance(raw_conf, bool) or not isinstance(raw_conf, (int, float)):
                    confidence = float("nan")
                else:
                    confidence = float(raw_conf)
                if not (0.0 <= confidence <= 1.0):  # NaN fails both comparisons -> invalid
                    res = IntentRoute(
                        intent=jev_res.get("intent", "unknown") if isinstance(jev_res.get("intent"), str) else "unknown",
                        target=RoutingTarget.CLARIFICATION,
                        confidence=0.0,
                        handler="ask_clarification",
                        used_fast_path=False,
                        classifier="jev",
                        fallback_applied=True,
                        details={"reason": "invalid_confidence", "original_target": jev_res.get("target")},
                    )
                    elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                    _record(res, latency_s1=elapsed_ms, latency_first_msg=elapsed_ms, tokens=prompt_tokens, cost=0.0)
                    return res
                intent = jev_res.get("intent", "unknown")
                target_str = jev_res.get("target", "reasoning_llm")
                handler = jev_res.get("handler")
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0

                # If confidence is below calibrated threshold, route to clarification
                if confidence < self.confidence_threshold:
                    res = IntentRoute(
                        intent=intent,
                        target=RoutingTarget.CLARIFICATION,
                        confidence=confidence,
                        handler="ask_clarification",
                        used_fast_path=False,
                        classifier="jev",
                        fallback_applied=False,
                        details={"reason": "confidence_below_threshold", "original_target": target_str},
                    )
                    _record(res, latency_s1=elapsed_ms, latency_first_msg=elapsed_ms, tokens=prompt_tokens, cost=0.0)
                    return res

                try:
                    target = RoutingTarget(target_str)
                except ValueError:
                    target = RoutingTarget.REASONING_LLM

                res = IntentRoute(
                    intent=intent,
                    target=target,
                    confidence=confidence,
                    handler=handler,
                    used_fast_path=False,
                    classifier="jev",
                    fallback_applied=False,
                    details=jev_res.get("details"),
                )
                _record(res, latency_s1=elapsed_ms, latency_first_msg=elapsed_ms, tokens=prompt_tokens, cost=0.0)
                return res
            except RouterError as err:
                # Jev failed or timed out: fall back conservatively
                res = self._conservative_fallback(clean_text, job_id=job_id, error_cause=str(err))
                elapsed_ms = (time.perf_counter() - start_time) * 1000.0
                _record(res, latency_s1=elapsed_ms, latency_first_msg=elapsed_ms, tokens=prompt_tokens, cost=0.0)
                return res

        # Step 3: Default conservative fallback (no Jev available)
        res = self._conservative_fallback(clean_text, job_id=job_id, error_cause="JEV_NOT_CONFIGURED")
        elapsed_ms = (time.perf_counter() - start_time) * 1000.0
        _record(res, latency_s1=elapsed_ms, latency_first_msg=elapsed_ms, tokens=prompt_tokens, cost=0.0)
        return res

    def _conservative_fallback(self, text: str, job_id: str, error_cause: str) -> IntentRoute:
        """Conservative fallback when Jev is unavailable or fails.
        
        Permits reasoning or tool workflows via default Gemini / Hermes reasoning,
        or clarification if ambiguous, without granting any permissions.
        """
        lower = text.lower()

        # Check for obvious ambiguous / short inputs
        tokens = lower.split()
        if len(tokens) <= 3 and any(t in ("cosa", "fai", "tutto", "quello", "quella", "cancella") for t in tokens):
            return IntentRoute(
                intent="ambiguous_query",
                target=RoutingTarget.CLARIFICATION,
                confidence=0.5,
                handler="ask_clarification",
                used_fast_path=False,
                classifier="conservative_fallback",
                fallback_applied=True,
                details={"error_cause": error_cause, "policy_note": "conservative_fallback_clarification"},
            )

        # Keyword heuristics for safe fallback routing
        if any(w in lower for w in ("volo", "voli", "viaggio", "bali", "biglietto")):
            return IntentRoute(
                intent="travel_search",
                target=RoutingTarget.TOOL_WORKFLOW,
                confidence=0.75,
                handler="travel_search",
                used_fast_path=False,
                classifier="conservative_fallback",
                fallback_applied=True,
                details={"error_cause": error_cause},
            )

        if any(w in lower for w in ("push", "commit", "git push", "deploy")):
            return IntentRoute(
                intent="git_workflow",
                target=RoutingTarget.TOOL_WORKFLOW,
                confidence=0.75,
                handler="git_push",
                used_fast_path=False,
                classifier="conservative_fallback",
                fallback_applied=True,
                details={"error_cause": error_cause},
            )

        # Default fallback is reasoning with LLM (e.g. Gemini)
        return IntentRoute(
            intent="general_reasoning",
            target=RoutingTarget.REASONING_LLM,
            confidence=0.6,
            handler="gemini_reasoning",
            used_fast_path=False,
            classifier="conservative_fallback",
            fallback_applied=True,
            details={"error_cause": error_cause},
        )
