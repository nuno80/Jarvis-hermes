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
Stadio 2 live (B-full, 2026-10-04 21:00 CEST, patch host + plugin `c22a973`):
nuovo hook Hermes `post_stt_enrichment` (sparato dopo lo STT, prima
dell'echo; fail-open; prima direttiva vince: `skip`/`rewrite`/`reply`/
`suppress_echo`) + `post_stt_enrichment_hook` nel plugin che invia il Talker
sul testo trascritto. Evidenza: `post_stt_enrichment firing transcripts=1` →
`jarvis post-stt talker status=working job=job-bb09509c254ccdc0 chars=34` →
`post_stt_enrichment rewrite` + ack `🎙️ Ho trascritto… (job …)` in chat prima
della risposta Reasoner (32.1 s). Patch host (fuori repo, backup
`*.bak-jarvis-poststt`): `plugins.py` VALID_HOOKS, `plugins_activation.py`
transforms, `run_inbound.py` (`_hm_post_stt_apply` once-per-event +
innesti `_enrich_inbound_voice`/`_transcribe_and_echo_pending_voice` + drop
`None` in `_prepare_inbound_message_text`), `run_turn.py` (drain None-safe).
`stt.local.language: 'it'` in `~/.hermes/config.yaml` (auto-detect → inglese).

## Conseguenze

- Durata max 300 s (Telegram rende i clip lunghi come 0:00): oltre → reinvio,
  fail-closed, consumo comunque registrato. Trascrizione vuota → reinvio.
- Trascrizione ambigua su parametri importanti (intent ambiguo o hint di
  effetto esterno) → chiarimento prima di qualsiasi effetto; nessun consenso
  desunto dall'audio (D07: zero `ApprovalStore`/token/elicit nel modulo).
- Retention esplicita: `audio_bytes_stored_by_jarvis=false`; l'audio resta
  solo nella cache Hermes, nel job solo la trascrizione.
- Budget esaurito → `LLMError` dal tracker, mai risultati simulati.
- Selezione Provider STT (issue #45): Groq STT (`whisper-large-v3`) come
  primario cloud ad alta accuratezza e latenza sub-secondo con chiave
  `GROQ_API_KEY` in env; fallback trasparente su faster-whisper locale
  (`small`, `it`) in caso di assenza chiave o esaurimento quota. Zero-secret:
  chiave mai esposta in telemetria o metadati dei job.
- Demo: `uv run jarvis voice-demo` (stato macchina + latenze ack misurate).
