# Inventario Tool MCP — Jarvis

> Generato automaticamente dal server FastMCP tramite `scripts/inventory_tools.py`.
> Totale tool registrati nel server: **50**.

## Matrice delle Capacità

Ogni tool espone un contratto tipizzato con schemi conformi al protocollo MCP.
La tabella seguente descrive per ciascuna capacità:
- **Politica di Conferma ed Effetti**: se l'operazione è read-only, bozza locale priva di rete, mediata da elicitazione nativa / approvazione monouso o protetta da policy deterministica/checkpoint.
- **Stato**: stato di implementazione e test.
- **Descrizione**: sommario del tool.

| Tool | Categoria / Politica Effetti | Stato | Descrizione |
|---|---|---|---|
| `ask_gemini` | Read-only / Consumo tracciato | Implementato & Testato via MCP | Interroga il modello Gemini con token usage, costo per job e budget vincolante. |
| `calendar_create_event` | **Elicitazione nativa MCP** (digest evento) | Implementato & Testato via MCP | Crea evento su Google Calendar previo consenso utente con digest SHA-256. |
| `calendar_delete_event` | **Elicitazione nativa MCP** (id, digest) | Implementato & Testato via MCP | Cancella evento su Google Calendar previo consenso esplicito. |
| `calendar_search` | Read-only (scope `calendar.readonly`) | Implementato & Testato via MCP | Cerca e legge eventi Google Calendar entro intervallo temporale. Contenuto non fidato. |
| `calendar_update_event` | **Elicitazione nativa MCP** (modifiche, digest) | Implementato & Testato via MCP | Aggiorna evento Google Calendar previo consenso esplicito. |
| `cancel_reminder` | Mutativo controllato | Implementato & Testato via MCP | Annulla reminder persistente schedulato prima della scadenza. |
| `coding_session_init` | Ispezione / Workspace | Implementato & Testato via MCP | Inizializza workspace coding e risolve progetti in `~/programmazione`. |
| `commit_project_changes` | **Mutativo vincolato** (verifica run richiesta) | Implementato & Testato via MCP | Commit atomico dei soli file rilevanti previa verifica superata (`verification_run_id`). |
| `create_checkpoint` | Salvataggio sicuro locale | Implementato & Testato via MCP | Crea snapshot di sicurezza di un file di progetto prima di modifiche. |
| `create_reminder` | Persistente locale | Implementato & Testato via MCP | Crea reminder persistente con timezone e scadenza assoluta UTC (SQLite). |
| `device_status` | Read-only | Implementato & Testato via MCP | Ispeziona stato host locale (WSL/Linux/Windows). Nessun check remoto fittizio. |
| `disk_usage` | Read-only | Implementato & Testato via MCP | Spazio disco del filesystem di lavoro dell'host configurato. |
| `draft_calendar_event` | **Bozza locale** (zero chiamate di rete) | Implementato & Testato via MCP | Genera bozza locale di evento calendario con validazione RFC3339 e DST. |
| `draft_email_reply` | **Bozza locale** (zero chiamate di rete) | Implementato & Testato via MCP | Genera bozza locale di risposta email con threading e digest SHA-256. |
| `email_read` | Read-only (IMAP) | Implementato & Testato via MCP | Legge email per UID tramite IMAP SSL. Dati trattati come non fidati. |
| `email_search` | Read-only (IMAP) | Implementato & Testato via MCP | Cerca email per cartella, mittente o testo. Mai mutativo. |
| `email_send` | **Elicitazione obbligatoria** (destinatari, digest) | Implementato & Testato via MCP | Invio email via SMTP Yahoo/custom strettamente vincolato al consenso del proprietario. |
| `execute_gui_action` | **Policy protetta** (whitelist + desktop interattivo) | Implementato & Testato via MCP | Esegue azione GUI non elevata con lock esclusivo e screenshot prima/dopo. |
| `fetch_page` | Read-only (SSRF safe) | Implementato & Testato via MCP | Estrae testo e metadati da URL web. Dati non fidati. |
| `forget_memory` | Mutativo controllato con backup | Implementato & Testato via MCP | Rimuove o corregge memoria inferita con backup dichiarativo e preservazione note. |
| `get_job_budget_usage` | Read-only | Implementato & Testato via MCP | Consulta utilizzo token e costi accumulati per uno specifico job. |
| `get_profile` | Read-only | Implementato & Testato via MCP | Legge preferenze esplicite e profilo memoria dal vault. |
| `get_routing_report` | Read-only | Implementato & Testato via MCP | Riepilogo telemetria routing, latenze p50/p95 ed escalation. |
| `git_push` | **Elicitazione obbligatoria** (remote, branch, commit) | Implementato & Testato via MCP | Push git verificato dopo consenso esplicito dell'utente su Telegram. |
| `gui_status` | Read-only | Implementato & Testato via MCP | Verifica interattività desktop Windows e finestra attiva. |
| `job_cancel` | Mutativo controllato | Implementato & Testato via MCP | Interrompe job in esecuzione, segnalando processo e passi non annullati. |
| `job_status` | Read-only | Implementato & Testato via MCP | Stato persistente, tempo trascorso e progresso di un job. |
| `list_reminders` | Read-only | Implementato & Testato via MCP | Elenca reminder persistenti per stato (scheduled, completed, cancelled, missed). |
| `pi_task` | Confinamento sandbox & lock | Implementato & Testato via MCP | Esegue task di programmazione autonomo tramite Pi CLI confinato in `~/programmazione`. |
| `propose_memory` | Mutativo soft (non sovrascrive esplicite) | Implementato & Testato via MCP | Propone memoria inferita candidata con evidenza sorgente. |
| `read_note` | Read-only | Implementato & Testato via MCP | Legge nota Markdown UTF-8 dal vault entro limiti dimensionali. Dati non fidati. |
| `read_project_file` | Read-only | Implementato & Testato via MCP | Legge file da progetti registrati con impaginazione e redazione segreti. |
| `read_web_page` | Read-only | Implementato & Testato via MCP | Scarica pagina web e analizza form HTML. Dati non fidati. |
| `reconcile_calendar_event` | Read-only | Implementato & Testato via MCP | Riconcilia stato creazione evento calendario via digest senza retry ciechi. |
| `reconcile_email` | Read-only | Implementato & Testato via MCP | Riconcilia esito invio email per Message-ID nell'audit log senza retry ciechi. |
| `record_routing_correction` | Mutativo locale telemetria | Implementato & Testato via MCP | Registra correzione utente su routing per dataset di calibrazione. |
| `restore_checkpoint` | **Ripristino protetto** da Job ID | Implementato & Testato via MCP | Ripristina file modificato al checkpoint iniziale fornendo diff. |
| `route_request` | Read-only (diagnostica) | Implementato & Testato via MCP | Diagnostica di instradamento richieste verso modelli o fallback. |
| `run_command` | **Policy deterministica** | Implementato & Testato via MCP | Esegue comandi terminale: automatico se READONLY, checkpoint se WRITE, approvazione se PRIVILEGED/DESTRUCTIVE. |
| `run_project_workflow` | **Esecuzione vincolata** | Implementato & Testato via MCP | Esegue workflow pre-registrato validando hash di script e integrità git hooks. |
| `search_flexible_flights` | Read-only / Cache locale | Implementato & Testato via MCP | Cerca voli su intervalli di date flessibili con budget chiamate e cache SQLite. |
| `search_flights` | Read-only (provider travel) | Implementato & Testato via MCP | Ricerca offerte voli normalizzate tramite provider Duffel o SerpApi. |
| `search_notes` | Read-only | Implementato & Testato via MCP | Ricerca per sottostringa su note visibili nel vault Obsidian. |
| `simulate_with_approval` | **Elicitazione / Simulazione monouso** | Implementato & Testato via MCP | Chiede conferma all'utente su Telegram e registra effetto fittizio isolato. |
| `submit_web_form` | **Elicitazione obbligatoria** (URL, campi, digest) | Implementato & Testato via MCP | Invia form web esterno solo previo consenso utente con campi verificati. |
| `transcribe_voice_message` | Read-only / STT | Implementato & Testato via MCP | Gestisce trascrizione vocale Telegram (Groq whisper-large-v3 con fallback locale). |
| `update_preference` | **Mutativo con controllo concorrenza** | Implementato & Testato via MCP | Aggiorna preferenza esplicita tipizzata in `preferences.yaml` con guardrail. |
| `web_search` | Read-only / Cache locale | Implementato & Testato via MCP | Ricerca web con fallback a catena (Exa -> Tavily), cache SQLite e rate limiting. |
| `write_note` | **Mutativo con controllo concorrenza** | Implementato & Testato via MCP | Scrive/aggiorna nota Markdown nel vault con verifica ottimistica su SHA-256. |
| `write_project_file` | **Scrittura protetta da Checkpoint** | Implementato & Testato via MCP | Scrive file di progetto sotto checkpoint attivo, fallendo su conflitto di hash. |
