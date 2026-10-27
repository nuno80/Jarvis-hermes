# ADR 0006: Conferme server-side per tool distruttivi su vault e GUI

Stato: Accettata.

## Contesto

Secondo la specifica JARVIS V1 (D07, D14, sezione 6, C1) le annotazioni MCP dei tool (`destructive`, `readonly`) sono puramente informative e non costituiscono né garantiscono un confine di sicurezza. Un agent harness o un client non conforme (o compromesso) potrebbe invocare un tool annotato come distruttivo senza sollecitare l'approvazione dell'utente (`ctx.elicit`).

Occorre definire chiaramente quali operazioni su **vault** e **GUI** sono distruttive o ad alto impatto, richiedono conferma obbligatoria lato server/MCP prima dell'effetto e non possono affidarsi solo al client.

## Decisione

1. **Vault (Obsidian / Knowledge base):**
   - **`write_note` (sovrascrittura/creazione di note canoniche) e `forget_memory` (cancellazione chiavi o note di memoria):**
     Modificano o eliminano lo stato canonico del vault utente. Richiedono conferma obbligatoria lato server via `ctx.elicit` e token monouso (`ApprovalStore`), con controllo versione deterministico prima del prompt. Nessuna modalità batch e nessun flag fornito dal client esonera dal consenso: il consenso mancante, rifiutato o non disponibile fallisce chiuso con `APPROVAL_DECLINED` senza alcun effetto.
   - **`propose_memory`:**
     Non sovrascrive preferenze esplicite; crea o aggiorna soltanto note candidate validate (`source: inferred` con `status: candidate` o `type: candidate_preference`) e preserva le preferenze esplicite (D08). Una `note_id` canonica o esplicita produce `PERMISSION_DENIED` senza modifiche. Non richiede blocco d'approvazione perché non tocca lo stato canonico.
   - **`update_preference` (preferences.yaml):**
     Scrittura canonica: richiede `expected_version` esplicita e consenso via elicitation come `write_note`.
   - **`read_note`, `search_notes`, `get_profile`:**
     Operazioni strettamente read-only.

2. **Automazione Desktop e GUI (`execute_gui_action`):**
   - Le azioni GUI come `open_and_inspect` su applicazioni consentite (es. Notepad, Calcolatrice) con screenshot prima e dopo sono osservabili e benigne: restano accessibili senza richiesta di conferma.
   - Tuttavia, qualunque azione GUI modificatrice o distruttiva (come la chiusura forzata `close` o futuro input/salvataggio dati) richiede obbligatoriamente il consenso dell'utente via `ctx.elicit` al boundary MCP prima dell'esecuzione.
   - Senza elicitation o in caso di rifiuto/timeout, `close` fallisce chiuso con `APPROVAL_DECLINED` e non raggiunge né il manager GUI né PowerShell.
   - La scrittura simulata non implementata (`write_test_note`) è ritirata dall'allowlist: qualsiasi invocazione produce `ACTION_NOT_PERMITTED` senza sollecitare l'utente.
   - Nel server MCP, `execute_gui_action` applica una quadrupla barriera server-side:
     1. Validazione statica dell'allowlist di applicazioni non elevate e azioni ammesse (`PERMISSION_DENIED` o `ACTION_NOT_PERMITTED` immediati prima di ogni effetto o richiesta di consenso).
     2. Verifica del consenso via `ctx.elicit` al boundary MCP per qualsiasi azione non osservativa (`close`), con token monouso (`ApprovalStore`).
     3. Verifica rigorosa dell'interattività della sessione Windows (`DESKTOP_INTERACTIVE` via OpenInputDesktop, AT10). Se bloccata, fallisce immediatamente (`DESKTOP_UNAVAILABLE`).
     4. Concurrency lock esclusivo: mai più di un job GUI in esecuzione contemporanea (`CONCURRENT_GUI_JOB`).

3. **Invariante D14 applicato a Vault e GUI:**
   - La conferma dell'approvatore (`ApprovalStore`) consuma un token crittografico monouso associato all'hash dei parametri specifici dell'azione.
   - Nessun flag `approved=true` o dizionario fornito dal client o dall'LLM può aggirare questo controllo lato server.

4. **Matrice operazione / versione / consenso / target (stato implementato, fix 1):**

   | Operazione | Versione richiesta | Consenso | Target ammessi |
   |---|---|---|---|
   | `write_note` crea | `expected_version: "new"`, altrimenti `CONFLICT` | elicitation + token monouso, altrimenti `APPROVAL_DECLINED` | qualsiasi nota visibile `.md` nel vault |
   | `write_note` sovrascrive | hash corrente della nota, altrimenti `CONFLICT` | elicitation + token monouso, altrimenti `APPROVAL_DECLINED` | qualsiasi nota visibile `.md` nel vault |
   | `update_preference` | digest corrente di `preferences.yaml`, altrimenti `CONFLICT` | elicitation + token monouso, altrimenti `APPROVAL_DECLINED` | chiavi non in `FORBIDDEN_PREFERENCE_KEYS`, altrimenti `PERMISSION_DENIED` |
   | `propose_memory` | `new` implicito per nota assente; hash corrente per candidata esistente | nessuno (solo candidate) | solo note nuove o candidate validate (`source: inferred`); canoniche → `PERMISSION_DENIED` |
   | `forget_memory` intera / chiave | hash corrente obbligatorio, altrimenti `CONFLICT` | elicitation + token monouso, altrimenti `APPROVAL_DECLINED`; backup in `.jarvis_backups` prima dell'effetto | solo note candidate validate; canoniche → `PERMISSION_DENIED` |
   | `read_note`, `search_notes`, `get_profile` | nessuna | nessuno | read-only |
   | `execute_gui_action` (`open_and_inspect`) | nessuna | nessuno (osservativo) | sole app in allowlist (`notepad`, `calc`) |
   | `execute_gui_action` (`close`) | nessuna | elicitation + token monouso, altrimenti `APPROVAL_DECLINED` (non tocca PowerShell) | sole app in allowlist (`notepad`, `calc`) |
   | `execute_gui_action` (`write_test_note`) | non applicabile | non applicabile (`ACTION_NOT_PERMITTED` prima del consenso) | nessuno (ritirata dall'allowlist) |

   Ordine di valutazione: versione prima del consenso (una versione stale restituisce `CONFLICT` senza sollecitare il proprietario); target canonico prima di ogni effetto per i tool di memoria inferita (`PERMISSION_DENIED` senza modifiche a frontmatter o corpo).
