# Local configuration

The doctor consumes optional JARVIS_VAULT_PATH. The MCP server additionally uses JARVIS_DEVICE_ID (default local).
The doctor checks directory availability without reading notes; the MCP server can read visible Markdown notes under that configured root; it does not load .env automatically.
Keep the actual absolute path in local environment configuration, outside Git.

Future schema validation, provider settings and policy configuration are tracked in issues.
No sample Hermes YAML is presented as executable until its installed version is known.
Existing bot: @nuno_agent_bot. Reuse the existing local Hermes configuration.

## Soglie di routing per classe di azione (issue 40 / JARVIS-38)

`config/routing_thresholds.json` contiene le soglie di confidenza calibrate per classe di azione (`readonly`, `protected`, `reasoning`, `clarify`). Il router applicherà la soglia della classe target; quelle protette restano comunque vincolate a policy e consenso (D07, AT13). Ricalibrare con `uv run jarvis routing-calibrate` a ogni cambio di modello, prompt o domande (Appendice I2); il report usa il dataset versionato `tests/data/routing_it.jsonl` con split deterministico (seed 42).

## Gestione lavori lunghi (`job_status`, `job_cancel`)

I lavori di lunga durata e le relative transizioni di stato sono tracciati in modo persistente nel database SQLite locale (`$XDG_STATE_HOME/jarvis-hermes/jobs.sqlite3`):
- `job_status(job_id)`: consulta stato corrente, elapsed time in secondi, progresso e avviso esplicito se la durata supera 30 secondi (`progress_notice`).
- `job_cancel(job_id)`: interrompe i passi successivi, distingue il processo interrotto, segnala l'ultimo passo completato e il conteggio di effetti non annullati (`unreverted_effects_count`).
- **Recupero da riavvio del nodo**: al riavvio, i job non terminati vengono marcati esplicitamente come `outcome_unknown` con warning, impedendo riesecuzioni cieche di mutazioni.


Per usare `simulate_with_approval`, imposta `JARVIS_APPROVER_ID` nel blocco `env` del server MCP `jarvis` in `~/.hermes/config.yaml`, oppure passa la variabile nel comando stdio configurato. Deve essere lo stesso unico ID numerico già configurato in `TELEGRAM_ALLOWED_USERS`; non aggiungere `actor_id` agli argomenti dello strumento. Hermes v0.20.0 inoltra la richiesta form-mode MCP elicitation alla superficie di approvazione della sessione Telegram attiva.

Se l'ID manca o non è valido, la richiesta fallisce in modo chiuso. Il registro SQLite è in `$XDG_STATE_HOME/jarvis-hermes/approvals.sqlite3` oppure `~/.local/state/jarvis-hermes/approvals.sqlite3`, fuori dal repository.

Questo server non espone resources né prompts. FastMCP 1.30 pubblicizza comunque quelle capacità, quindi Hermes aggiunge quattro wrapper (`list_resources`, `read_resource`, `list_prompts`, `get_prompt`) ai cinque tool reali: nove voci in `tools list`. Disattivarli con `tools.resources: false` e `tools.prompts: false` sotto `mcp_servers.jarvis`, come documentato nel riferimento di configurazione MCP di Hermes. Dopo la modifica serve un `/reload_mcp`.

L'esito del tool riporta `decision` (`accept`, `decline`, `unavailable`) e `effects_recorded` per quella singola decisione: 1 se approvata, 0 se annullata. Il valore non è il totale del registro. **Ogni chiamata chiede una nuova conferma**: il cancello impedisce il riuso dello stesso consenso, non la ripetizione della domanda. Se l'agente ripete la richiesta, riceverai un secondo prompt e una seconda approvazione produrrà un secondo effetto. Il tool resta limitato a effetti simulati.

## Progetto e lettura log/file

Per abilitare `read_project_file`, specifica la variabile d'ambiente `JARVIS_PROJECTS_CONFIG` indicando il percorso assoluto a un file di configurazione JSON (`projects.json`), ad esempio in `~/.hermes/config.yaml` o nell'ambiente locale.

Esempio di struttura `projects.json`:
```json
{
  "devices": {
    "local": {"environment": "wsl"},
    "windows-pc": {"environment": "windows"}
  },
  "projects": {
    "fantavega": {
      "name": "Fantavega App",
      "paths": {
        "local": "/home/user/projects/fantavega",
        "windows-pc": "C:/Users/User/Projects/fantavega"
      }
    }
  }
}
```

