"""Smoke Laya (issue #41, fase 1): 30 casi stratificati da routing_it.jsonl.

Eseguire SOLO sull'host reale con laya-serve attivo (RTX 5070):
  LAYA_ENDPOINT_URL=http://localhost:8000/v1/systemone python3 scripts/laya_spike/smoke.py

Stop-loss dell'intesa: accuracy argmax intent < 90% -> STOP, niente fase 2.
Successo fase 1 = accuracy >= 90% E zero errori su protette; latenza solo
riportata (il gate p50 <= 300ms si applica sui 246 in fase 2).
Stdlib-only: nessuna dipendenza oltre il repo (come tests/test_ollama_models.py).
"""
import json
import os
import statistics
import sys
import time
import urllib.request

BASE = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(BASE, "src"))

from jarvis_hermes.laya import LayaClient, build_questions, parse_answers  # noqa: E402

DATASET = os.path.join(BASE, "tests", "data", "routing_it.jsonl")
PROTECTED_INTENTS = {"git_push", "run_command", "web_form_submit", "agenda_create"}
N_PER_INTENT = 1
SEED_ORDER = True


def load_cases():
    by_intent = {}
    with open(DATASET, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            by_intent.setdefault(row["intent"], []).append(row)
    cases = []
    for intent in sorted(by_intent):
        cases.extend(by_intent[intent][:N_PER_INTENT])
    return cases


def main():
    endpoint = os.environ.get("LAYA_ENDPOINT_URL", "http://localhost:8000/v1/systemone")
    client = LayaClient(endpoint=endpoint)
    questions = build_questions()
    try:
        with urllib.request.urlopen(endpoint.replace("/v1/systemone", "/health"),
                                     timeout=5) as resp:
            print("health:", resp.read().decode("utf-8")[:200])
    except Exception as exc:
        print(f"laya-serve non raggiungibile su {endpoint}: {exc}")
        return 2
    cases = load_cases()
    print(f"casi: {len(cases)} (1 per intent, {len(PROTECTED_INTENTS)} protetti)")
    # Warm-up escluso dalle misure.
    client.system_one({"text": "quanto spazio libero ho?"}, questions)
    ok = protected_errors = 0
    lat = []
    for i, case in enumerate(cases, 1):
        t0 = time.perf_counter()
        try:
            payload = client.system_one({"text": case["query"]}, questions)
            raw, _ = parse_answers(payload)
            got = raw["intent"]
        except Exception as exc:
            got = f"ERROR:{exc}"
        ms = (time.perf_counter() - t0) * 1000.0
        lat.append(ms)
        expected = case["intent"]
        good = got == expected
        ok += good
        prot_err = (expected in PROTECTED_INTENTS and got not in PROTECTED_INTENTS
                    and not got.startswith("ERROR")) or (
                        expected not in PROTECTED_INTENTS and got in PROTECTED_INTENTS)
        protected_errors += prot_err
        flag = "OK " if good else "ERR"
        print(f"{i:02d} | {flag} | expected={expected:<18} got={got:<18} | {ms:7.1f}ms"
              + (" | PROTECTED-ERR" if prot_err else ""))
    acc = ok / len(cases)
    print(f"\naccuracy: {ok}/{len(cases)} ({acc:.1%})")
    print(f"protected-errors: {protected_errors}")
    print(f"latenza p50: {statistics.median(lat):.1f}ms p95: "
          f"{sorted(lat)[min(len(lat) - 1, int(0.95 * len(lat)))]:.1f}ms")
    if acc < 0.90 or protected_errors:
        print("STOP-LOSS: niente fase 2.")
        return 1
    print("SMOKE PASS: procedere con fase 2 (eval 246).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
