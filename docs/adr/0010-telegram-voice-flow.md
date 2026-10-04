# ADR 0010: Flusso vocale Telegram (issue #18 / JARVIS-18)

**Stato:** Approvato (implementato con demo isolata + hook live su host reale).
**Data:** 4 ottobre 2026.
**Evidenza live 2026-10-04 18:46 CEST:** vocale 4 s da Telegram → probe
`mtype=VOICE text_len=0 duration=4` (STT Hermes gira dopo il hook) → ricevuta
`🎙️ Vocale ricevuto (4 s)` inviata <1 s, dispatch proseguito (`msg=''` →
Reasoner, risposta dopo 25.0 s). Conseguenza: ack sul contenuto trascritto
impossibile al `pre_gateway_dispatch`; servono due stadi (vedi sotto) con
hook post-STT lato Hermes come follow-up.
**Contesto:** J11, D03, storie 2 e 11, sezione 7 (Talker–Reasoner). Bloccato da
#15 (Gemini + budget, CHIUSA) e #31 (System 1, CHIUSA).

## Decisione

Lo STT resta di Hermes: cache Telegram (`.ogg`) → faster-whisper locale
(`stt: provider local` in `~/.hermes/config.yaml`); Jarvis riceve solo la
trascrizione e la tratta come dato non fidato (AT05). Un modulo
`src/jarvis_hermes/voice.py` (dict-in/dict-out, riusa `BudgetTracker`,
`JobStore`, `pre_turn_dispatch`) fa: validazione → consumo STT per job →
System 1 → ack Talker / risposta / chiarimento. Il plugin `jarvis-s1`
(`pre_gateway_dispatch`) lavora in due stadi: (1) testo vuoto (caso live:
STT non ancora girato) → ricevuta immediata `🎙️ Vocale ricevuto (N s)`
entro 3 s, nessun esito dichiarato, `None` = dispatch normale che prosegue;
(2) testo gia presente (caption) → ack Talker diretto, `skip` solo se la
risposta fast e gia stata inviata in chat, altrimenti `rewrite` sul testo
trascritto (il Reasoner deve girare); `rewrite`/`skip` preservano
auth/pairing e prefix cache. Troppo-lungo fail-closed anche senza
transcript (durata da `raw_message.voice/audio.duration`).
`transcribe_voice_message` espone lo stesso flusso come tool MCP readonly.
Follow-up B (fuori da questo repo): hook Hermes post-STT per lo stadio 2
sul testo trascritto (ack Talker sul contenuto + risposta verificata).

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
