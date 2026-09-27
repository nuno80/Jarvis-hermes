AUDIT — JEV / System 1 / #17 / #38

> **Aggiornamento post-audit (fix fail-closed eseguito):**
> - Il bug confidence di sezione B/E.4 (NaN che supera la soglia) è **corretto**:
>   confidence mancante, NaN, ±inf, fuori [0,1] o non numerica → CLARIFICATION
>   fail-closed (router.py, test `test_jev_invalid_confidence_is_fail_closed_to_clarification`).
>   Risolto il punto "Fix fail-closed" del passo 2 del piano (G).
> - JEV è marcato "adapter futuro, non usato": status banner in docs/jev.md e
>   JEV_* commentate in .env.example. La voce REMOVE di JevClient resta
>   differita al passo 7 del piano, come da audit.
> - Il resto dell'audit è invariato: #17 e #38 ancora OPEN, System 1 non
>   implementato, prossimo passo = dataset/calibrazione (JARVIS-38 / GitHub #40).

 Contesto rilevato (CONFERMATO DAL CODICE): sei su branch
 feat/issue-17-local-model, ma è identico a origin/main (nessun
 commit sopra, working tree pulito — il nome del branch è
 fuorviante). main = 8ccf8f0, che include il commit 17dc866 "v1.2 —
 architettura System 1/System 2 al posto di Jev".

 A. Stato JEV

 ┌───────────────┬────────────────────────────────┬───────────────┐
 │ Voce          │ Esito                          │ Provenienza   │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ JEV runtime   │ NON attivo nel percorso reale. │ CONFERMATO    │
 │               │ JevClient esiste solo come     │ DAL CODICE    │
 │               │ client HTTP; route_request     │               │
 │               │ (tool MCP) istanzia il client  │               │
 │               │ solo se JEV_ENDPOINT_URL è     │               │
 │               │ impostato. Nessuna variabile   │               │
 │               │ JEV in ~/.hermes/config.yaml   │               │
 │               │ (env del server jarvis: solo   │               │
 │               │ vault path). Nessun            │               │
 │               │ processo/servizio JEV sulla    │               │
 │               │ macchina. Nessun .env con JEV. │               │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ JEV           │ Sì: JEV_ENDPOINT_URL +         │ CONFERMATO    │
 │ configurabile │ JEV_API_KEY in                 │ DAL CODICE    │
 │               │ router.py:136-137. Assenti da  │               │
 │               │ .env.example.                  │               │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ JEV realmente │ Solo dal tool MCP              │ CONFERMATO    │
 │ usato         │ route_request (diagnostica) e  │ DAL CODICE +  │
 │               │ da jarvis routing-demo (CLI).  │ CONFERMATO DA │
 │               │ Nessun percorso Telegram lo    │ SPEC          │
 │               │ tocca: il flusso reale è       │ (jarvis-v1.md │
 │               │ Telegram → Hermes gateway →    │ §7: "Un tool  │
 │               │ modello principale → tool MCP. │ MCP           │
 │               │ Il router non gira pre-turno.  │ route_request │
 │               │                                │  invocato dal │
 │               │                                │ modello       │
 │               │                                │ principale    │
 │               │                                │ non è un fast │
 │               │                                │ path")        │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ JEV           │ No. L'account TypeSafe/Jev non │ CONFERMATO DA │
 │ necessario    │ è ottenibile e l'API reale non │ GITHUB + SPEC │
 │               │ corrisponde al client          │               │
 │               │ ipotizzato (JARVIS-31, D13).   │               │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ Codice da     │ Il determinismo e il fallback  │ INFERITO      │
 │ mantenere     │ conservativo, non Jev:         │               │
 │               │ FAST_PATH_RULES,               │               │
 │               │ RoutingTarget, IntentRoute,    │               │
 │               │ telemetria,                    │               │
 │               │ _conservative_fallback come    │               │
 │               │ schema di escalation.          │               │
 │               │ Reusabili nel System 1.        │               │
 ├───────────────┼────────────────────────────────┼───────────────┤
 │ Codice legacy │ JevClient (router.py:133-179), │ CONFERMATO    │
 │               │ il ramo Step 2 in route(),     │ DAL CODICE    │
 │               │ ITALIAN_ROUTING_DATASET (9     │               │
 │               │ esempi, la spec la dichiara    │               │
 │               │ "solo un seed"), docs/jev.md,  │               │
 │               │ il test routing-demo, i test   │               │
 │               │ test_jev_* in                  │               │
 │               │ test_router.py/test_telemetry. │               │
 │               │ py/test_llm.py (coprono il     │               │
 │               │ contratto fallback,            │               │
 │               │ riutilizzabile come template). │               │
 └───────────────┴────────────────────────────────┴───────────────┘

 Percorso reale ricostruito (CONFERMATO DAL CODICE):

 ```
   Telegram → Hermes Gateway (poller esistente) → modello
 principale (Gemini)
     → eventuale tool MCP route_request (solo se il modello lo
 chiama: diagnostica)
     → dentro route(): fast-path deterministico → Jev (mai
 configurato → fallback) → fallback conservativo
 ```

 Il fallback "conservativo" di fatto gira sempre, con euristiche
 keyword che instradano push→git_push con confidence 0.75 — ma è un
 tool MCP readonly che non esegue nulla, quindi nessun rischio
 d'effetto diretto. È però un doppio router rispetto alla direzione
 System 1.

 B. Stato System 1

 ┌──────────────────┬───────────────────────────────┬─────────────┐
 │ Voce             │ Esito                         │ Provenienza │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Deterministic    │ Implementato                  │ CONFERMATO  │
 │ layer            │ (FAST_PATH_RULES, 3 intent)   │ DAL CODICE  │
 │                  │ ma agganciato solo al tool    │             │
 │                  │ MCP route_request, non        │             │
 │                  │ pre-turno.                    │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ System 1         │ Assente. Nessun modulo        │ CONFERMATO  │
 │ implementazione  │ decision, nessun runtime      │ DAL CODICE  │
 │                  │ locale, nessun output         │             │
 │                  │ vincolato, nessun logprob.    │             │
 │                  │ Niente in src/jarvis_hermes/. │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Hermes hooks     │ Spike validato, non prodotto. │ CONFERMATO  │
 │                  │  ADR 0005 dimostra su         │ DA ADR +    │
 │                  │ installazione reale (Hermes   │ CODICE      │
 │                  │ v0.21.5) che                  │             │
 │                  │ pre_gateway_dispatch (bypass  │             │
 │                  │ {"action":"skip"}),           │             │
 │                  │ llm_request (rewrite model) e │             │
 │                  │ pre_llm_call (context         │             │
 │                  │ injection) funzionano, senza  │             │
 │                  │ secondo poller e preservando  │             │
 │                  │ la prefix cache. Ma           │             │
 │                  │ ~/.hermes/plugins non esiste: │             │
 │                  │ nessun plugin Jarvis          │             │
 │                  │ installato.                   │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Telegram         │ Zero. Nessun componente       │ CONFERMATO  │
 │ integration      │ Jarvis gira nel percorso      │ DAL CODICE  │
 │                  │ Telegram reale.               │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Structured       │ Non esiste (non richiesto     │ —           │
 │ output           │ fino a #31).                  │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Logprob          │ Non esiste. Nota: c'è già un  │ CONFERMATO  │
 │                  │ Ollama in ascolto su          │ (processo)  │
 │                  │ 127.0.0.1:11434 sull'host —   │ / DA        │
 │                  │ candidato runtime per #17, da │ VERIFICARE  │
 │                  │ verificare versione e         │ (capacità)  │
 │                  │ supporto logprob/grammar.     │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Confidence       │ Bug noto confermato:          │ CONFERMATO  │
 │                  │ router.py:260 confidence =    │ DAL CODICE  │
 │                  │ float(...) poi <              │ + GITHUB    │
 │                  │ self.confidence_threshold;    │             │
 │                  │ nessun controllo              │             │
 │                  │ NaN/inf/range. float("nan") < │             │
 │                  │ 0.70 è False → NaN passa come │             │
 │                  │ alta confidenza. Segnalato    │             │
 │                  │ anche in #38 e nella spec     │             │
 │                  │ §stato reale.                 │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Telemetry        │ Implementata (#39 chiuso,     │ CONFERMATO  │
 │                  │ commit 8ccf8f0):              │ DAL CODICE  │
 │                  │ RoutingDecisionStore,         │             │
 │                  │ get_routing_report,           │             │
 │                  │ record_routing_correction.    │             │
 │                  │ Misura percorsi JEV/fallback, │             │
 │                  │ non System 1.                 │             │
 ├──────────────────┼───────────────────────────────┼─────────────┤
 │ Policy           │ Solido: il router è tool      │ CONFERMATO  │
 │ separation       │ readonly, nessuna             │ DAL CODICE  │
 │                  │ classeificazione concede      │             │
 │                  │ permessi; policy/approval     │             │
 │                  │ restano server-side           │             │
 │                  │ (approval.py,                 │             │
 │                  │ command_policy.py,            │             │
 │                  │ checkpoint). Invarianti       │             │
 │                  │ AT13/AT14 rispettate per      │             │
 │                  │ costruzione finché il System  │             │
 │                  │ 1 non esiste.                 │             │
 └──────────────────┴───────────────────────────────┴─────────────┘

 Nota su tests/test_hermes_hooks.py: testa funzioni finte definite
 nel test stesso, non Hermes né alcun codice Jarvis. Protegge solo
 la documentazione dei contratti (ADR 0005). Non è integrazione.

 C. Stato #17 (JARVIS-17 = GitHub #17)

- GitHub status: OPEN, ready-for-agent, "Non iniziato" —
   CONFERMATO DA GITHUB
- Implementazione trovata: nessuna. Il branch
   feat/issue-17-local-model è vuoto rispetto a main. — CONFERMATO
   DAL CODICE
- Benchmark trovato: nessuno (nessun report, nessuno script). —
   CONFERMATO DAL CODICE
- Modelli testati: nessuno. — CONFERMATO
- ADR trovato: nessun ADR su modello System 1 (ADR 0006 è su
   vault/GUI). — CONFERMATO
- Conclusione: System 1 non validato. Nessun modello locale
   scelto. La spec stessa dice "Modello System 1: aperto — scelto
   dal benchmark".

 D. Stato #38 (JARVIS-38 = GitHub #40)

- GitHub status: OPEN, ready-for-agent — CONFERMATO DA GITHUB
- Dataset trovato: no. tests/data/ non esiste; c'è solo
   ITALIAN_ROUTING_DATASET inline in router.py con 9 esempi. —
   CONFERMATO DAL CODICE
- Numero esempi: 9 / 200 richiesti. — CONFERMATO
- Calibrazione trovata: no. Soglia hardcoded 0.70 nel costruttore,
   non in config, non calibrata. — CONFERMATO
- ECE: mai calcolato. — CONFERMATO
- Threshold: nessuna per classe di azione; una sola globale. —
   CONFERMATO
- Report: nessuno (get_routing_report è telemetria runtime, non
   report di calibrazione). — CONFERMATO
- Conclusione: confidence del router non calibrata → nessun fast
   path basato su confidence è giustificabile oggi.

 E. Rischi

 1. Doppio router (CONFERMATO): route_request MCP + futuro System 1
    pre-turno. La spec già lo vincola a "solo diagnostica"; va
    mantenuto tale o rimosso alla migrazione.
 2. JEV ancora attivo: NO (CONFERMATO) — inattivo, ma importabile
    con una sola env var; rimozione = rimuovere il rischio di
    riattivazione accidentale.
 3. System 1 non calibrato (CONFERMATO) — non esiste.
 4. Confidence non affidabile (CONFERMATO): bug NaN in
    router.py:260 + keyword-heuristic del fallback che assegna
    confidence inventate (0.75/0.6). Oggi innocuo perché il tool
    non esegue nulla; diventa esplosivo se riusato come base del
    System 1.
 5. Bypass di policy: nessuno trovato (CONFERMATO) — enforcement
    resta nei tool MCP server-side.
 6. Fast path pericolosi: nessuno attivo (CONFERMATO); il fallback
    però classifica "push"→git_push con confidence 0.75 senza
    verifica — DA VERIFICARE che questo comportamento non venga
    ereditato.
 7. Fallback non conservativi: il fallback attuale instrada per
    keyword verso tool workflow; conservativo sui permessi, non sul
    target. Da rivedere in fase System 1.
 8. Documentazione obsoleta (CONFERMATO): docs/jev.md descrive Jev
    come tier attivo; docs/roadmap.md ha titoli vecchi (JARVIS-31
    "iniettare Jev", JARVIS-17 "task semplice su modello locale") —
    la roadmap stessa dice di fidarsi delle issue, ma i titoli
    traggono in inganno; README/mcp-readonly obsoleti (già
    tracciato in #39/GitHub #39 → issue GitHub #40 è JARVIS-40).

 F. Decisione proposta

 ┌───────────────────────────────┬────────────────────────────────┐
 │ Componente                    │ Azione                         │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Fast-path deterministico      │ KEEP (base del nuovo routing)  │
 │ (FAST_PATH_RULES, targets,    │                                │
 │ IntentRoute)                  │                                │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Telemetria routing (#39)      │ KEEP                           │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Policy/approval/checkpoint    │ KEEP — autorità finale,        │
 │ server-side                   │ intoccabile                    │
 ├───────────────────────────────┼────────────────────────────────┤
 │ JevClient + ramo Jev in       │ REMOVE (dopo VERIFIED: nessun  │
 │ route()                       │ attivatore in env/config       │
 │                               │ reale)                         │
 ├───────────────────────────────┼────────────────────────────────┤
 │ _conservative_fallback        │ MIGRATE → logica di escalation │
 │                               │ del System 1 (fixando NaN e    │
 │                               │ confidence inventate)          │
 ├───────────────────────────────┼────────────────────────────────┤
 │ ITALIAN_ROUTING_DATASET (9    │ MIGRATE → seed di              │
 │ esempi)                       │ tests/data/routing_it.jsonl    │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Tool MCP route_request        │ MIGRATE → diagnostica-only sul │
 │                               │ nuovo router, o REMOVE se      │
 │                               │ ridondante                     │
 ├───────────────────────────────┼────────────────────────────────┤
 │ docs/jev.md                   │ REMOVE/MIGRATE → storia in     │
 │                               │ ADR, fuori dai doc operativi   │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Plugin Hermes pre-turno (ADR  │ VERIFY FIRST → BUILD (spike    │
 │ 0005)                         │ chiuso, plugin da scrivere)    │
 ├───────────────────────────────┼────────────────────────────────┤
 │ Test test_jev_*, routing-demo │ MIGRATE (conservare le         │
 │                               │ asserzioni di                  │
 │                               │ fallback/permessi)             │
 └───────────────────────────────┴────────────────────────────────┘

 Sulla domanda architetturale (§7 del tuo messaggio): i dati
 confermano la seconda ipotesi come direzione, ma la decisione va
 presa dopo #17/#38. Nota however: la domanda "System 1 per tutto
 vs solo basso rischio" è in parte già risolta dalla spec — le
 soglie per classe di azione di #38 sono la seconda ipotesi
 formalizzata: alto rischio → nessun fast path possibile
 indipendentemente dalla confidence (AT13). Il benchmark #17 decide
 solo se il System 1 è abbastanza buono per le classi basse.

 G. Piano (ordine confermato, con una nota)

 L'ordine che proponi è corretto. Una sola inversione suggerita:
 #38 (dataset) prima di #17 (benchmark) — l'issue #17 richiede
 esplicitamente "Usa il dataset di #38 per la parte System 1", e
 #38 è l'unica senza blocker. Valutare il modello su 9 esempi non
 avrebbe significato statistico.

 1. Audit ✅ (questo documento)
 2. JARVIS-38 / GitHub #40 — tests/data/routing_it.jsonl (≥200
    esempi, etichette appendice I1, split cal/test), harness con
    accuratezza, confusion matrix, reliability curve, ECE,
    escalation rate, fast-path errati su azioni protette. Fix
    fail-closed della confidence (NaN/inf/fuori range/assente →
    escalation) nel codice di valutazione, così il bug è morto
    prima che il System 1 nasca.
 3. JARVIS-17 / GitHub #17 — benchmark su RTX 5070 12GB, ≥2 modelli
    piccoli, output vincolato + logprob (llama.cpp/llama-cpp-python
    o Ollama se supporta grammar+logprob — da verificare
    l'installazione esistente su :11434), p50/p95 ≤300ms, VRAM,
    italiano, richieste ambigue e protette → ADR modello scelto.
 4. Decisione architetturale — con i numeri di 2+3: soglie per
    classe; in pratica "System 1 solo per classi sicure, tutto il
    resto → System 2" se l'ECE non regge, altrimenti fast path
    esteso alle classi basse. Scritta in ADR.
 5. JARVIS-31 — implementare decision + plugin Hermes
    (pre_gateway_dispatch/llm_request/pre_llm_call secondo ADR
    0005), consumo nel budget, telemetria.
 6. Test — unit + integrazione su host reale (turni Telegram con
    log percorso/confidenza/latenza, AT13/AT14/AT15).
 7. Rimozione JEV — solo ora: eliminare JevClient, ramo Step 2,
    docs/jev.md, JEV_* da ogni riferimento; i test di fallback
    migrati prima. Verifica finale uv sync --locked, unittest,
    doctor --json.
 8. Documentazione — README, docs/mcp-readonly.md, roadmap index
    (titoli aggiornati), spec già a posto.
 9. Issue — aggiornare #17/#38/#31 con evidenze; chiudere solo con
    evidenza reale.

 Da confermare col tuo go: partire da #38 (dataset + fix
 fail-closed) come primo task eseguibile.
