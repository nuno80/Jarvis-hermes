# Verifica inizializzazione e test di regressione

Stato aggiornato ad ottobre 2026. Ambiente: runner Linux / Debian WSL, Python 3.12.

- `uv lock`: completato; lockfile sincronizzato.
- `uv sync --locked`: installazione dipendenze completata.
- `uv run python -m unittest discover -s tests -v`: **341 test** registrati, **340 superati**, 1 skipped (test opzionale con modello Ollama live in locale).
- `uv run jarvis doctor --json`: output valido con disco fisico dell'host, rilevamento WSL/Linux e stato delle integrazioni.

La CI è configurata su Linux e Windows tramite GitHub Actions (`.github/workflows/ci.yml`).

## Verifica Capacità & Tool

Tutti i 52 tool MCP registrati sono coperti da test unitari ed end-to-end con client MCP reale e trasporto stdio isolato (`tests/test_mcp.py` e suite dedicate per calendar, email, travel, routing, checkpoint, gui, ecc.).

Per l'inventario aggiornato di tutti i tool registrati nel server FastMCP e il dettaglio delle relative policy di sicurezza e conferma:
```bash
uv run ./scripts/inventory_tools.py
```
Vedere anche [`docs/tools-inventory.md`](tools-inventory.md).
