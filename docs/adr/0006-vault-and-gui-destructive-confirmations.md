# ADR 0006: Conferme server-side per tool distruttivi su vault e GUI

Stato: Accettata.

## Contesto

Secondo la specifica JARVIS V1 (D07, D14, sezione 6, C1) le annotazioni MCP dei tool (`destructive`, `readonly`) sono puramente informative e non costituiscono né garantiscono un confine di sicurezza. Un agent harness o un client non conforme (o compromesso) potrebbe invocare un tool annotato come distruttivo senza sollecitare l'approvazione dell'utente (`ctx.elicit`).

Occorre definire chiaramente quali operazioni su **vault** e **GUI** sono distruttive o ad alto impatto, richiedono conferma obbligatoria lato server/MCP prima dell'effetto e non possono affidarsi solo al client.

## Decisione

1. **Vault (Obsidian / Knowledge base):**
   - **`write_note` (sovrascrittura/creazione di note canoniche) e `forget_memory` (cancellazione chiavi o note di memoria):**
     Modificano o eliminano lo stato canonico del vault utente. Richiedono conferma obbligatoria lato server quando invocati via MCP con elicit/token se configurato un approvatore, oppure richiedono checkpoint/version conflict checking deterministico. In particolare per `forget_memory`, l'eliminazione definitiva è un'operazione distruttiva che richiede elicitazione/approvazione esplicita se non invocata in modalità batch o con flag di backup tracciato.
   - **`propose_memory`:**
     Non sovrascrive preferenze esplicite; aggiunge inferenze a bassa fiducia (soft candidate). Non è distruttivo e non richiede blocco d'approvazione, ma preserva le preferenze esplicite dell'utente come sancito da D08.
   - **`read_note`, `search_notes`, `get_profile`:**
     Operazioni strettamente read-only.

2. **Automazione Desktop e GUI (`execute_gui_action`):**
   - Le azioni GUI come `open_and_inspect` su applicazioni consentite (es. Notepad, Calcolatrice) con screenshot prima e dopo sono osservabili e benigne.
   - Tuttavia, qualunque azione GUI che comporti input di comandi non banali o chiusura forzata (`close` / `kill`) o invio di keystroke/interazione che muta dati applicativi è classificata come potenziale mutazione protetta.
   - Nel server MCP, `execute_gui_action` applica una triplice barriera server-side:
     1. Verifica rigorosa dell'interattività della sessione Windows (`DESKTOP_INTERACTIVE` via OpenInputDesktop, AT10). Se bloccata, fallisce immediatamente (`DESKTOP_UNAVAILABLE`).
     2. Concurrency lock esclusivo: mai più di un job GUI in esecuzione contemporanea (`CONCURRENT_GUI_JOB`).
     3. Allowlist stretta di applicazioni non elevate e azioni ammesse. Azioni o applicazioni non registrate vengono rifiutate con `PERMISSION_DENIED` indipendentemente dalle richieste dell'LLM.
     4. Quando un'azione GUI distruttiva o modificatrice viene richiesta (es. input batch o salvataggio/chiusura), l'approvazione viene richiesta al proprietario via elicitation sul server prima dell'esecuzione.

3. **Invariante D14 applicato a Vault e GUI:**
   - La conferma dell'approvatore (`ApprovalStore`) consuma un token crittografico monouso associato all'hash dei parametri specifici dell'azione.
   - Nessun flag `approved=true` o dizionario fornito dal client o dall'LLM può aggirare questo controllo lato server.
