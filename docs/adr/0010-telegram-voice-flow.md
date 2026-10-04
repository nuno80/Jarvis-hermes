# ADR 0010: Flusso vocale Telegram (issue #18 / JARVIS-18)

**Stato:** Approvato (implementato con demo isolata + hook live su host reale).
**Data:** 4 ottobre 2026.
**Contesto:** J11, D03, storie 2 e 11, sezione 7 (Talker–Reasoner). Bloccato da
#15 (Gemini + budget, CHIUSA) e #31 (System 1, CHIUSA).

## Decisione

Lo STT resta di Hermes: cache Telegram (`.ogg`) → faster-whisper locale
(`stt: provider local` in `~/.hermes/config.yaml`); Jarvis riceve solo la
trascrizione e la tratta come dato non fidato (AT05). Un modulo
`src/jarvis_hermes/voice.py` (dict-in/dict-out, riusa `BudgetTracker`,
`JobStore`, `pre_turn_dispatch`) fa: validazione → consumo STT per job →
System 1 → ack Talker / risposta / chiarimento. Il plugin `jarvis-s1`
(`pre_gateway_dispatch`) invia l'ack entro 3 s: `skip` solo se la risposta
fast e gia stata inviata in chat (nessun lavoro residuo, come il fast-path
testuale), altrimenti `rewrite` sul testo trascritto (il Reasoner deve
girare); `rewrite`/`skip` preservano auth/pairing e prefix cache. `transcribe_voice_message` espone lo stesso
flusso come tool MCP readonly.

## Conseguenze

- Durata max 300 s (Telegram rende i clip lunghi come 0:00): oltre → reinvio,
  fail-closed, consumo comunque registrato. Trascrizione vuota → reinvio.
- Trascrizione ambigua su parametri importanti (intent ambiguo o hint di
  effetto esterno) → chiarimento prima di qualsiasi effetto; nessun consenso
  desunto dall'audio (D07: zero `ApprovalStore`/token/elicit nel modulo).
- Retention esplicita: `audio_bytes_stored_by_jarvis=false`; l'audio resta
  solo nella cache Hermes, nel job solo la trascrizione.
- Budget esaurito → `LLMError` dal tracker, mai risultati simulati.
- Demo: `uv run jarvis voice-demo` (stato macchina + latenze ack misurate).
