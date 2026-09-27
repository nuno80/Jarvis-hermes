"""Calibration harness for the System 1 router (issue 40 / JARVIS-38).

Traceability:
- Issue 40 / JARVIS-38, Appendix I2, D13, AT13, AT14
- Versioned dataset: tests/data/routing_it.jsonl (>= 200 labeled Italian requests)
- Metrics: per-question accuracy, reliability curve, ECE, escalation rate,
  wrong fast paths on protected actions (goal: zero), per-action-class thresholds.
- Action classes: readonly / protected / reasoning / clarify.
"""
import json
import math
import random
from typing import Any

from .router import RoutingTarget

# I1 questions with numeric score ranges; others are exact-match strings.
SCORE_QUESTIONS = {"risk": 5, "complexity": 5}
I1_QUESTIONS = ["intent", "handler", "skill_hint", "needs_clarification",
                "external_effect", "private_data", "risk", "complexity"]

# Default per-action-class confidence thresholds, overridable via config file.
DEFAULT_THRESHOLDS = {"readonly": 0.70, "protected": 0.70, "reasoning": 0.70, "clarify": 0.70}
DEFAULT_THRESHOLD_PATH = "config/routing_thresholds.json"


class DatasetError(Exception):
    pass


def load_dataset(path: str | Any) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    with open(path, encoding="utf-8") as f:
        for lineno, line in enumerate(f, 1):
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise DatasetError(f"line {lineno}: invalid JSON: {exc}") from None
            if not isinstance(row.get("query"), str) or not row["query"].strip():
                raise DatasetError(f"line {lineno}: missing query")
            if not isinstance(row.get("expected_target"), str) or not row["expected_target"].strip():
                raise DatasetError(f"line {lineno}: missing expected_target")
            rows.append(row)
    if not rows:
        raise DatasetError("dataset is empty")
    return rows


def split_calibration_test(rows, test_fraction=0.25, seed=42):
    """Deterministic stratified-ish split by (query, intent) key."""
    keyed = [(r["query"], r.get("intent", ""), r) for r in rows]
    keyed.sort(key=lambda t: (t[0], t[1]))
    rng = random.Random(seed)
    cal, test = [], []
    for _, _, r in rng.sample(keyed, len(keyed)):
        (test if rng.random() < test_fraction else cal).append(r)
    return cal, test


def expected_action_class(row: dict[str, Any]) -> str:
    """Map a dataset row to an action class for threshold selection."""
    if row.get("external_effect") or (row.get("risk") or 0) >= 4:
        return "protected"
    if row.get("expected_target") == RoutingTarget.CLARIFICATION.value:
        return "clarify"
    if row.get("expected_target") == RoutingTarget.REASONING_LLM.value:
        return "reasoning"
    return "readonly"


def _answers_from_route(route) -> dict[str, Any]:
    details = getattr(route, "details", None) or {}
    answers = details.get("answers")
    return answers if isinstance(answers, dict) else {}


def _answers_correct(row, answers) -> dict[str, bool]:
    correct = {}
    for q in I1_QUESTIONS:
        pred = answers.get(q)
        exp = row.get(q)
        if pred is None:
            correct[q] = False
        elif q in SCORE_QUESTIONS:
            try:
                correct[q] = abs(float(pred) - float(exp)) <= 1.0
            except (TypeError, ValueError):
                correct[q] = False
        elif isinstance(exp, bool):
            correct[q] = isinstance(pred, bool) and pred == exp
        else:
            correct[q] = str(pred).strip().lower() == str(exp).strip().lower()
    return correct


def _ece(confidences: list[float], corrects: list[bool], n_bins: int = 10) -> tuple[float, list[dict]]:
    if not confidences:
        return 0.0, []
    bin_edges = [i / n_bins for i in range(n_bins + 1)]
    bins = [{"conf_sum": 0.0, "n": 0, "correct": 0} for _ in range(n_bins)]
    for c, ok in zip(confidences, corrects):
        b = min(n_bins - 1, max(0, int(c * n_bins)))
        bins[b]["conf_sum"] += c
        bins[b]["n"] += 1
        if ok:
            bins[b]["correct"] += 1
    curve, ece = [], 0.0
    for i, b in enumerate(bins):
        if b["n"] == 0:
            continue
        avg_conf = b["conf_sum"] / b["n"]
        acc = b["correct"] / b["n"]
        ece += (b["n"] / len(confidences)) * abs(acc - avg_conf)
        curve.append({"bin": i, "range": [bin_edges[i], bin_edges[i + 1]],
                      "count": b["n"], "avg_confidence": round(avg_conf, 3), "accuracy": round(acc, 3)})
    return round(ece, 4), curve


