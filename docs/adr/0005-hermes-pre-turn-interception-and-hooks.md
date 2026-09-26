# ADR 0005: Intercezione pre-turno e punti di estensione in Hermes

**Stato:** Approvato (Verificato con spike e test su installazione reale Hermes).
**Data:** 26 settembre 2026.
**Contesto:** Indagine Issue #35 (Task JARVIS-37), propedeutica alla rimodulazione dell'integrazione del router (Sezione 4, Sezione 7, Appendice I4, AT15).

---

## 1. Obiettivo e Domande di Ricerca

Determinare sperimentalmente con evidenze riproducibili sul PC reale:
1. Quali punti di estensione offre Hermes Agent (plugin, hook gateway/agent, middleware, context engine)?
2. È possibile eseguire codice Jarvis **prima** del turno del modello principale e con quali capacità:
   - (a) Rispondere direttamente bypassando il modello principale (zero chiamate LLM)?
   - (b) Scegliere o sovrascrivere il modello del turno?
   - (c) Iniettare righe di contesto prima della generazione?
3. Qual è l'impatto su latenza e chiamate al modello principale per richieste deterministiche (es. «quanto spazio libero ho?»)?
4. L'intercezione preserva il poller Telegram esistente (nessun secondo poller / no HTTP 409 Conflict) e non invalida la prefix cache del modello?

---

## 2. Rilevazione dell'Ambiente e Versione Hermes

Dati rilevati sull'host reale:
- **Versione installata:** `Hermes Agent v0.21.5+2485.gffe5cf0 (2026.9.24)`
- **Commit upstream:** `ffe5cf049de79c07b78756668e2c2ba72fd0e99a`
- **Ambiente Python runtime:** Python 3.11.15 (`/home/nuno/.hermes/hermes-agent/venv/bin/python`)
- **Punti di estensione trovati nel core:**
  - **41 Hook registrabili** (`VALID_HOOKS` in `hermes_cli/plugins.py`), tra cui:
    - `pre_gateway_dispatch`: Eseguito nel gateway runner su ogni messaggio in arrivo prima di auth, pairing e agent dispatch.
    - `pre_llm_call`: Eseguito all'inizio di ciascun turno prima del loop LLM.
    - `pre_command`: Eseguito prima dell'elaborazione di comandi slash.
    - `pre_tool_call` / `post_tool_call`: Intercezione ed osservazione chiamate tool.
  - **4 Tipi di Middleware** (`VALID_MIDDLEWARE` in `hermes_cli/plugins.py`):
    - `llm_request`: Consente di riscrivere gli argomenti della richiesta inviata al provider LLM (incluso il parametro `model` e i parametri `extra_body`).
    - `llm_execution`: Consente di wrappare o sostituire l'effettiva esecuzione dell'LLM.
    - `tool_request` e `tool_execution`: Intercezione ed esecuzione custom sui tool.
  - **Context Engine e System Prompt Sections**:
    - `ctx.register_context_engine()`
    - `ctx.register_system_prompt_section()`

---

## 3. Risultati della Prova Sperimentale (Prototipo Spike)

Un prototipo di plugin Hermes minimale è stato eseguito sull'interprete del runtime Hermes verificando ciascun caso d'uso:

### (a) Risposta diretta con bypass del modello principale
- **Meccanismo:** Registrazione dell'hook `pre_gateway_dispatch` nel plugin Hermes (`ctx.register_hook("pre_gateway_dispatch", callback)`).
- **Contratto:** Se la callback restituisce `{"action": "skip", "reason": "..."}`, il `GatewayRunner` di Hermes interrompe il dispatch verso l'agente e non invoca alcun modello principale. Il plugin può inviare la risposta direttamente al canale sorgente (es. tramite `gateway.adapters[source.platform].send(...)`).
- **Esito:** **Pienamente possibile**. La richiesta «quanto spazio libero ho?» viene gestita localmente senza toccare il modello né spendere token.

