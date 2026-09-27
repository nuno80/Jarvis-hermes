# ADR 0007: Modello locale System 1 e riepiloghi — scelta qwen3.5:4b

**Stato:** Accettata.
**Data:** 27 settembre 2026.
**Contesto:** Issue #17 (JARVIS-17; J10; D09, D13; E3; appendice I2). Bloccata da #15 (chiusa). Dataset #38/#40 già su main (246 richieste, soglie per classe). Benchmark su host reale: Ollama, RTX 5070 12 GB, `temperature=0`, `think=false`, `keep_alive=10m`, script stdlib-only `tests/test_ollama_models.py` (30 casi italiani, validazione fail-closed AT14: `intent ∈ ALLOWED_INTENTS`, `confidence` finita in `[0,1]`, altrimenti risposta errata).

## Decisione

1. **System 1 default: `qwen3.5:4b`** (4.7B, Q4_K_M, ctx 262k). Unico modello con latenza System 1 accettabile; l'unico errore è un fallback `ambiguous` fail-safe (caso 05), mai un'azione protetta sbagliata.
2. **`qwen3.5:9b`** (9.7B, Q4_K_M) resta opzione per riepiloghi profondi / ragionamento locale dove la latenza pesa meno. Non è il default del fast path.
3. Nessun fallback cloud forzato: modello indisponibile o output invalido → escalation al System 2, permessi invariati (AT07/AT14).

## Evidenze (run 27/09/2026, riproducibili)

| Metrica | qwen3.5:4b | qwen3.5:9b |
|---|---|---|
| Accuracy (30 casi) | 29/30 (96,7 %), errore solo 05 (`ambiguous`→`git_status`) | 30/30 (100 %) |
| JSON valid / coerenza / protected errors | 100 % / 100 % / 0 | 100 % / 100 % / 0 |
| ECE (10 bin) | 0,313 | 0,192 |
| Total p50 / p95 | 332,8 / 369,9 ms | 675,7 / 747,6 ms |
| Eval p50 / p95 | 141,2 / 157,4 ms | 441,7 / 497,0 ms |
| VRAM post-run (`nvidia-smi`) | ~9,2 / 12,2 GB | ~9,9 / 12,2 GB |

Il total p50 del 4b (332,8 ms) supera l'obiettivo I2 (≤ 300 ms); la sola generazione è a 141,2 ms p50 (il resto è load/prompt_eval per chiamata). Il 9b costa ~2× la latenza: inaccettabile per il fast path.

Riproduzione: `python3 tests/test_ollama_models.py --model qwen3.5:4b --json-out results/…` (default `--model qwen3.5:4b`). JSON dei due run allegati alla issue #17 (`results/` non committato, vedi `.gitignore`).

## Limiti dichiarati

- Misurato solo `intent` su 30 casi di confronto; le altre domande I1 (`risk`, `complexity`, binary) e le 246 richieste etichettate con split train/calibration/test e varianti semantiche sono lavoro della **#31** (che questa ADR sblocca), non di #17.
- Ricalibrare soglie a ogni cambio di modello, prompt o domande (appendice I2).

## Conseguenze

- La #31 implementa il decisore con `qwen3.5:4b` come default e soglie da `config/routing_thresholds.json`.
- `results/benchmark-*.json` resta evidenza allegata alle issue, mai committata.
