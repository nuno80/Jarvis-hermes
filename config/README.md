# Local configuration

The doctor consumes optional JARVIS_VAULT_PATH. The MCP server additionally uses JARVIS_DEVICE_ID (default local).
The doctor checks directory availability without reading notes; the MCP server can read visible Markdown notes under that configured root; it does not load .env automatically.
Keep the actual absolute path in local environment configuration, outside Git.

Future schema validation, provider settings and policy configuration are tracked in issues.
No sample Hermes YAML is presented as executable until its installed version is known.
Existing bot: @nuno_agent_bot. Reuse the existing local Hermes configuration.

## Disabilitazione della vecchia conferma MCP

La prova Telegram ha restituito `executed` quando l'utente intendeva rifiutare, e il registro mostrava tre effetti simulati. Non usare `simulate_with_approval` dalla chat. La versione corrente del server MCP espone soltanto quattro tool in sola lettura.

Se il gateway usa ancora il vecchio processo, aggiungi temporaneamente questo filtro sotto `mcp_servers.jarvis` in `~/.hermes/config.yaml`:

```yaml
    tools:
      exclude:
        - simulate_with_approval
```

Aggiorna il repository, esegui `/reload_mcp` e verifica con `hermes mcp test jarvis` che il tool non appaia. `JARVIS_APPROVER_ID` può essere rimosso dal blocco `env` dopo il riavvio: non serve ai quattro tool in sola lettura. Il database delle prove rimane locale sotto `$XDG_STATE_HOME/jarvis-hermes/approvals.sqlite3` (oppure `~/.local/state/jarvis-hermes/approvals.sqlite3`).
