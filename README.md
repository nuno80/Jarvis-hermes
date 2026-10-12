# Jarvis-hermes

Assistente personale MCP-first basato su Hermes, controllabile da Telegram, con memoria Obsidian e strumenti per Windows/WSL, sviluppo e viaggi.

**Stato:** server MCP integrato con **50 tool registrati** (ispezione sistema, gestione file/checkpoint, workflow e commit atomici, push autorizzato, automazione GUI Windows, navigazione web protetta, vault/preferenze Obsidian, Gemini/routing LLM, vocale Telegram con Groq STT, Google Calendar, Yahoo IMAP/SMTP, ricerca voli e reminder persistenti). Suite automatizzata con oltre **340 test** unitari e di integrazione MCP. I tool con effetti collaterali reali sono rigorosamente vincolati a gate di approvazione esplicita (MCP elicitation Telegram) e a policy di confinamento deterministiche lato server.

Bot Telegram collegato: **@nuno_agent_bot**.

## Documenti

- [Specifica V1](docs/specs/jarvis-v1.md)
- [Inventario Completo Tool MCP (50 tool)](docs/tools-inventory.md)
- [Server MCP: contratti iniziali e confini](docs/mcp-readonly.md)
- [Guida alla configurazione](config/README.md)
- [Roadmap e avanzamento issue](docs/roadmap.md)
- [Cosa verificare al ritorno al PC](docs/return-to-pc.md)
- [Decisioni architetturali](docs/adr/)
- [Glossario](CONTEXT.md)
- [Workflow per agenti](AGENTS.md)

## Avvio della base locale

Richiede Python 3.12 e uv. Da Debian/WSL o Windows:

```bash
uv sync --locked
uv run python -m unittest discover -s tests -v
uv run jarvis doctor --json
```

Il doctor legge solo dati del proprio host e verifica la presenza opzionale del vault. Non contatta Telegram, non legge note e non dimostra il funzionamento del tuo PC da remoto.

`uv run jarvis approval-demo` prova localmente una conferma monouso in un database temporaneo. Registra un effetto soltanto simulato, senza Telegram o operazioni sul PC. Vedi [configurazione locale](config/README.md) per collegare `simulate_with_approval` al gateway esistente.

## Organizzazione

Questo repository contiene software e documentazione tecnica. Il vault privato `nuno80/jarvis-vault` rimane separato e sarà collegato tramite configurazione locale. Non caricare token, note personali, database runtime o checkpoint nel repository software.

## Metodo di sviluppo

Task definiti applicando `to-tickets` di Matt Pocock. La fonte dello stato è GitHub Issues; una issue è eseguibile quando tutti i blocker sono completati. Documentazione di setup in docs/agents/.
Le skill si installano nell'agente locale secondo il catalogo mattpocock/skills; qui non sono state vendorizzate.

Nessuna issue host-dependent va chiusa senza prova sul dispositivo reale. Le prove cloud della CLI non validano GUI, GPU, gateway esistente o accesso al vault dell'utente.

## Incremento e Architettura MCP

`uv run jarvis serve` avvia il server FastMCP esponendo 50 strumenti tipizzati per Hermes e il client MCP.
L'architettura segue il principio del *fail-closed* e della separazione dei poteri:
- **Operazioni di sola lettura / bozza**: operano senza mutazioni, trattando tutti i contenuti esterni (note, pagine web, email, output di ricerca) come dati grezzi non fidati (`untrusted_content`).
- **Operazioni mutative protette**: modifiche a file di progetto (`write_project_file`, `restore_checkpoint`) sono vincolate a snapshot creati con `create_checkpoint`.
- **Operazioni ad alto impatto / esterne**: comandi distruttivi (`run_command`), invio email (`email_send`), modifiche calendario (`calendar_create_event`, `calendar_update_event`, `calendar_delete_event`), compilazione form (`submit_web_form`) e push Git (`git_push`) richiedono immancabilmente approvazione monouso tramite MCP elicitation inoltrata su Telegram all'utente autorizzato (`JARVIS_APPROVER_ID`), vincolata al digest esatto dei parametri.

Vedere [l'inventario completo dei tool](docs/tools-inventory.md) e i [contratti e limiti](docs/mcp-readonly.md).
