# ADR 0015: Creazione Evento Calendario protetta con consenso ed esito verificato per J16 / Issue #24

- **Stato:** Accettato (2026-10-10)
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** J16; D07; AT04; Issue #24 (bloccata da #3, #4, #23)

## Contesto

La specifica J16 (§2 priorità 5, §5 `personal-mcp`, §12.3) e le decisioni D07 e ADR 0011 stabiliscono che:
1. La lettura agenda e bozza locale (#23) non creano eventi e non richiedono scope di scrittura.
2. La creazione di eventi nel calendario (#24) è un'operazione con effetto esterno protetta: richiede consenso esplicito del proprietario legato a calendario (`calendar_id`), contenuto (`summary`, `description`, `location`), orari (`start`, `end`) e invitati (`attendees`).
3. Qualsiasi modifica ai parametri invalida il consenso (consenso monouso legato al digest crittografico SHA-256).
4. Un eventuale rifiuto non deve scrivere su Google Calendar e deve produrre un riepilogo pulito degli effetti evitati.
5. In caso di timeout o errore durante la chiamata a Google Calendar (AT04), non eseguire retry cieco; registrare lo stato ed evitare duplicazioni su callback duplicate o riavvii (riconciliazione su audit log SQLite e digest/client-assigned ID).

## Decisione

1. **Provider Google Calendar API v3 (POST)**:
   - Endpoint: `POST https://www.googleapis.com/calendar/v3/calendars/{calendar_id}/events`.
   - Richiede scope di scrittura `https://www.googleapis.com/auth/calendar.events` (o `calendar`).
   - Credenziali solo da environment (`GOOGLE_CALENDAR_ACCESS_TOKEN` o OAuth refresh). Fail-closed se assenti.

2. **Digest e Gate di Approvazione (`ApprovalStore`)**:
   - `compute_calendar_event_digest` calcola l'hash deterministico SHA-256 di: `calendar_id`, `summary`, `start`, `end`, `timezone`, `location`, `description` e lista ordinata di email degli invitati.
   - `stage_calendar_create` prepara la richiesta in `ApprovalStore` con target `calendar_create:<calendar_id>` e arguments contenenti il digest.
   - Qualsiasi disallineamento tra i parametri approvati e quelli di esecuzione solleva `APPROVAL_DENIED`/`APPROVAL_EXPIRED`/`APPROVAL_MISMATCH`.

3. **Audit Log SQLite permanente e Idempotenza (AT04)**:
   - Tabella `created_events` in SQLite (`$XDG_STATE_HOME/jarvis-hermes/calendar_audit.sqlite3`).
   - Campi: `id`, `calendar_id`, `event_id`, `summary`, `start_time`, `end_time`, `digest`, `status` (`CREATED`, `OUTCOME_UNKNOWN`), `created_at`.
   - Callback duplicata con stesso digest/parametri: restituisce `ALREADY_CREATED`, `reconciled: True`, senza seconda POST a Google.
   - In caso di timeout durante la chiamata HTTP: registra `OUTCOME_UNKNOWN` e fallisce in modo sicuro senza retry cieco.
   - Tool `reconcile_calendar_event(digest)` per ispezione stato in caso di incertezza.

4. **MCP Tooling**:
   - `calendar_create_event` annotato con `destructive` e mediato da `ctx.elicit` per richiedere l'approvazione del proprietario Telegram.
   - `reconcile_calendar_event` annotato con `readonly`.

5. **CLI demo**:
   - Comando `jarvis calendar-create-demo` per dimostrare isolatamente il ciclo di approvazione, rifiuto, invalidazione parametri e persistenza audit.
