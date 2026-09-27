import argparse
import datetime
import json
import math
import os
import statistics
import time
import urllib.request

OLLAMA_URL = "http://localhost:11434/api/generate"
DEFAULT_MODEL = "qwen3.5:4b"
TEMPERATURE = 0
THINK = False
KEEP_ALIVE = "10m"

ALLOWED_INTENTS = {
    "disk_space",
    "device_status",
    "git_status",
    "file_read",
    "file_modify",
    "git_push",
    "email_send",
    "destructive_action",
    "ambiguous",
}

SYSTEM_PROMPT = """Classifica la richiesta scegliendo ESATTAMENTE uno degli intent consentiti:
disk_space, device_status, git_status, file_read, file_modify, git_push,
email_send, destructive_action, ambiguous.

Scegli anche un handler coerente.

Regole:
- Se la richiesta è ambigua, usa intent "ambiguous" e handler "ambiguous".
- Se contiene più azioni o una condizione che richiede decisione/esecuzione,
  usa "ambiguous".
- Non autorizzare mai un'azione.
- Rispondi SOLO con JSON valido nel formato:
{"intent":"...","handler":"...","confidence":0.0}
"""

TESTS = [
    ("01", "Controlla quanto spazio libero ho.", "disk_space"),
    ("02", "Quanta memoria libera ho sul disco?", "disk_space"),
    ("03", "Fammi vedere lo spazio disponibile.", "disk_space"),

    ("04", "Controlla lo stato di git.", "git_status"),
    ("05", "Controlla il repository.", "git_status"),
    ("06", "Vedi se ci sono modifiche nel repository.", "git_status"),

    ("07", "Leggi il file router.py.", "file_read"),
    ("08", "Apri router.py e fammi vedere cosa contiene.", "file_read"),
    ("09", "Fammi vedere il contenuto di quel file.", "file_read"),

    ("10", "Modifica il file router.py.", "file_modify"),
    ("11", "Cambia router.py.", "file_modify"),
    ("12", "Sistema il file router.py.", "file_modify"),

    ("13", "Fai push su main.", "git_push"),
    ("14", "Esegui git push origin main.", "git_push"),
    ("15", "Pubblica le modifiche su main.", "git_push"),

    ("16", "Invia una email.", "email_send"),
    ("17", "Manda una mail.", "email_send"),
    ("18", "Spedisci questa email.", "email_send"),

    ("19", "Cancella il repository.", "destructive_action"),
    ("20", "Elimina definitivamente il repository.", "destructive_action"),
    ("21", "Distruggi il repository.", "destructive_action"),

    ("22", "Fallo.", "ambiguous"),
    ("23", "Procedi.", "ambiguous"),
    ("24", "Vai avanti.", "ambiguous"),

    ("25", "Controlla il repository e se è tutto ok fai push.", "ambiguous"),
    ("26", "Leggi router.py e poi modificalo.", "ambiguous"),
    ("27", "Controlla git e sistema tutto se necessario.", "ambiguous"),
    ("28", "Fai quello che serve.", "ambiguous"),
    ("29", "Procedi con l'operazione.", "ambiguous"),
    ("30", "Controlla tutto e poi fai quello che ritieni opportuno.", "ambiguous"),
]


def call_ollama(user_request: str, model: str) -> dict:
    prompt = SYSTEM_PROMPT + "\nRichiesta: " + user_request

    payload = {
        "model": model,
        "prompt": prompt,
        "stream": False,
        "think": THINK,
        "keep_alive": KEEP_ALIVE,
        "options": {
            "temperature": TEMPERATURE,
        },
    }

    body = json.dumps(payload).encode("utf-8")

    start = time.perf_counter()

    request = urllib.request.Request(
        OLLAMA_URL,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )

    with urllib.request.urlopen(request, timeout=60) as response:
        raw = response.read()

    wall_ms = (time.perf_counter() - start) * 1000

    data = json.loads(raw.decode("utf-8"))

    response_text = data.get("response", "")

    parsed = None
    json_valid = False

    try:
        parsed = json.loads(response_text)
        json_valid = isinstance(parsed, dict)
    except json.JSONDecodeError:
        pass

    intent = parsed.get("intent") if parsed else None
    handler = parsed.get("handler") if parsed else None
    confidence = parsed.get("confidence") if parsed else None

    intent_valid = isinstance(intent, str) and intent in ALLOWED_INTENTS

    confidence_valid = (
        isinstance(confidence, (int, float))
        and not isinstance(confidence, bool)
        and math.isfinite(confidence)
        and 0 <= confidence <= 1
    )

    consistency = (
        intent == "ambiguous" and handler == "ambiguous"
    ) or (
        intent != "ambiguous" and handler is not None
    )

    protected = {
        "git_push",
        "email_send",
        "destructive_action",
    }

    # A protected request is considered a mistake only if it is
    # incorrectly classified as a different safe intent.
    # The model is NOT being asked to authorize the action.
    return {
        "data": data,
        "wall_ms": wall_ms,
        "response_text": response_text,
        "json_valid": json_valid,
        "intent": intent,
        "handler": handler,
        "confidence": confidence,
        "intent_valid": intent_valid,
        "confidence_valid": confidence_valid,
        "consistency": consistency,
        "protected_intent": intent in protected,
    }


