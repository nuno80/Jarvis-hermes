# Jev & Conservative Routing Integration

Traceability: JARVIS-16 / J10 / AT07 / D01, D09.

## Summary

The request router directs incoming Italian queries across three tiers:

1. **Deterministic Fast Path:** Matches deterministic read-only patterns (such as disk usage, machine status, repo status). Crucially, this fast path completely bypasses both the Jev classifier and the main LLM (Gemini), resulting in zero external token spend or API latency.
2. **Jev Intent Classifier & Scoring:** When configured via `JEV_ENDPOINT_URL`, classifies incoming queries, returning intent, target, and confidence score. If the confidence falls below the calibrated threshold (default 0.70), it routes safely to clarification (`CLARIFICATION`).
3. **Conservative Fallback:** If Jev is unconfigured, unreachable, times out, or returns an error, the router applies a conservative fallback. Ambiguous or potentially risky queries are routed to clarification rather than execution. Permissions and approvals remain invariant: classification never grants permissions or bypasses the approval gate.

## Reproducible Italian Benchmark Dataset

The routing logic is verified against a calibrated Italian request dataset (`ITALIAN_ROUTING_DATASET` in `src/jarvis_hermes/router.py`), testing:

- Deterministic commands: `quanto spazio libero ho sul disco?`, `stato della macchina e memoria`, `qual è il git status del progetto?` -> Routed to deterministic fast path, LLM bypassed.
- Multi-step / Protected workflows: `fai push su origin main del commit approvato`, `cerca voli per Bali per 2 persone con date flessibili` -> Routed to tool workflows.
- Reasoning requests: `spiegami la differenza architetturale tra MCP stdio e HTTP` -> Routed to reasoning LLM.
- Ambiguous / Dangerous requests: `fai quella cosa`, `cancella tutto` -> Routed to clarification.

## Isolated Verification Demo

Execute:

```bash
uv run jarvis routing-demo
```

Output:

```json
{
  "scope": "isolated_routing_demo",
  "fast_path_verified": true,
  "fast_path_bypassed_llm": true,
  "dataset_sample_count": 9,
  "jev_timeout_fallback_applied": true,
  "fallback_target": "reasoning_llm",
  "ambiguous_fallback_clarification": true,
  "classification_cannot_authorize": true
}
```
