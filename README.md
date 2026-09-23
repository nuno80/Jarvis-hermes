# Jarvis-hermes

Assistente personale MCP-first basato su Hermes, controllabile da Telegram, con memoria Obsidian e strumenti per Windows/WSL, sviluppo e viaggi.

**Stato:** inizializzazione del progetto. Disponibili documentazione, issue e una CLI di diagnostica locale con test. L'integrazione Hermes/MCP/Telegram non è ancora implementata.

Bot esistente da riutilizzare: **@nuno_agent_bot**. La presenza del bot è confermata dall'utente; connessione al gateway e host devono ancora essere verificati.

## Documenti

- [Specifica V1](docs/specs/jarvis-v1.md)
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

## Organizzazione

Questo repository contiene software e documentazione tecnica. Il vault privato `nuno80/jarvis-vault` rimane separato e sarà collegato tramite configurazione locale. Non caricare token, note personali, database runtime o checkpoint nel repository software.

## Metodo di sviluppo

Task definiti applicando `to-tickets` di Matt Pocock. La fonte dello stato è GitHub Issues; una issue è eseguibile quando tutti i blocker sono completati. Documentazione di setup in docs/agents/.
Le skill si installano nell'agente locale secondo il catalogo mattpocock/skills; qui non sono state vendorizzate.

Nessuna issue host-dependent va chiusa senza prova sul dispositivo reale. Le prove cloud della CLI non validano GUI, GPU, gateway esistente o accesso al vault dell'utente.
