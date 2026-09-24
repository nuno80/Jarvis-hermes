# ADR 0004: Conferma persistente per un effetto simulato

Stato: logica locale conservata; integrazione MCP elicitation riesposta dopo la diagnosi della prova Telegram.

`ApprovalStore` espone un confine destinato soltanto a un adapter del gateway affidabile. Né il modello né un argomento MCP possono stabilire l'identità dell'approvatore o decidere una conferma. `actor_id` e `decide()` non sono esposti nel catalogo MCP. Nessun polling Telegram viene aggiunto.

`request()` prepara esclusivamente l'azione `simulate`: riepilogo con target, contenuto, digest SHA-256 dei parametri canonici e scadenza; token opaco casuale monouso, memorizzato solo come hash. Il token viene restituito all'adapter affinché lo consegni sul canale autenticato, mai inserito nel riepilogo. `decide()` controlla token, ID, digest, stato e tempo. Una transazione SQLite `BEGIN IMMEDIATE` registra l'effetto simulato e consuma la conferma in modo atomico; callback duplicate non ripetono l'effetto. L'annullamento consuma il token senza effetto; la scadenza lo rende inutilizzabile. I database runtime rimangono locali e ignorati da Git.

Il database della demo contiene soltanto un registro di effetti simulati. Non esegue comandi, push o invii. Per effetti esterni reali serviranno un'ulteriore progettazione dell'idempotenza e del recupero da esito ignoto, più il collegamento al gateway dopo ispezione dell'installazione esistente. L'API Python presume che i suoi chiamanti locali siano affidabili; SQLite non offre isolamento contro processi ostili con accesso alla directory. Il clock di sistema deve essere affidabile; la verifica usa la scadenza al momento della decisione.

Prova senza Telegram: `uv run jarvis approval-demo`. La CLI crea un database temporaneo, conferma una sola simulazione, prova a ripetere la callback e riporta il numero di effetti. Non emette il token. Il test automatico verifica anche identità diversa, modifica di target/parametri, `approved=true`, annullamento, scadenza, persistenza e callback concorrenti.

## Esito della prova Telegram: causa accertata

Il collegamento di `simulate_with_approval` alla form-mode MCP elicitation di Hermes v0.20.0 è stato riesaminato sui log del gateway del 23 settembre 2026. Il rifiuto non è stato aggirato: l'utente ha approvato, ma **la stessa azione è stata confermata più volte**.

Il gateway registra il proprio esito per ogni pressione del pulsante:

| Ora | Evento | Esito |
|---|---|---|
| 20:13:45 | elicitation target `prova-telegram-1` | 20:13:47 `choice=once` → `executed` |
| 20:15:18 | elicitation target `prova-telegram-2` («verifica rifiuto») | 20:15:20 `choice=once` → `executed` |
| 20:17:57 | *seconda* elicitation, stesso target `prova-telegram-2` | 20:18:06 `choice=once` → `executed` |

L'intero log del gateway non contiene alcun `choice=deny`. La richiesta `prova-telegram-2` è passata tre volte in quattro minuti, ogni volta con una approvazione reale `Allow Once`; la schermata finale `Approved once` corrisponde all'ultimo tap dell'utente, non a una decisione autonoma del sistema. Tre approvazioni su un cancello senza stato producono tre esecuzioni: il comportamento era coerente, il requisito no.

Due difetti reali sono stati corretti di conseguenza:

1. **Il conteggio degli effetti non era per decisione.** Il tool restituiva `len(store.effects())`, il totale del registro, quindi la seconda conferma riportava 2 o 3 in un campo che sembrava descrivere quella singola azione. `decide()` restituisce ora `effects_recorded` (1 esecuzione, 0 annullamento) e il tool propaga quel valore con `decision`; un chiamante non può più leggere un contatore condiviso come prova della propria esecuzione.
2. **L'esito mancava del collegamento al riepilogo.** Il risultato espone `decision` (`accept`/`decline`/`unavailable`), così ogni decisione resta tracciabile alla richiesta che l'ha generata.

Il limite dichiarato resta: l'elicitation di Hermes è un cancello *senza stato e riutilizzabile*, mentre `ApprovalStore` è monouso. Nessun cancello che chiede conferma a ogni chiamata può deduplicare due richieste distinte e identiche: se un agente ripete la stessa domanda, l'utente riceve una seconda richiesta e una seconda approvazione è una seconda esecuzione. Il monouso protegge dal replay della *stessa* conferma (token, digest, identità, scadenza), non dalla ripetizione della *domanda*; contenere quella ripetizione richiede un limite nel gateway o nel runtime, non in questo modulo.

Il tool è quindi riesposto, limitato a effetti simulati. Un esito `executed` dimostra che il proprietario ha approvato quella richiesta, non che una sola richiesta sia stata inviata: non collegarlo a effetti esterni reali.

## Prova di accettazione su Telegram, 24 settembre 2026

Due chiamate reali identiche (`target=prova-rifiuto`, `arguments={"nota":"test deny"}`), quindi stesso digest, con tap diversi. Fonte: log del gateway e registro locale.

| Tap | `decision` | `status` | `effects_recorded` | Riga nel registro |
|---|---|---|---|---|
| Allow Once | `accept` | `executed` | `1` | sì |
| **Deny** | `decline` | `cancelled` | `0` | **no** |

Il digest è identico (`6f4669187f74…`) in entrambe le risposte, a conferma che la seconda chiamata era davvero la stessa richiesta. Il registro contiene una sola riga con quel target; lo stato delle richieste è 5 `executed`, 1 `cancelled`, nessuna `pending`. Il log del gateway registra `choice=deny` per la prima volta in tutto il suo storico (una occorrenza), a riprova che il rifiuto non era mai stato premuto prima della diagnosi.

Questa prova copre il criterio che era rimasto aperto nella issue: il rifiuto reale annulla l'azione senza registrare alcun effetto, e l'accettazione reale registra un solo effetto descritto dalla propria decisione.
