# ADR 0011: Google Calendar come connettore agenda per J16 / Issue #23

- **Stato:** Accettato (scelta del proprietario, 2026-10-08)
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** J16; priorità 5; B3; Issue #23 (blocca #24)

## Contesto

J16 richiede lettura agenda, bozza e invio/creazione con conferma su un account
reale con consenso OAuth. La spec (§5 `personal-mcp`, §12.3) chiede di scegliere
servizi e autorizzazioni in discovery; senza credenziali la funzione resta
`NOT_CONFIGURED` (milestone Core), non simulata.

## Decisione

1. **Provider: Google Calendar API v3** (`GET /calendar/v3/calendars/{id}/events`,
   `singleEvents=true`, `orderBy=startTime`), nessun SDK: solo `urllib` come
   `travel.py`/`web.py`.
2. **Scope minimo: `calendar.readonly`** per la #23 (solo lettura). La scrittura
   (#24) richiederà scope/consenso separato e conferma per evento.
3. **Credenziali solo da env**: `GOOGLE_CALENDAR_ACCESS_TOKEN` (token OAuth con
   scope readonly) oppure tripletta `GOOGLE_CALENDAR_CLIENT_ID` /
   `GOOGLE_CALENDAR_CLIENT_SECRET` / `GOOGLE_CALENDAR_REFRESH_TOKEN` per rinnovo
   automatico. Mai in vault, log o messaggi. Timezone default configurabile
   (`JARVIS_CALENDAR_TIMEZONE`, default `Europe/Rome`).
4. **Fail-closed come ADR 0008/0009**: senza credenziali `PROVIDER_NOT_CONFIGURED`;
   401/403 → `PROVIDER_AUTH_ERROR` (accesso revocato); 404 → `CALENDAR_NOT_FOUND`;
   429/5xx → `PROVIDER_UNAVAILABLE` retryable. Mai eventi fittizi.
5. **Bozza sempre locale**: `draft_calendar_event` valida e normalizza senza rete
   (`created_event: false`); la creazione resta alla #24 con conferma.
6. **Contenuti non fidati** (AT05): eventi marcati `untrusted_content`, mai istruzioni.
7. **Dipendenza `tzdata` su Windows**: `zoneinfo` richiede il DB IANA, assente su
   Windows (CI `windows-latest`); dipendenza condizionale in `pyproject.toml`.

## Conseguenze

- Tool MCP: `calendar_search` (lettura) + `draft_calendar_event` (bozza locale).
- Resta da fare dal proprietario: creare il client OAuth Google, eseguire il
  consenso una tantum, impostare le env su host reale; poi demo live e prove
  host (ora legale, accesso revocato, calendario vuoto) per chiudere la #23.