Lo strumento:
- Risolve `project_id` e `device_id` verificando traversal `..` e symlink non consentiti.
- Supporta percorsi con spazi.
- Pagina l'output grande (`offset`, `limit`) e fornisce metadati artifact se il file eccede 512 KiB.
- Esegue la redazione automatica dei pattern di segreti (token, api key, password).
- Distingue errori puntuali: `FILE_NOT_FOUND`, `PERMISSION_DENIED`, `WSL_UNAVAILABLE`, `DEVICE_OFFLINE`.

## Checkpoint e ripristino file di progetto

La modifica sicura di file di progetto avviene tramite tre strumenti MCP con supporto a snapshot/checkpoint isolati:
1. `create_checkpoint(job_id, project_id, relative_path, device_id)`:
   - Cattura lo stato iniziale del file (incluso file vuoto/nuovo o file esistente con modifiche non committate dell'utente).
   - Registra hash iniziale e copia snapshot in `$XDG_STATE_HOME/jarvis-hermes/checkpoints/`.
2. `write_project_file(checkpoint_id, project_id, relative_path, content, expected_initial_hash, device_id)`:
   - Verifica che l'hash corrente del file corrisponda a `expected_initial_hash`.
   - Se il file è stato modificato concurrentemente (ad es. modifica manuale dall'utente), fallisce con errore `CONFLICT` senza sovrascrivere.
   - Esegue la scrittura atomica del file.
3. `restore_checkpoint(checkpoint_id, expected_job_id)`:
   - Ripristina il file al suo stato originale salvato nel checkpoint.
   - Fornisce il diff unificato tra la versione modificata dal job e la versione ripristinata.
   - È limitato al solo file del checkpoint e al solo job associato (nessun reset globale del repository).

## Workflow di progetto e commit atomico (J06, D05, C3)

L'esecuzione di verifiche e la creazione di commit avvengono in modo controllato tramite due strumenti:
1. `run_project_workflow(project_id, workflow_name, device_id, timeout_seconds)`:
   - Esegue solo comandi pre-registrati per il progetto nella configurazione (whitelist di workflow, es. `test`, `build`).
   - Verifica l'integrità dei git hooks in `.git/hooks`: se un hook viene aggiunto o modificato rispetto agli hash ammessi, il workflow fallisce con `HOOK_MODIFIED`.
   - Verifica l'integrità di script specificati in `expected_hashes`: fallisce con `SCRIPT_MODIFIED` in caso di discrepanze.
   - Restituisce esito booleano `ok`, exit code e output (con redazione automatica dei segreti). Non maschera i fallimenti dei test come successi.
2. `commit_project_changes(project_id, files, commit_message, verification, device_id)`:
   - Richiede una verifica passata con successo (`verification.passed == True`).
   - Esegue lo staging e il commit mirato solo dei file indicati in `files`.
   - Preserva intatte le modifiche concorrenti o non correlate presenti nel working tree (non fa commit globale né reset).
   - Restituisce hash del commit, file inclusi e metadati della verifica svolta.

## Push protetto da autorizzazione esplicita (`git_push`, J06, D06, AT03, AT04)

L'invio sul remoto di modifiche (`git_push`) richiede consenso esplicito e vincolato:
1. `git_push(project_id, remote, branch, commit_hash, device_id)`:
   - Richiede approvazione Telegram tramite MCP elicitation (o token monouso tramite store interno).
   - Il consenso è strettamente vincolato a `target` (`git_push:<project_id>`) e parametri esatti: `remote`, `branch` e `commit_hash`.
   - Se uno qualsiasi dei parametri cambia, o se il consenso scade o viene rifiutato, l'operazione fallisce (`APPROVAL_DENIED`, `APPROVAL_EXPIRED`, `APPROVAL_DECLINED`) e il remoto non viene toccato.
   - Solo i remoti e branch configurati in `allowed_remotes` e `allowed_branches` sono consentiti.
   - Verifica lo stato del remoto prima/dopo l'operazione (`remote_verified`) per garantire coerenza anche a fronte di timeout di rete o errori parziali.
   - Nessun comando arbitrario o bypass shell è abilitato.

## Automazione GUI Windows mediata da policy (J07, D04, AT10)

L'interazione con l'interfaccia grafica Windows avviene tramite policy protetta:
- `gui_status()`: verifica se la sessione Windows è interattiva (desktop non bloccato) e legge lo stato della finestra attiva.
- `execute_gui_action(app_name, action)`:
  - Consente l'avvio e l'ispezione solo di applicazioni grafiche innocue e non elevate (es. `notepad`, `calc`).
  - Blocca categoricamente shell, interpreti o strumenti di sistema (`cmd.exe`, `powershell.exe`, `regedit.exe`, ecc.) prevenendo bypass delle policy o esecuzioni come amministratore permanente.
  - Verifica la disponibilità della sessione grafica interattiva (`check_desktop_interactive`): se il desktop è bloccato, disconnesso o non disponibile (criterio AT10), dichiara esplicitamente `DESKTOP_UNAVAILABLE` senza simulare successi fittizi.
  - Acquisisce un lock esclusivo (`gui_job.lock`), consentendo un solo job GUI contemporaneo (`CONCURRENT_GUI_JOB`).
  - Cattura screenshot prima e dopo l'operazione (`before_*.png`, `after_*.png`), salvandoli localmente in `$XDG_STATE_HOME/jarvis-hermes/screenshots/` con permessi ristretti (0700).

## Comandi amministrativi e terminale con policy sugli effetti (`run_command`, J04–J06 estesi, D04, D07, C3)

L'esecuzione di comandi amministrativi e da terminale non è vincolata a una whitelist rigida, ma valuta deterministamente la tipologia di effetti:
- `run_command(command, timeout_seconds, cwd, job_id)`:
  - **Letture ed esplorazione (`READONLY`)**: comandi non mutativi (es. `ls`, `df`, `uptime`, `git status`, `systemctl status`, `docker ps`) vengono eseguiti automaticamente senza consenso manuale.
  - **Modifiche a file con recupero (`WRITE_RECOVERABLE`)**: scritture o redirection verso file (es. `echo "val" > file.txt`) creano automaticamente uno snapshot checkpoint con `CheckpointManager` prima dell'esecuzione per consentire il ripristino.
  - **Privilegi, manutenzione e mutazione servizi (`PRIVILEGED_MAINTENANCE`)**: comandi di sistema o riavvio servizi (es. `systemctl restart service`, `docker restart`, modifiche di pacchetti o permessi) richiedono approvazione del proprietario (MCP elicitation Telegram o token approvato).
  - **Distruzioni ed effetti esterni (`DESTRUCTIVE`, `EXTERNAL_EFFECT`)**: comandi distruttivi (`rm`, `dropdb`, ecc.) o che contattano l'esterno (`curl`, `ssh`) richiedono conferma esplicita vincolata al digest esatto.
  - **Composizioni non analizzabili (`UNPARSEABLE_COMPLEX`)**: catene shell non banali con pipe, subshell, `eval`, o chaining presentano il comando esatto per conferma prima di qualunque esecuzione.
  - **Nessuna concessione da parte di classificazioni LLM**: la classificazione è deterministica lato server; prompt injection o asserzioni del modello non possono concedere permessi né scavalcare la policy.
  - **Target protetti immutabili**: file di policy, file di configurazione (`config.yaml`, `projects.json`), secret store (`.env`, credentials, chiavi SSH, `shadow`, database di stato) non sono mai accessibili o sovrascrivibili tramite `run_command` (`PROTECTED_TARGET_DENIED`).

## Interazione pagina web e consenso invii esterni (`read_web_page`, `submit_web_form`, J07, C3, AT05)

L'interazione con pagine web e l'invio di moduli esterni avvengono sotto controllo rigoroso del consenso:
- `read_web_page(url, timeout_seconds)`:
  - Scarica la pagina ed estrae titolo, testo e moduli HTML (`<form>`, input, campi textarea).
  - **Dati non fidati**: il contenuto della pagina web è trattato puramente come dato grezzo e mai come istruzioni eseguibili. Eventuali tentativi di prompt injection nella pagina non possono concedere privilegi o bypassare i gate di sicurezza.
- `submit_web_form(action_url, method, fields, ctx, page_url, timeout_seconds)`:
  - Richiede approvazione Telegram tramite MCP elicitation (o token monouso dallo store).
  - Mostra il riepilogo approvato con l'URL di destinazione esatto, metodo HTTP, bozza dei campi compilati e digest SHA-256.
  - **Redazione credenziali**: password, token, chiavi o segreti inseriti nei campi vengono mascherati (`[REDACTED]`) nei log e nei riepiloghi utente.
  - **Vincolo atomico**: se l'URL o i parametri inviati differiscono anche di un solo carattere rispetto al riepilogo approvato, l'invio è respinto (`APPROVAL_DENIED`).
  - **Esito incerto senza retry ciechi (AT04 / C2)**: se durante l'invio la connessione cade o si verifica un timeout, l'operazione restituisce `OUTCOME_UNKNOWN` e non viene ritentata ciecamente per evitare doppi invii indesiderati.

## Ricerca web e lettura pagine (`web_search`, `fetch_page`, J12, AT05, ADR 0009)

- `web_search(query, max_results, timeout_seconds)`:
  - Catena di provider configurabile `JARVIS_SEARCH_PROVIDERS` (default `exa,tavily`, opzionali `brave`, `searxng`). Fallback solo per `SEARCH_QUOTA_EXCEEDED`, `NOT_CONFIGURED`, 402/429, timeout o 5xx.
  - Richiede almeno una chiave in env (`EXA_API_KEY`, `TAVILY_API_KEY`, `BRAVE_API_KEY`, `SEARXNG_BASE_URL`); le chiavi viaggiano solo in header HTTP, mai in URL, e sono redatte dai log.
  - Restituisce risultati normalizzati (`rank`, `title`, `url`, `snippet` ≤ 500 caratteri, `source`, `provider`, `retrieved_at`, `published_at`/`score` oppure `unknown`) con `providers_tried`, `retrieved_at` (UTC ISO 8601) e `cache_hit`.
  - **Dati non fidati**: titoli, snippet e testi sono dati grezzi e mai istruzioni. Se nessun provider è utilizzabile, fallisce chiuso (`NOT_CONFIGURED` / `ALL_PROVIDERS_FAILED`) con l'elenco dei provider tentati e delle variabili mancanti; mai risultati simulati.
  - Cache locale SQLite con TTL (`JARVIS_SEARCH_CACHE_TTL_SECONDS`, default 15 minuti) e contatori giorno/mese (`JARVIS_SEARCH_DAILY_LIMIT`, `JARVIS_SEARCH_MONTHLY_LIMIT`). Un cache hit non consuma quota.
- `fetch_page(url, timeout_seconds)`:
  - Alias sicuro in sola lettura di `read_web_page`: stesso controllo anti-SSRF, solo testo estratto (senza moduli), URL, titolo e `retrieved_at`.

## Agenda Google Calendar (`calendar_search`, `draft_calendar_event`, J16, #23, ADR 0011)

- `calendar_search(calendar_id, q, time_min, time_max, time_zone, max_results)`:
  - Lettura sola via Calendar API v3 con scope `calendar.readonly`; GET non crea eventi. Credenziali solo da env: `GOOGLE_CALENDAR_ACCESS_TOKEN` oppure tripletta `GOOGLE_CALENDAR_CLIENT_ID` / `GOOGLE_CALENDAR_CLIENT_SECRET` / `GOOGLE_CALENDAR_REFRESH_TOKEN` (rinnovo automatico). Timezone default `JARVIS_CALENDAR_TIMEZONE` (`Europe/Rome`).
  - Restituisce eventi normalizzati (`event_id`, `summary`, `start`/`end` con offset o date all-day, `location`, `attendees`, `organizer`, `status`, `html_link`) con timezone del calendario, `observed_at` e campi ignoti espliciti (`unknown_fields`).
  - **Dati non fidati** (AT05): contenuti marcati `untrusted_content`, mai istruzioni. Senza credenziali fallisce chiuso (`PROVIDER_NOT_CONFIGURED`) senza toccare la rete; accesso revocato → `PROVIDER_AUTH_ERROR`; calendario assente → `CALENDAR_NOT_FOUND`. Mai eventi simulati.
- `draft_calendar_event(summary, start, end, calendar_id, time_zone, location, description, attendees)`:
  - Bozza puramente locale, nessuna rete e nessun evento creato (`created_event: false`); valida orari RFC3339/date, timezone IANA, invitati email e segnala transizioni DST (`dst_transition`). La creazione con conferma è la #24.

## Email e bozze di risposta (`email_search`, `email_read`, `draft_email_reply`, J16, #25, ADR 0012)

- `email_search(folder, q, from_sender, subject, max_results)`:
  - Ricerca e lettura da account IMAP (Yahoo Mail o generico IMAP over SSL, porta 993).
  - Credenziali lette solo da env: `YAHOO_EMAIL` (o `JARVIS_EMAIL_USER`) e `YAHOO_APP_PASSWORD` (o `JARVIS_EMAIL_PASSWORD`). Host configurabile `JARVIS_EMAIL_IMAP_HOST` (default `imap.mail.yahoo.com`).
  - Restituisce email normalizzate (`uid`, `message_id`, `subject`, `from`, `to`, `cc`, `date`, `in_reply_to`, `references`, `body_text`).
  - **Dati non fidati** (AT05): messaggi marcati `untrusted_content`, non possono impartire istruzioni operative fidate.
  - Fail-closed: senza credenziali fallisce con `PROVIDER_NOT_CONFIGURED` senza dati simulati; autenticazione fallita → `PROVIDER_AUTH_ERROR`.
- `email_read(uid, folder)`:
  - Lettura del singolo messaggio per UID con estrazione testo/plaintext o fallback HTML.
- `draft_email_reply(original_message, reply_body, reply_all, bcc, attachments)`:
  - Bozza di risposta locale: nessun invio di rete (`sent: false`, `status: 'draft'`).
  - Threading corretto (`Re: <subject>`, `In-Reply-To`, `References`, citazione automatica del testo originale).
  - Calcolo del `digest` SHA-256 immutabile della bozza (copre destinatari, cc, bcc, oggetto, corpo e allegati), pronto per il gate di approvazione della Issue #26.

## Invio email con consenso monouso ed esito verificato (`email_send`, `reconcile_email`, J16, #26, ADR 0013)

- `email_send(to, subject, body, cc, bcc, in_reply_to, references, attachments, message_id)`:
  - Invio protetto via SMTP (Yahoo Mail su porta 465 SSL o 587 STARTTLS) mediato rigorosamente da elicitazione e consenso monouso (`ApprovalStore`).
  - Il proprietario approva destinatario, cc/bcc, oggetto, corpo e allegati tramite il prompt con digest crittografico SHA-256.
  - Fail-closed: se l'approvazione scade (`APPROVAL_EXPIRED`), viene rifiutata (`APPROVAL_DECLINED`) o i parametri divergono (`APPROVAL_DENIED`), nessun messaggio viene inviato.
  - Message-ID deterministico generato e registrato in SQLite `email_audit.sqlite3`.
  - Gestione callback duplicate / de-duplicazione: se il `message_id` è già stato inviato, restituisce `ALREADY_SENT` riconciliato senza effettuare un secondo invio di rete.
  - In caso di timeout o disconnessione durante la trasmissione SMTP (AT04), non effettua retry cieco e registra l'esito come `OUTCOME_UNKNOWN`.
  - Assenza di segreti in audit: le credenziali applicative e le password non vengono mai salvate nel registro di audit.
- `reconcile_email(message_id)`:
  - Consulta l'audit log per verificare lo stato di recapito di un `message_id` senza rischiare doppi invii esterni.

## Trascrizione vocale e provider STT (Issue #45, ADR 0010)

- **Provider primario cloud**: Groq STT (`whisper-large-v3` / `whisper-large-v3-turbo`) abilitato impostando `GROQ_API_KEY` in `.env` o `~/.hermes/.env` e `stt.provider: groq` in `~/.hermes/config.yaml`.
  - Consente trascrizione ultra-rapida (<0.5s per clip) ad altissima accuratezza con modello SOTA a costo zero (free tier).
- **Fallback automatico su faster-whisper locale**:
  - Se `GROQ_API_KEY` non è configurata o se la quota gratuita su Groq si esaurisce (HTTP 429), il gateway Hermes e la pipeline Jarvis commutano automaticamente sulla trascrizione locale (`local` faster-whisper, modello `small`, lingua `it`).
- **Zero-secret & Sicurezza**:
  - `GROQ_API_KEY` non viene mai esposta nei log di audit, nella telemetria o nei metadati dei job (D07, ADR 0010).
  - Trascrizioni trattate rigorosamente come dati non fidati (`transcript_source: voice_stt_untrusted`).







