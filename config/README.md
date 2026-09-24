# Local configuration

The doctor consumes optional JARVIS_VAULT_PATH. The MCP server additionally uses JARVIS_DEVICE_ID (default local).
The doctor checks directory availability without reading notes; the MCP server can read visible Markdown notes under that configured root; it does not load .env automatically.
Keep the actual absolute path in local environment configuration, outside Git.

Future schema validation, provider settings and policy configuration are tracked in issues.
No sample Hermes YAML is presented as executable until its installed version is known.
Existing bot: @nuno_agent_bot. Reuse the existing local Hermes configuration.

## Conferma simulata via Telegram/Hermes

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


