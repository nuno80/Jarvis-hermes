# Jarvis-hermes

Assistente personale MCP-first basato su Hermes, controllabile da Telegram, con memoria Obsidian e strumenti per Windows/WSL, sviluppo e viaggi.

**Stato:** disponibili documentazione, issue, diagnostica e un server MCP con quattro tool in sola lettura più la conferma monouso di un effetto simulato. La causa della prova Telegram negativa è stata accertata: la stessa azione era stata confermata più volte, non aggirata; il conteggio degli effetti è ora per decisione. Il tool resta limitato a effetti simulati e non va collegato a effetti esterni reali.

Bot esistente da riutilizzare: **@nuno_agent_bot**. La presenza del bot è confermata dall'utente; connessione al gateway e host devono ancora essere verificati.

## Documenti

- [Specifica V1](docs/specs/jarvis-v1.md)
- [Server MCP e collegamento locale](docs/mcp-readonly.md)
- [Confine delle conferme simulate](docs/adr/0004-simulated-approval-gate.md)
- [Roadmap e 30 issue](docs/roadmap.md)
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

## Incremento MCP

`uv run jarvis serve` espone quattro tool in sola lettura (stato macchina, spazio disco, ricerca e lettura note) e `simulate_with_approval`, che registra soltanto effetti simulati dopo una conferma monouso. Configurare il percorso del vault nel processo che lo avvia; i test usano solo directory temporanee. Nessuna nota del vault personale è stata letta o modificata. Vedere [contratti e limiti](docs/mcp-readonly.md).
