# ADR 0004: Conferma persistente per un effetto simulato

Stato: implementata la logica locale; callback Telegram e integrazione Hermes da verificare sul PC.

`ApprovalStore` espone un confine destinato soltanto a un adapter del gateway affidabile. L'adapter dovrà ricavare l'ID numerico dell'utente dalla sessione autenticata e verificare la chat autorizzata. Né il modello né un argomento MCP possono stabilire quell'identità o decidere una conferma. Per questo `decide()` non è pubblicato nel catalogo MCP. Nessun polling Telegram viene aggiunto.

`request()` prepara esclusivamente l'azione `simulate`: riepilogo con target, contenuto, digest SHA-256 dei parametri canonici e scadenza; token opaco casuale monouso, memorizzato solo come hash. Il token viene restituito all'adapter affinché lo consegni sul canale autenticato, mai inserito nel riepilogo. `decide()` controlla token, ID, digest, stato e tempo. Una transazione SQLite `BEGIN IMMEDIATE` registra l'effetto simulato e consuma la conferma in modo atomico; callback duplicate non ripetono l'effetto. L'annullamento consuma il token senza effetto; la scadenza lo rende inutilizzabile. I database runtime rimangono locali e ignorati da Git.

Il database della demo contiene soltanto un registro di effetti simulati. Non esegue comandi, push o invii. Per effetti esterni reali serviranno un'ulteriore progettazione dell'idempotenza e del recupero da esito ignoto, più il collegamento al gateway dopo ispezione dell'installazione esistente. L'API Python presume che i suoi chiamanti locali siano affidabili; SQLite non offre isolamento contro processi ostili con accesso alla directory. Il clock di sistema deve essere affidabile; la verifica usa la scadenza al momento della decisione.

Prova senza Telegram: `uv run jarvis approval-demo`. La CLI crea un database temporaneo, conferma una sola simulazione, prova a ripetere la callback e riporta il numero di effetti. Non emette il token. Il test automatico verifica anche identità diversa, modifica di target/parametri, `approved=true`, annullamento, scadenza, persistenza e callback concorrenti.

Per chiudere l'issue #3, completare prima #2 sul PC: identificare l'unico gateway Hermes e il suo punto di callback affidabile; collegare il riepilogo e i pulsanti di @nuno_agent_bot; provare approvazione, annullamento e duplicati da telefono senza aprire un secondo consumer. Conservare le prove senza token o ID personali nel repository.
