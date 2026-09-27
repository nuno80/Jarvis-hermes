"""Eval Laya fase 2 (issue #41): tutti i 246 casi di routing_it.jsonl.

Eseguire SOLO sull'host reale dopo smoke pass:
  LAYA_ENDPOINT_URL=http://localhost:8000/v1/systemone python3 scripts/laya_spike/eval.py [--json-out results/laya-eval-<ts>.json]

Metriche: accuracy argmax intent, ECE su answer_confidence (10 bin),
latenza p50/p95, protected-errors (AT13), copertura per intent.
Successo = accuracy >= baseline qwen sui 246 E p50 <= 300ms E zero protette.
Stdlib-only. Output JSON in results/ (git-ignored), mai committare.
"""
import datetime
import json
import os
import statistics
import sys
import time

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "src"))

from jarvis_hermes.laya import LayaClient, build_questions, parse_answers  # noqa: E402

DATASET = os.path.join(BASE, "tests", "data", "routing_it.jsonl")
PROTECTED_INTENTS = {"git_push", "run_command", "web_form_submit", "agenda_create"}


def compute_ece(confs, corrects, n_bins=10):
    pairs = [(float(c), bool(k)) for c, k in zip(confs, corrects)]
    if not pairs:
        return 0.0
    sums = [0.0] * n_bins
    hits = [0] * n_bins
    counts = [0] * n_bins
    for conf, hit in pairs:
        idx = min(int(conf * n_bins), n_bins - 1)
        sums[idx] += conf
        hits[idx] += 1 if hit else 0
        counts[idx] += 1
    total = len(pairs)
    return sum(abs(hits[i] / counts[i] - sums[i] / counts[i]) * (counts[i] / total)
               for i in range(n_bins) if counts[i])


def percentile(values, pct):
    ordered = sorted(values)
    if not ordered:
        return 0.0
    pos = (len(ordered) - 1) * pct
    lo = int(pos)
    hi = min(lo + 1, len(ordered) - 1)
    return ordered[lo] + (ordered[hi] - ordered[lo]) * (pos - lo)


def main(argv=None):
    argv = list(argv or [])
    json_out = None
    if "--json-out" in argv:
        json_out = argv[argv.index("--json-out") + 1]
    if not json_out:
        ts = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        json_out = os.path.join(BASE, "results", f"laya-eval-{ts}.json")
    endpoint = os.environ.get("LAYA_ENDPOINT_URL", "http://localhost:8000/v1/systemone")
    client = LayaClient(endpoint=endpoint)
    questions = build_questions()
    with open(DATASET, encoding="utf-8") as fh:
        cases = [json.loads(line) for line in fh if line.strip()]
    client.system_one({"text": "warm-up"}, questions)
    rows, lat, confs, corrects = [], [], [], []
    protected_errors = 0
    for i, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        try:
            payload = client.system_one({"text": case["query"]}, questions)
            raw, override = parse_answers(payload)
            got, conf = raw["intent"], override["intent"]
        except Exception as exc:
            got, conf = f"ERROR:{exc}", float("nan")
        ms = (time.perf_counter() - t0) * 1000.0
        lat.append(ms)
        good = got == case["intent"]
        if got == case["intent"]:
            pass
        prot_err = (case["intent"] in PROTECTED_INTENTS and got not in PROTECTED_INTENTS) or (
            case["intent"] not in PROTECTED_INTENTS and got in PROTECTED_INTENTS)
        protected_errors += 1 if prot_err else 0
        if isinstance(conf, float) and conf == conf:
            confs.append(conf)
            corrects.append(good)
        rows.append({"id": i, "query": case["query"], "expected": case["intent"],
                     "got": got, "confidence": conf, "correct": good,
                     "protected_error": bool(prot_err), "latency_ms": round(ms, 2)})
        if i % 50 == 0:
            print(f".. {i}/{len(cases)}")
    n = len(rows)
    ok = sum(r["correct"] for r in rows)
    summary = {
        "accuracy": ok / n if n else 0.0,
        "correct": ok, "total": n,
        "protected_errors": protected_errors,
        "latency_ms": {"p50": percentile(lat, 0.5), "p95": percentile(lat, 0.95),
                       "max": max(lat) if lat else 0.0},
        "ece_answer_confidence": compute_ece(confs, corrects),
    }
    print(f"accuracy: {ok}/{n} ({summary['accuracy']:.1%})")
    print(f"protected-errors: {protected_errors}")
    print(f"latenza p50: {summary['latency_ms']['p50']:.1f}ms "
          f"p95: {summary['latency_ms']['p95']:.1f}ms")
    print(f"ECE(answer_confidence): {summary['ece_answer_confidence']:.3f}")
    ok_gate = summary["accuracy"] >= 0.90 and protected_errors == 0 \
        and summary["latency_ms"]["p50"] <= 300.0
    print("GATE:", "PASS" if ok_gate else "FAIL")
    parent = os.path.dirname(json_out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as fh:
        json.dump({"endpoint": endpoint,
                   "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
                   "summary": summary, "results": rows},
                  fh, ensure_ascii=False, indent=2)
    print(f"JSON scritto in: {json_out} (non committare)")
    return 0 if ok_gate else 1


if __name__ == "__main__":
    raise SystemExit(main())
