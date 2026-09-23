# Quando torni al PC

1. Clona il repository software in Debian/WSL, accanto agli altri progetti, mantenendo il vault nella sua posizione attuale.
2. Prima di reinstallare qualcosa, identifica dove gira già Hermes (Windows, WSL o altro host), il profilo utilizzato e come parte il gateway. Conserva un backup della configurazione senza caricarlo su GitHub.
3. Riusa @nuno_agent_bot. Verifica che un solo gateway consumi gli aggiornamenti del bot e che il tuo ID Telegram numerico sia autorizzato. Token mai in issue, log o commit.
4. Con Python 3.12 e uv disponibili esegui `uv sync --locked`, `uv run python -m unittest discover -s tests -v`, `uv run jarvis doctor --json`.
5. Il doctor descrive solo l'ambiente in cui lo esegui. Se eseguito in WSL non dimostra che la GUI Windows sia raggiungibile.
6. Imposta JARVIS_VAULT_PATH al percorso assoluto locale del vault per verificare la presenza della directory; questa versione non legge le note.
7. Segui l'issue JARVIS-02 per Telegram → Hermes → MCP e le successive per conferme, Windows/WSL e GUI.

Il codice attuale non collega Telegram, non usa API cloud, non modifica il PC e non esegue comandi remoti. È la base verificabile del progetto, non l'assistente già operativo.
