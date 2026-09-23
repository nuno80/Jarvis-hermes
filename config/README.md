# Local configuration

The doctor consumes optional JARVIS_VAULT_PATH. The MCP server additionally uses JARVIS_DEVICE_ID (default local).
The doctor checks directory availability without reading notes; the MCP server can read visible Markdown notes under that configured root; it does not load .env automatically.
Keep the actual absolute path in local environment configuration, outside Git.

Future schema validation, provider settings and policy configuration are tracked in issues.
No sample Hermes YAML is presented as executable until its installed version is known.
Existing bot: @nuno_agent_bot. Reuse the existing local Hermes configuration.

## Conferma simulata via Telegram/Hermes

Per usare `simulate_with_approval`, imposta `JARVIS_APPROVER_ID` nel blocco `env` del server MCP `jarvis` in `~/.hermes/config.yaml`. Deve essere lo stesso unico ID numerico già configurato in `TELEGRAM_ALLOWED_USERS`; non aggiungere `actor_id` agli argomenti dello strumento. Hermes v0.20.0 inoltra la richiesta form-mode MCP elicitation alla superficie di approvazione della sessione Telegram attiva. L'esito affermativo autorizza solo la simulazione locale descritta nel prompt.

Se l'ID manca o non è valido, la richiesta fallisce in modo chiuso. Il registro SQLite è in `$XDG_STATE_HOME/jarvis-hermes/approvals.sqlite3` oppure `~/.local/state/jarvis-hermes/approvals.sqlite3`, fuori dal repository.

Dopo aver modificato config.yaml, esegui `/reload_mcp` in Hermes e avvia una nuova sessione Telegram. Prima dell'uso verifica che nel blocco `env` sia presente solo il tuo ID Telegram e che `GATEWAY_ALLOW_ALL_USERS` non sia attivo. Test reale Telegram (accetta/annulla) ancora da eseguire sul PC.
