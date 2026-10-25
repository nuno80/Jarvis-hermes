# ADR 0017: list_create via MCP con argomenti LLM e scelta tool forzata

- **Stato:** Accettato (rivisto)
- **Data:** 2026-10-25 (revisione: niente regex per gli argomenti)
- **Tracciabilità:** Issue #64; estende ADR 0005 e ADR 0016

## Contesto e decisione

Una richiesta vocale `list_create` è stata instradata correttamente, ma il
Reasoner ha annunciato la creazione senza chiamare Todoist. Un hint testuale
più forte lascerebbe invariato questo failure mode: il modello può ancora
scegliere di non chiamare il tool.

La bozza precedente (parsing regex degli item in `direct_action.py`) è stata
**scartata, non estesa**: una regex può riconoscere il pattern della frase ma
non interpreta quantità, articoli, modificatori, cibi composti o liste in
linguaggio naturale. Gli argomenti `{name, tasks[]}` li produce il System 2/LLM;
il codice deterministico fa solo da confine di validazione ed enforcement.

Flusso dello slice (solo `list_create` spesa):

1. `pre_turn_dispatch` decide SOLO l'instradamento (`system_1` / `deterministic`,
   `intent=handler=list_create`, `confidence=1.0`). `confidence = 1.0`
   certifica il routing, non autorizza alcuna write e non garantisce argomenti.
2. Il middleware `llm_request` (`constrain_llm_request_for_list_create`), puro e
   senza I/O, filtra i tool a `mcp__jarvis__todoist_create_task_list` e imposta
   `tool_choice="required"`: il Reasoner deve chiamare proprio quel tool, non
   può rispondere in sola prosa. Nessuna chiamata LLM aggiuntiva, nessun
   problema di hook-timeout: il System 2 resta responsabile degli argomenti.
3. `validate_list_create_arguments` applica la policy dello slice prima di ogni
   effetto: nome `Spesa`, 1..50 task non vuoti, niente duplicati
   case-insensitive. Qualsiasi dubbio → fallback al Reasoner, zero side effect.
4. L'esecuzione usa il registry MCP esistente di Hermes e il tool reale
   `todoist_create_task_list` (mai `TodoistClient` diretto dal plugin). Il
   successo richiede envelope `ok=true`, project id e conteggio task coerente;
   altrimenti il job va a `outcome_unknown`, il turno si chiude senza retry né
   rientro del Reasoner (un retry duplicherebbe progetto e task). Il
   `request_id` MCP coincide con il job id.

Non vengono introdotti un secondo poller, un client Todoist alternativo o un
bypass di `TodoistClient`. Il `ctx.call_mcp` diretto del plugin resta
default-deny (richiede `mcp_allowlist` dell'operatore): lo slice usa il flusso
tool dell'agente, già connesso al server MCP `jarvis`.

## Conseguenze

- Testo e vocale usano lo stesso gate; il vocale non esegue mai prima dello STT.
- `SAFE_DIRECT_HANDLERS` invariato; ogni futura direct action è una decisione a sé.
- Dopo una write tentata, risposta via invio plugin più `skip`, mai fallback
  fail-open del Reasoner (evita duplicati anche se la consegna fallisce).
- Da verificare sul gateway reale: una sola chiamata main-model per gli
  argomenti (non zero: lo zero valeva solo per la bozza regex scartata), un
  solo effetto MCP, latenza osservata.
