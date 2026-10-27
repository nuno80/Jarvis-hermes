# ADR 0014: Reminder esplicito persistente e consegna dopo riavvio per J17 / Issue #27

- **Stato:** Accettato (2026-10-10, aggiornato con Fix 11 / Issue #66)
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** J17; D11; AT12; Issue #27; Issue #66 (Follow-up #57)

## Contesto

La specifica J17 (§2 priorità 5, §5 `personal-mcp`, §12.3), la decisione architetturale D11
e il criterio di accettazione AT12 richiedono che:
1. Una richiesta esplicita programmi un reminder Telegram consultabile e annullabile.
2. Nessun monitoraggio spontaneo o proattività non richiesta (D11): vengono gestiti solo i reminder esplicitamente richiesti dall'utente.
3. Timezone esplicita/confermata e scadenza assoluta memorizzata in UTC; comportamento corretto al cambio ora legale (DST) (AT12).
4. Persistenza locale SQLite su filesystem: i reminder sopravvivono al riavvio del server/nodo.
5. Reminder scaduto durante lo spegnimento/downtime segue una policy dichiarata e deterministica:
   viene notificato/marcato come scaduto durante il downtime (`missed_during_downtime`), evitando retry ciechi o loop di ri-notifica indefinita.
6. Eventuale consegna incerta di Telegram non viene spacciata per garanzia exactly-once: il sistema traccia lo stato della consegna e registra esiti incerti se Telegram fallisce o non conferma.
7. Nessun falso `delivered`: un claim atomico porta il reminder da `scheduled` a `sending`, l'invio al chat associato all'attore avviene fuori dalla transazione con sender obbligatorio, e un crash durante `sending` viene riconciliato all'avvio come esito incerto (`delivery_uncertain`) senza retry automatico.

## Decisione

1. **Storage persistente SQLite (`ReminderStore`)**:
   - Tabella `reminders` memorizzata in `$XDG_STATE_HOME/jarvis-hermes/reminders.sqlite3`.
   - Campi: `reminder_id`, `actor_id`, `text`, `due_at_utc` (ISO 8601 UTC o timestamp epoch),
     `timezone_str` (es. `Europe/Rome`), `scheduled_local_str`, `status` (`scheduled`, `sending`, `fired`, `cancelled`, `missed_during_downtime`),
     `created_at`, `updated_at`, `delivery_status` (`pending`, `sending`, `delivered`, `delivery_failed`, `delivery_uncertain`),
     `delivery_metadata_json`.

2. **Timezone e Cambio Ora Legale (DST) (AT12)**:
   - Utilizzo dello standard library `zoneinfo.ZoneInfo` (con dipendenza condizionale da `tzdata` su Windows già presente).
   - Registrazione sia dell'orario locale richiesto che della scadenza assoluta in UTC.
   - I calcoli tengono conto delle transizioni di ora legale (es. passaggio a ora solare/legale a fine marzo e fine ottobre) senza salti temporali errati.

3. **Integrazione con il ciclo di vita dei Job e Consultazione/Annullamento**:
   - Tool MCP `create_reminder`: programma un nuovo reminder esplicito con scadenza assoluta e timezone.
   - Tool MCP `list_reminders`: consulta i reminder attivi o storici filtrati per stato (`status=scheduled`).
   - Tool MCP `cancel_reminder`: annulla un reminder pianificato prima della scadenza; i reminder in stato `sending` o terminale non possono essere annullati.
   - Funzione di riconciliazione all'avvio `reconcile_reminders_on_startup(now_utc)`:
     - Reminder rimasti in stato `sending` (interrotti da un crash durante l'invio HTTP) vengono riconciliati come terminali `fired` con `delivery_status = 'delivery_uncertain'`, prevenendo retry ciechi o doppi invii.
     - Reminder con `due_at_utc <= now_utc` che erano ancora in stato `scheduled` vengono identificati come scaduti a nodo spento (`missed_during_downtime`).

4. **Consegna Telegram, Claim Atomico e Scheduler di Produzione**:
   - Claim atomico `claim_due_reminders`: transiziona atomicamente i reminder scaduti da `scheduled` a `sending`. Due o più dispatcher concorrenti non possono acquisire lo stesso record.
   - Sender obbligatorio in `dispatch_due`: `sender_func` è richiesto; l'assenza di sender solleva errore e non può marcare `delivered`.
   - L'invio Telegram viene eseguito fuori dalla transazione SQLite al chat associato all'attore (`actor_id`).
   - Timeout o disconnessioni di rete durante la richiesta HTTP registrano `delivery_status = 'delivery_uncertain'` (senza retry automatico cieco).
   - `ReminderScheduler`: singolo loop in background supervisionato e arrestabile (tramite `threading.Event`), avviato in `main()` durante l'esecuzione del server di produzione. Nessun secondo poller Hermes viene avviato.

5. **CLI e accettazione**:
   - Comando CLI `jarvis reminder-demo` per dimostrare la pianificazione, il test di cambio DST, l'annullamento e la riconciliazione dopo riavvio simulato.

## Conseguenze

- Modulo `src/jarvis_hermes/reminder.py` implementato con `ReminderStore`, stato `sending`, claim atomico, `ReminderScheduler` e `send_telegram_reminder`.
- Tool MCP in `src/jarvis_hermes/server.py`: `create_reminder`, `list_reminders`, `cancel_reminder` e avvio supervisionato dello scheduler in `main()`.
- Test esaustivi in `tests/test_reminder.py` a copertura di J17, D11, AT12 e Fix 11 (#66).
