# ADR 0016: Selezione Provider Task e Liste (Todoist) per Issue #47

- **Stato:** Accettato
- **Data:** 2026-10-18
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** Issue #47; correlata a Issue #46 (#46 risolta, prerequisito)

## Contesto e Problema

Quando l'utente richiede di creare una lista (es. "crea una lista della spesa", "crea una lista per il weekend"), Jarvis in precedenza produceva una risposta testuale fittizia in chat Telegram (falso successo senza invocare alcun tool, log `agent.log` `api_calls=1`, zero tool call).

È necessario integrare un provider reale di gestione to-do / liste, seguendo il pattern dei provider già definiti (ADR 0008 travel, ADR 0009 web search, ADR 0011 calendar):
1. Distinguere rigorosamente le "note" nel vault personale (gestite da `note_create` via `write_note`, #46) dalle "liste / task" da sincronizzare su mobile.
2. Valutare alternative per la gestione di task e liste.
3. Selezionare il provider e definire il protocollo di interazione, credenziali, fail-fast e politica di conferma.

## Alternative Valutate

1. **Todoist REST API v2 (Scelta):**
   - *Autenticazione:* Personal API Token statico (`TODOIST_API_TOKEN`) da Impostazioni > Integrazioni > Sviluppatori. Nessun flusso OAuth interattivo o refresh token complesso per un bot personale.
   - *Modello dati:* Gerarchia naturale `Project` (corrispondente a lista utente, es. "Spesa") e `Task` (elementi della lista con `content` e `project_id`). Possibilità di inserire task direttamente nel default Inbox.
   - *Esperienza mobile:* Client nativo per iOS, Android, smartwatch e web con sincronizzazione immediata e widget to-do.
   - *Reversibilità:* I task e i progetti creati sono facilmente spuntabili, archiviabili o cancellabili direttamente dall'app Todoist, senza impatti distruttivi irreversibili.

2. **Google Tasks API:**
   - *Svantaggi:* Richiede setup OAuth2 (client ID, client secret, refresh token, consenso in Google Cloud Console), overhead sproporzionato rispetto a un API token personale. Interfaccia mobile meno flessibile per liste condivise rapide.

3. **Note nel Vault Obsidian (estensione di #46):**
   - *Svantaggi:* Le note locali nel vault (Markdown) sono ottime per memoria a lungo termine e appunti personali, ma offrono una pessima esperienza utente per liste della spesa o to-do in mobilità (nessun widget nativo sullo smartphone, potenziali conflitti di sincronizzazione git/cloud).

4. **Microsoft To-Do / Microsoft Graph:**
   - *Svantaggi:* Autenticazione complessa tramite Azure AD / Microsoft Entra ID con tenant e permessi dedicati.

## Decisione

1. **Provider Primario:** **Todoist** tramite la API v1 unificata (`https://api.todoist.com/api/v1`), usando `urllib.request` standard (nessun SDK esterno). A febbraio 2026 Todoist ha effettuato il sunset della vecchia REST API v2 (restituendo HTTP 410 Gone).
2. **Credenziali:** Letta esclusivamente dalla variabile d'ambiente `TODOIST_API_TOKEN` (o `TODOIST_API_KEY` come alias). Mai salvata in vault, mai esposta in log, URL o messaggi (AT13, filtrata da `redact_secrets`).
3. **Fail-Closed / No Mock:** In assenza di `TODOIST_API_TOKEN`, il tool e la CLI sollevano immediatamente `PROVIDER_NOT_CONFIGURED` o `CREDENTIALS_REQUIRED` indicando le istruzioni di configurazione. Non vengono mai generate o simulate checklist fittizie in chat.
4. **Politica di Conferma:** Operazione mutativa reversibile: non richiede conferma interattiva (i task creati sono cancellabili dall'app Todoist).
5. **Disambiguazione Routing (#46 vs #47):**
   - "crea una nota ..." → intent `note_create`, handler `note_create`, skill `memory` (System 2 scrive nel vault via `write_note`).
   - "crea/aggiungi ... lista ..." → intent `list_create`, handler `list_create`, skill `tasks` (System 2 invoca il tool MCP `todoist_create_task_list`).
   - Le due espressioni regolari in `FAST_PATH_RULES` sono mutuamente esclusive e non catturano richieste GUI ("notepad").
6. **Tool MCP:** `todoist_create_task_list(name, tasks, project_id)`:
   - Se `name` è fornito e non è "inbox", crea il progetto su Todoist (se non esistente).
   - Se sono forniti `tasks`, crea i relativi task all'interno del progetto (o nell'Inbox di default).

## Conseguenze

- Aggiunto intent e handler `list_create` e skill `tasks` in `src/jarvis_hermes/decision.py`.
- Regola fast-path deterministica in `src/jarvis_hermes/router.py`.
- Nuovo modulo `src/jarvis_hermes/todoist.py` e tool MCP `todoist_create_task_list` in `server.py`.
- Ispezione provider in `jarvis doctor` (`report["providers"]["todoist"]`).