def protected_fast_path_errors(rows, router) -> list[dict[str, Any]]:
    """AT13: dataset rows for protected actions where the router took the fast path."""
    errors = []
    for row in rows:
        if expected_action_class(row) != "protected":
            continue
        try:
            route = router.route(row["query"])
        except Exception:
            continue
        if getattr(route, "used_fast_path", False):
            errors.append({"query": row["query"], "intent": row.get("intent"), "risk": row.get("risk")})
    return errors


def run_calibration(rows, router, test_fraction=0.25, seed=42,
                    thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    """Split, evaluate the router on the I1 questions, compute Appendix I2 metrics.

    `router` is a RequestRouter or any callable(query) -> route object with
    .confidence, .used_fast_path, .details["answers"].

    ponytail: automatic per-class threshold sweep is deferred until a real System 1
    model (issue 17) produces per-class confidences to sweep over; thresholds in
    config/routing_thresholds.json are chosen from this report's metrics meanwhile.
    """
    cal, test = split_calibration_test(rows, test_fraction=test_fraction, seed=seed)
    thresholds = thresholds or dict(DEFAULT_THRESHOLDS)

    def evaluate_subset(subset, route_fn):
        per_q = {q: {"total": 0, "correct": 0, "confs": [], "oks": []} for q in I1_QUESTIONS}
        escalations = 0
        for row in subset:
            route = route_fn(row)
            answers = _answers_from_route(route)
            conf = getattr(route, "confidence", 0.0)
            conf = conf if isinstance(conf, (int, float)) and math.isfinite(conf) else 0.0
            conf = min(1.0, max(0.0, float(conf)))
            correct = _answers_correct(row, answers)
            for q in I1_QUESTIONS:
                per_q[q]["total"] += 1
                per_q[q]["oks"].append(bool(correct[q]))
                per_q[q]["confs"].append(conf)
                if correct[q]:
                    per_q[q]["correct"] += 1
            target = getattr(route, "target", None)
            target_val = target.value if isinstance(target, RoutingTarget) else str(target or "")
            cls = expected_action_class(row)
            thr = thresholds.get(cls, thresholds.get("default", 0.70))
            if target_val == RoutingTarget.CLARIFICATION.value or conf < thr:
                escalations += 1
        return per_q, escalations

    def default_route_fn(row):
        if hasattr(router, "route"):
            return router.route(row["query"])
        return router(row["query"])

    cal_per_q, cal_escalations = evaluate_subset(cal, default_route_fn)
    test_per_q, test_escalations = evaluate_subset(test, default_route_fn)

    accuracy = {}
    for q in I1_QUESTIONS:
        d = test_per_q[q]
        n = d["total"]
        acc = d["correct"] / n if n else 0.0
        ece, curve = _ece(d["confs"], d["oks"])
        accuracy[q] = {
            "accuracy": round(acc, 4),
            "ece": ece,
            "reliability_curve": curve,
            "sample_count": n,
        }

    fast_errors_test = protected_fast_path_errors(test, router) if hasattr(router, "route") else []

    result = {
        "dataset_size": len(rows),
        "calibration_size": len(cal),
        "test_size": len(test),
        "accuracy_per_question": accuracy,
        "escalation_rate": round(test_escalations / len(test), 3) if test else 0.0,
        "escalation_rate_calibration": round(cal_escalations / len(cal), 3) if cal else 0.0,
        "protected_fast_path_errors_on_test": len(fast_errors_test),
        "protected_fast_path_errors_detail": fast_errors_test,
        "chosen_thresholds": thresholds,
        "calibration_split_seed": seed,
    }
    return result


def load_thresholds(path: str | None = None) -> dict[str, float]:
    """Load per-action-class thresholds from config file, falling back to defaults."""
    from pathlib import Path
    p = Path(path or DEFAULT_THRESHOLD_PATH)
    thresholds = dict(DEFAULT_THRESHOLDS)
    if p.is_file():
        try:
            data = json.loads(p.read_text(encoding="utf-8"))
            for k, v in data.items():
                if isinstance(v, (int, float)) and 0.0 <= float(v) <= 1.0:
                    thresholds[k] = float(v)
        except Exception:
            pass
    return thresholds