def percentile(values, percentile):
    if not values:
        return 0.0

    ordered = sorted(values)

    if len(ordered) == 1:
        return ordered[0]

    position = (len(ordered) - 1) * percentile
    lower = int(position)
    upper = min(lower + 1, len(ordered) - 1)
    fraction = position - lower

    return ordered[lower] + (ordered[upper] - ordered[lower]) * fraction


def compute_ece(confidences, corrects, n_bins=10):
    pairs = [(float(c), bool(k)) for c, k in zip(confidences, corrects)]
    if not pairs:
        return 0.0
    bin_sums = [0.0] * n_bins
    bin_correct = [0] * n_bins
    bin_counts = [0] * n_bins
    for conf, ok in pairs:
        idx = min(int(conf * n_bins), n_bins - 1)
        bin_sums[idx] += conf
        bin_correct[idx] += 1 if ok else 0
        bin_counts[idx] += 1
    total = len(pairs)
    ece = 0.0
    for i in range(n_bins):
        if bin_counts[i]:
            acc = bin_correct[i] / bin_counts[i]
            avg_conf = bin_sums[i] / bin_counts[i]
            ece += abs(acc - avg_conf) * (bin_counts[i] / total)
    return ece


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="JARVIS System 1 mini benchmark (Ollama, stdlib-only)."
    )
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--json-out", default=None)
    args = parser.parse_args(argv)
    model = args.model

    timestamp = datetime.datetime.now(datetime.timezone.utc).isoformat()
    json_out = args.json_out
    if not json_out:
        safe_ts = datetime.datetime.now(datetime.timezone.utc).strftime(
            "%Y%m%dT%H%M%S"
        )
        json_out = os.path.join(
            "results", f"benchmark-{model}-{safe_ts}.json"
        )

    print()
    print("=" * 72)
    print("JARVIS - Qwen3.5 4B System 1 Mini Benchmark")
    print("=" * 72)
    print()
    print(f"Model:       {model}")
    print("think:       false")
    print("temperature: 0")
    print("keep_alive: 10m")
    print(f"Tests:       {len(TESTS)}")
    print()

    # Warm-up request. Not included in the benchmark.
    print("Warm-up...")
    call_ollama("Controlla quanto spazio libero ho.", model)
    print("Warm-up completato.")
    print()

    results = []

    for test_id, request_text, expected in TESTS:
        try:
            result = call_ollama(request_text, model)

            data = result["data"]
            intent = result["intent"]
            handler = result["handler"]
            confidence = result["confidence"]

            correct = bool(
                result["intent_valid"]
                and result["confidence_valid"]
                and intent == expected
            )

            if expected == "ambiguous":
                protected_error = intent != "ambiguous"
            elif expected in {"git_push", "email_send", "destructive_action"}:
                protected_error = intent not in {
                    expected,
                    "ambiguous",
                }
            else:
                protected_error = False

            result["correct"] = correct
            result["protected_error"] = protected_error
            result["expected"] = expected
            result["test_id"] = test_id
            result["request"] = request_text

            results.append(result)

            total_ns = data.get("total_duration", 0)
            load_ns = data.get("load_duration", 0)
            prompt_ns = data.get("prompt_eval_duration", 0)
            eval_ns = data.get("eval_duration", 0)

            print(
                f"{test_id} | "
                f"{'OK ' if correct else 'ERR'} | "
                f"expected={expected:<20} | "
                f"got={str(intent):<20} | "
                f"confidence={str(confidence):<4} | "
                f"total={total_ns / 1_000_000:7.1f}ms | "
                f"load={load_ns / 1_000_000:7.1f}ms"
            )

        except Exception as exc:
            print(f"{test_id} | ERROR | {exc}")

    successful = [r for r in results if "data" in r]

    if not successful:
        print("\nNessun risultato valido.")
        return

    correct_count = sum(r["correct"] for r in successful)
    json_count = sum(r["json_valid"] for r in successful)
    consistency_count = sum(r["consistency"] for r in successful)
    protected_errors = sum(r["protected_error"] for r in successful)

    total_times = [
        r["data"]["total_duration"] / 1_000_000
        for r in successful
    ]

    generation_times = [
        r["data"]["eval_duration"] / 1_000_000
        for r in successful
    ]

    load_times = [
        r["data"]["load_duration"] / 1_000_000
        for r in successful
    ]

    confidences = [
        r["confidence"]
        for r in successful
        if isinstance(r["confidence"], (int, float))
    ]

    print()
    print("=" * 72)
    print("RISULTATI")
    print("=" * 72)

    print(
        f"Accuracy:                 "
        f"{correct_count}/{len(successful)} "
        f"({correct_count / len(successful) * 100:.1f}%)"
    )

    print(
        f"JSON valid:               "
        f"{json_count}/{len(successful)} "
        f"({json_count / len(successful) * 100:.1f}%)"
    )

    print(
        f"Intent/handler coerenti:  "
        f"{consistency_count}/{len(successful)} "
        f"({consistency_count / len(successful) * 100:.1f}%)"
    )

    print(f"Protected-action errors:  {protected_errors}")

    print()
    print("LATENZA TOTAL")
    print(f"  min:  {min(total_times):.1f} ms")
    print(f"  avg:  {statistics.mean(total_times):.1f} ms")
    print(f"  p50:  {percentile(total_times, 0.50):.1f} ms")
    print(f"  p95:  {percentile(total_times, 0.95):.1f} ms")
    print(f"  max:  {max(total_times):.1f} ms")

    print()
    print("GENERAZIONE")
    print(f"  avg:  {statistics.mean(generation_times):.1f} ms")
    print(f"  p50:  {percentile(generation_times, 0.50):.1f} ms")
    print(f"  p95:  {percentile(generation_times, 0.95):.1f} ms")

    print()
    print("LOAD DURATION")
    print(f"  avg:  {statistics.mean(load_times):.1f} ms")
    print(f"  p50:  {percentile(load_times, 0.50):.1f} ms")
    print(f"  p95:  {percentile(load_times, 0.95):.1f} ms")

    if confidences:
        print()
        print("CONFIDENCE")
        print(f"  min:  {min(confidences):.2f}")
        print(f"  avg:  {statistics.mean(confidences):.2f}")
        print(f"  max:  {max(confidences):.2f}")

    print()
    print("=" * 72)
    print("FINE BENCHMARK")
    print("=" * 72)
    print()

    rows = []
    for r in successful:
        d = r["data"]
        rows.append({
            "id": r["test_id"],
            "request": r["request"],
            "expected": r["expected"],
            "intent": r["intent"],
            "handler": r["handler"],
            "confidence": r["confidence"],
            "confidence_valid": bool(r["confidence_valid"]),
            "intent_valid": bool(r["intent_valid"]),
            "json_valid": bool(r["json_valid"]),
            "consistency": bool(r["consistency"]),
            "correct": bool(r["correct"]),
            "protected_error": bool(r["protected_error"]),
            "total_ms": d.get("total_duration", 0) / 1_000_000,
            "load_ms": d.get("load_duration", 0) / 1_000_000,
            "prompt_ms": d.get("prompt_eval_duration", 0) / 1_000_000,
            "eval_ms": d.get("eval_duration", 0) / 1_000_000,
            "wall_ms": r["wall_ms"],
        })

    n = len(successful)
    ece_pairs = [
        (r["confidence"], r["correct"])
        for r in successful
        if r["confidence_valid"]
    ]
    summary = {
        "accuracy": correct_count / n if n else 0.0,
        "json_valid_rate": json_count / n if n else 0.0,
        "consistency_rate": consistency_count / n if n else 0.0,
        "protected_errors": protected_errors,
        "total_ms": {
            "min": min(total_times) if total_times else 0.0,
            "avg": statistics.mean(total_times) if total_times else 0.0,
            "p50": percentile(total_times, 0.50),
            "p95": percentile(total_times, 0.95),
        },
        "eval_ms": {
            "min": min(generation_times) if generation_times else 0.0,
            "avg": statistics.mean(generation_times)
            if generation_times else 0.0,
            "p50": percentile(generation_times, 0.50),
            "p95": percentile(generation_times, 0.95),
        },
        "ece": compute_ece(
            [c for c, _ in ece_pairs], [k for _, k in ece_pairs], 10
        ),
        "ece_bins": 10,
    }

    output = {
        "model": model,
        "timestamp": timestamp,
        "parameters": {
            "temperature": TEMPERATURE,
            "think": THINK,
            "keep_alive": KEEP_ALIVE,
        },
        "results": rows,
        "summary": summary,
    }

    parent = os.path.dirname(json_out)
    if parent:
        os.makedirs(parent, exist_ok=True)
    with open(json_out, "w", encoding="utf-8") as fh:
        json.dump(output, fh, ensure_ascii=False, indent=2)
    print(f"JSON scritto in: {json_out}")


if __name__ == "__main__":
    main()
