# Local configuration

The doctor consumes optional JARVIS_VAULT_PATH. The MCP server additionally uses JARVIS_DEVICE_ID (default local).
The doctor checks directory availability without reading notes; the MCP server can read visible Markdown notes under that configured root; it does not load .env automatically.
Keep the actual absolute path in local environment configuration, outside Git.

Future schema validation, provider settings and policy configuration are tracked in issues.
No sample Hermes YAML is presented as executable until its installed version is known.
Existing bot: @nuno_agent_bot. Reuse the existing local Hermes configuration.

## Gestione lavori lunghi (`job_status`, `job_cancel`)

I lavori di lunga durata e le relative transizioni di stato sono tracciati in modo persistente nel database SQLite locale (`$XDG_STATE_HOME/jarvis-hermes/jobs.sqlite3`):
- `job_status(job_id)`: consulta stato corrente, elapsed time in secondi, progresso e avviso esplicito se la durata supera 30 secondi (`progress_notice`).
- `job_cancel(job_id)`: interrompe i passi successivi, distingue il processo interrotto, segnala l'ultimo passo completato e il conteggio di effetti non annullati (`unreverted_effects_count`).
- **Recupero da riavvio del nodo**: al riavvio, i job non terminati vengono marcati esplicitamente come `outcome_unknown` con warning, impedendo riesecuzioni cieche di mutazioni.


Per usare `simulate_with_approval`, imposta `JARVIS_APPROVER_ID` nel blocco `env` del server MCP `jarvis` in `~/.hermes/config.yaml`, oppure passa la variabile nel comando stdio configurato. Deve essere lo stesso unico ID numerico già configurato in `TELEGRAM_ALLOWED_USERS`; non aggiungere `actor_id` agli argomenti dello strumento. Hermes v0.20.0 inoltra la richiesta form-mode MCP elicitation alla superficie di approvazione della sessione Telegram attiva.

Se l'ID manca o non è valido, la richiesta fallisce in modo chiuso. Il registro SQLite è in `$XDG_STATE_HOME/jarvis-hermes/approvals.sqlite3` oppure `~/.local/state/jarvis-hermes/approvals.sqlite3`, fuori dal repository.

Questo server non espone resources né prompts. FastMCP 1.30 pubblicizza comunque quelle capacità, quindi Hermes aggiunge quattro wrapper (`list_resources`, `read_resource`, `list_prompts`, `get_prompt`) ai cinque tool reali: nove voci in `tools list`. Disattivarli con `tools.resources: false` e `tools.prompts: false` sotto `mcp_servers.jarvis`, come documentato nel riferimento di configurazione MCP di Hermes. Dopo la modifica serve un `/reload_mcp`.

L'esito del tool riporta `decision` (`accept`, `decline`, `unavailable`) e `effects_recorded` per quella singola decisione: 1 se approvata, 0 se annullata. Il valore non è il totale del registro. **Ogni chiamata chiede una nuova conferma**: il cancello impedisce il riuso dello stesso consenso, non la ripetizione della domanda. Se l'agente ripete la richiesta, riceverai un secondo prompt e una seconda approvazione produrrà un secondo effetto. Il tool resta limitato a effetti simulati.

Dopo aver modificato config.yaml, esegui `/reload_mcp` in Hermes e avvia una nuova sessione Telegram. Verifica con `hermes mcp test jarvis` che il catalogo contenga i quattro tool in sola lettura più `simulate_with_approval`, e ricorda che quest'ultimo non mostra `readOnlyHint`.