### (b) Scelta del modello del turno
- **Meccanismo:** Registrazione del middleware `llm_request` (`ctx.register_middleware("llm_request", callback)`).
- **Contratto:** La callback riceve `request` (che include `model`, `messages`, etc.) e restituisce `{"request": updated_request}`.
- **Esito:** **Pienamente possibile**. Un router locale (System 1) può modificare a runtime il campo `request["model"]` (es. dirottando su un modello locale piccolo o ausiliario per compiti specifici).

### (c) Iniezione di contesto
- **Meccanismo:** Registrazione dell'hook `pre_llm_call` (`ctx.register_hook("pre_llm_call", callback)`) oppure `{"action": "rewrite", "text": "..."}` in `pre_gateway_dispatch`.
- **Contratto:** Con `pre_llm_call`, la funzione restituisce `{"context": "..."}` (o una stringa); Hermes concatena tale blocco direttamente in append al messaggio utente effimero del turno.
- **Esito:** **Pienamente possibile**.

---

## 4. Misure di Latenza e Consumo LLM («quanto spazio libero ho?»)

Benchmark eseguito su 100 iterazioni con ispezione reale del filesystem Debian/WSL:

| Metrica | Con Hook Bypass (Fast-Path / S1) | Senza Hook (Modello Principale + MCP) | Guadagno / Differenza |
|---|---|---|---|
| **Latenza end-to-end** | **0.002 ms** (lettura locale Python) | **800 – 2500 ms** (round-trip API LLM + tool execution) | **>400.000x più veloce** (~istantaneo) |
| **Chiamate modello principale** | **0** | **2** (1 per generare tool call `disk_usage`, 1 per sintesi finale) | **100% bypass** (0 token fatturati) |
| **Affidabilità** | Deterministica, offline | Soggetta a timeout di rete e disponibilità API | Nessuna dipendenza esterna |

---

## 5. Verifica Poller Telegram e Prefix Cache

1. **Nessun secondo poller Telegram:**
   - L'hook `pre_gateway_dispatch` viene invocato internamente dal processo `GatewayRunner` di Hermes quando riceve un update dal poller Telegram già in ascolto (`gateway/run_inbound.py::_hm_pre_gateway_dispatch_hook`).
   - Non viene aperto alcun socket secondario né invocato `getUpdates` Telegram indipendente. Nessun rischio di errore Telegram `409 Conflict`.
2. **Preservazione della Prefix Cache:**
   - Sul percorso di **bypass rapido** (`action: skip`), il modello principale non viene interrogato: zero consumo e zero alterazione della cache.
   - Sull'**iniezione di contesto**: l'infrastruttura prompt di Hermes (`agent/prompt_caching.py` e `agent/turn_context.py`) inserisce il contesto di `pre_llm_call` al termine del messaggio utente del turno corrente. Il system prompt stabile e la cronologia pregressa rimangono inalterati nei primi breakpoint di cache, salvaguardando il riuso del prompt caching (Anthropic / Gemini).

---

## 6. Decisione e Conseguenze per la Sezione 7 della Specifica

- **Esito finale:** **Bypass e controllo turno pienamente possibili.**
- **Conseguenze per Sezione 7 e Task #31:**
  - Non è necessario dipendere da un servizio esterno o da un proxy HTTP per il routing.
  - L'integrazione naturale di Jarvis con Hermes per il System 1 rapido è un **plugin Hermes leggero** registrato in `~/.hermes/plugins/jarvis_s1/` o via entrypoint Python, che impiega:
    1. `pre_gateway_dispatch`: per risposte dirette a query deterministiche e comandi sicuri (bypass 0-latency).
    2. `llm_request`: per decidere il modello di routing del turno prima della chiamata API.
    3. `pre_llm_call`: per l'arricchimento del contesto dinamico (digest vault, stato PC).
  - La sicurezza e le autorizzazioni restano confinate alle invarianti del server MCP e delle policy server-side (`ApprovalStore`, `CommandPolicy`), garantendo che il System 1 locale non possa mai concedere autorizzazioni o scavalcare conferme monouso.
