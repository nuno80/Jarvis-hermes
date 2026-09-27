# ADR 0009: Spike Laya-multilingual come backend System 1 — esito negativo

- **Stato:** Accettato (scarta Laya, conferma qwen3.5:4b)
- **Data:** 2026-09-27
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** D13, sezione 7, appendice I; AT07/AT13/AT14; Ticket Issue #41

## Contesto

La #41 valutava `laya-multilingual` (single-forward, ~20 ms) come secondo backend del System 1 dietro il seam `decide(model_call=...)` della #31, con stop-loss: accuracy argmax < 90 % o qualsiasi errore su protette → STOP, niente fase 2. Default, soglie e policy restano di qwen3.5:4b (ADR-0007).

## Evidenze (run 27/09/2026, host reale RTX 5070, laya 0.3.20, checkpoint `convaiinnovations/laya` multilingual su CUDA)

Smoke `scripts/laya_spike/smoke.py`, 29 casi (1/intent da `tests/data/routing_it.jsonl`):

| Metrica | Laya-multilingual | Baseline qwen3.5:4b (ADR-0007) |
|---|---|---|
| Accuracy intent | 17/29 (**58,6 %**) | 29/30 (96,7 %) |
| Protected-errors | **3** (`ambiguous`→`run_command`, `dangerous_ambiguous`→`run_command`, `project_workflow`→`run_command`) | 0 |
| Latenza p50 / p95 | 20,0 / 20,7 ms | 332,8 / 369,9 ms totali |

Errori tipici: `brainstorm`→`project_workflow`, `code_architecture`→`writing`, `debug_help`→`general_question`, `device_status`→`memory_search`, `travel_compare`→`summarize`. Log completo non committato (`results/` è git-ignored).

## Decisione

1. **Stop-loss scattato su entrambi i criteri** (58,6 % < 90 %; 3 errori su protette): niente fase 2 (eval 246), niente temperature-fitting — la calibrazione non ripara un argmax sbagliato né un `ambiguous` classificato come azione distruttiva (AT13).
2. **qwen3.5:4b resta unico backend System 1.** La latenza 20 ms di Laya è irrilevante senza accuracy minima.
3. Il codice della spike (`src/jarvis_hermes/laya.py`, `confidence_override` in `decision.py`, script e test mock) resta su branch come riferimento riusabile per futuri backend senza logprob — ma il merge su main è opzionale e fuori onda 1/2.

## Conseguenze

- La #41 si chiude con questo ADR; nessuna modifica a soglie, policy o hook.
- Rivalutare solo se upstream rilascia checkpoint con accuracy zero-shot ≥ 90 % sul nostro dataset (rieseguire `smoke.py`, invariato).
