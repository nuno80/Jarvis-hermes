Piano di attuazione

 Il piano comprende i fix 1–5 e 7–16. Il fix 6 resta escluso per scelta: non
 introdurremo sandbox, consenso obbligatorio o limitazioni read-only per Pi;
 interverremo solo su concorrenza, collisioni e lifecycle previsti dagli altri fix.

 La baseline attuale è fd6558d, con worktree pulito. Poiché include modifiche
 successive all’audit, la prima attività è rieseguire la verifica completa.

 Fase 0 — Preparazione e tracciamento

 1. Rieseguire sulla baseline corrente:

    ```bash
      uv sync --locked
      uv run python -m unittest discover -s tests -v
      uv run jarvis doctor --json
    ```

 2. Mappare i fix sulle GitHub Issue esistenti e creare solo quelle mancanti, senza
    duplicare il tracking in file locali.
 3. Organizzare il lavoro in PR piccole:
     - PR A: fix 1 e 2 — vault e checkpoint.
     - PR B: fix 3 e 4 — Git ed email.
     - PR C: fix 5 — GUI.
     - PR D: fix 7, 8 e 9 — processi, startup e backup.
     - PR E: fix 10 — concorrenza Pi, senza fix 6.
     - PR F: fix 11 — reminder.
     - PR G: fix 12 e 13 — IMAP e profilo.
     - PR H: fix 14, 15 e 16 — metadati, test e pulizia.
 4. Ogni PR deve contenere il test che fallirebbe senza il relativo fix. Non si
    useranno mock per dichiarare verificata un’integrazione reale.

 ────────────────────────────────────────────────────────────────────────────────

 Fase 1 — Perdita dati e consenso

### Fix 1 — Proteggere le note canoniche del vault

 Azione

- Rendere obbligatorio expected_version per write_note: hash corrente per una nota
   esistente, new per una creazione.
- Aggiungere elicitation MCP per le mutazioni canoniche, riutilizzando Context,
   ApprovalStore e _ApprovalResponse esistenti.
- Consentire a propose_memory di modificare soltanto note nuove o
   candidate/inferred validate tramite frontmatter.
- Applicare la stessa restrizione a forget_memory, mantenendo il backup prima
   della cancellazione.
- Rifiutare note esplicite/canoniche passate come note_id a propose_memory o
   forget_memory.
- Aggiornare ADR 0006 con una matrice chiara: operazione, versione richiesta,
   conferma richiesta e target ammessi.

 Perché è importante

 Il vault contiene preferenze e memoria che influenzano anche le richieste
 successive. Una scrittura incontrollata non causa soltanto una perdita di dati:
 può cambiare il comportamento futuro dell’assistente.

 Cosa risolve

 Elimina le sovrascritture senza controllo di versione, l’uso dei tool per memoria
 inferita contro note canoniche e l’esecuzione di mutazioni protette da parte di
 client privi di elicitation.

 Accettazione

- Senza elicitation, sovrascrittura e cancellazione protette vengono rifiutate
   senza modificare il file.
- Una versione stale restituisce CONFLICT.
- propose_memory(note_id=<nota canonica>) restituisce PERMISSION_DENIED.
- Il normale flusso candidate → lettura → cancellazione continua a funzionare.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 2 — Rendere sicuro il ripristino dei checkpoint

 Azione

- Aggiungere expected_current_hash obbligatorio al tool MCP restore_checkpoint.
- Rendere obbligatorio anche il job ID atteso.
- Passare entrambi i valori a CheckpointManager.restore_checkpoint().
- Documentare il flusso:
  - create_checkpoint fornisce l’hash iniziale;
  - safe_write_file fornisce il new_hash;
  - restore_checkpoint richiede l’ultimo hash noto.
- Aggiornare tutti i caller, i test MCP e la documentazione del contratto.

 Perché è importante

 Il rollback deve proteggere il lavoro, non cancellarlo. Tra l’ultima scrittura del
 job e il ripristino possono intervenire modifiche umane o di un altro processo.

 Cosa risolve

 Trasforma il controllo di conflitto già presente nel manager in una protezione
 effettivamente utilizzata dal percorso MCP, impedendo rollback basati su uno stato
 obsoleto.

 Accettazione

- Hash corretto: ripristino riuscito.
- File modificato dopo l’ultimo hash noto: CONFLICT e contenuto preservato.
- Hash o job ID mancanti: richiesta rifiutata prima di toccare il file.
- Un checkpoint non può essere ripristinato da un job diverso.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 3 — Impedire commit di file non verificati

 Azione

 Adottare inizialmente la strategia minima fail-closed:

 1. Prima di git add, verificare che l’indice non contenga modifiche preesistenti.
 2. Se esistono, restituire un errore esplicito come INDEX_NOT_CLEAN.
 3. Dopo git add, verificare che l’insieme staged corrisponda esattamente ai file
    autorizzati.
 4. Eseguire il commit soltanto se entrambi i controlli passano.
 5. Coordinare questa operazione con la serializzazione per progetto del fix 10.

 Non cambiare le regole esistenti su hook, verification run e no_verify.

 Perché è importante

 Il commit rappresenta il confine tra codice verificato e codice pronto per essere
 pubblicato. Includere file estranei invalida il significato della verifica
 precedente.

 Cosa risolve

 Impedisce che modifiche già staged, appartenenti all’utente o a un altro job,
 vengano incluse accidentalmente nel commit del task.

 Accettazione

- Un file estraneo pre-staged blocca il commit.
- HEAD, indice e working tree restano invariati dopo il rifiuto.
- Il commit normale di uno o più file verificati continua a funzionare.
- Restano coperti hook, file nuovi, modifiche dopo la verifica e no_verify.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 4 — Vincolare l’approvazione email ai byte degli allegati

 Azione

- Sostituire il contenuto testuale generico con content_base64 nel contratto di
   invio.
- Decodificare una sola volta e applicare limiti costanti per allegato e
   dimensione totale.
- Calcolare SHA-256 e size_bytes direttamente dai byte decodificati.
- Rifiutare eventuali metadati dichiarati non coincidenti, oppure rimuoverli dal
   contratto e ricavarli sempre internamente.
- Usare lo stesso oggetto bytes per digest e MIME.
- Aggiornare manager, CLI, MCP, test e ADR 0013.

 Perché è importante

 Per un’azione esterna irreversibile, il digest approvato deve identificare il
 contenuto realmente trasmesso. Dati dichiarati dal caller non sono una prova di
 integrità.

 Cosa risolve

 Elimina la possibilità che approvazione e audit descrivano un allegato mentre SMTP
 ne invia un altro.

 Accettazione

- Contenuto diverso da hash/size dichiarati: rifiuto prima dell’invio.
- Testo e allegati binari: round-trip MIME corretto.
- Il digest nel record di audit corrisponde ai byte effettivamente passati a SMTP.
- Tutti i test SMTP restano simulati; nessuna email reale è necessaria.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 5 — Allineare la GUI alle regole di consenso e agli effetti reali

 Azione

- Rendere execute_gui_action asincrono e aggiungere Context.
- Richiedere elicitation per close e per ogni futura azione modificatrice.
- Lasciare open_and_inspect senza conferma, perché osservativo.
- Come intervento minimo, rimuovere write_test_note dall’allowlist invece di
   mantenere una funzione non implementata.
- Se in futuro verrà reintrodotto, dovrà avere inserimento reale e verifica
   dell’effetto.
- Aggiornare ADR 0006, inventario e test.

 Perché è importante

 La chiusura forzata può perdere dati non salvati. Inoltre, un risultato ok per
 un’azione mai eseguita induce l’utente e i passaggi successivi a fare affidamento
 su uno stato falso.

 Cosa risolve

 Impedisce chiusure non approvate ed elimina il falso successo della scrittura
 simulata.

 Accettazione

- Senza elicitation, close viene negato e non raggiunge il manager.
- Con consenso, viene eseguita una sola azione.
- write_test_note restituisce ACTION_NOT_PERMITTED.
- open_and_inspect resta disponibile.
- Il test finale su Windows verifica chiusura e stato osservato sul desktop reale.

 ────────────────────────────────────────────────────────────────────────────────

 Fase 2 — Lifecycle dei processi e dello stato

### Fix 8 — Separare costruzione del server e riconciliazione all’avvio

 Va implementato prima dei fix 10 e 11, che dipendono da un lifecycle chiaro.

 Azione

- Rendere build_server() una costruzione senza effetti sui job e sui reminder.
- Eseguire reconcile_on_startup() soltanto nel percorso reale main()/serve.
- Mantenere l’inventario e i test di introspection isolati dallo stato
   predefinito.
- Non introdurre un secondo poller o una seconda istanza del servizio.
- Conservare la riconciliazione dei reminder scaduti durante il downtime.

 Perché è importante

 Una funzione chiamata per costruire o ispezionare il server non deve modificare lo
 stato operativo. La riconciliazione ha senso solo come evento di avvio del
 servizio.

 Cosa risolve

 Evita che inventory, test o una seconda costruzione trasformino job attivi in
 outcome_unknown e li rendano impossibili da cancellare.

 Accettazione

- Due build_server() consecutivi lasciano un job attivo in running.
- inventory_tools.py non cambia i database.
- L’avvio reale continua a riconciliare job interrotti e reminder scaduti.
- Il comportamento preesistente del doctor resta read-only.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 7 — Terminare e verificare tutto l’albero dei processi

 Azione

- Riutilizzare un unico helper in jobs.py per JobStore, workflow e percorso Pi
   interessato dal timeout.
- Su POSIX, verificare il gruppo anche quando il leader è terminato.
- Escalare da SIGTERM a SIGKILL se rimane qualsiasi membro attivo.
- Su Windows, usare la terminazione dell’albero e verificarne l’effetto sull’host
   reale.
- Dopo la terminazione, limitare anche attesa e drenaggio delle pipe; nessun
   communicate() senza limite.
- Riportare process_stopped=True soltanto quando non restano discendenti.

 Perché è importante

 Terminare il solo processo principale lascia attivi processi che possono
 continuare a scrivere file, usare risorse o trattenere le pipe del chiamante.

 Cosa risolve

 Corregge sia il falso process_stopped=True sia il blocco del workflow dopo il
 timeout.

 Accettazione

- Un figlio che ignora SIGTERM non sopravvive alla cancellazione.
- Se qualcosa resta attivo, il risultato dichiara esplicitamente la terminazione
   non verificata.
- Un workflow con figlio che eredita stdout/stderr restituisce entro un tempo
   limitato.
- Nessun processo o directory temporanea resta dopo i test.
- Il comportamento Windows è verificato sul vero host prima della chiusura
   dell’issue.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 9 — Rendere il restore backup fail-closed

 Azione

- Estrarre il backup nello staging come avviene oggi.
- Eseguire neutralizzazione di jobs e approvals dentro lo staging.
- Se una riconciliazione fallisce, sollevare BackupError e non copiare nulla nei
   target.
- Copiare stato e vault soltanto dopo una riconciliazione completa.
- Eliminare i due except Exception: pass nei passaggi di sicurezza.
- Mantenere il divieto di riesecuzione cieca e la cancellazione delle approvazioni
   pendenti.

 Perché è importante

 Un restore non è soltanto una copia di file: deve anche rendere inerti operazioni
 rimaste a metà. Dichiarare successo quando questo passaggio fallisce crea un falso
 confine di sicurezza.

 Cosa risolve

 Impedisce che un backup ripristinato come “sicuro” contenga approvazioni ancora
 pendenti o job attivi che dovevano essere neutralizzati.

 Accettazione

- Errore SQLite su approvals: restore fallito e target invariati.
- Errore SQLite su jobs: stesso comportamento.
- Restore normale: approvals pendenti cancellate e job attivi marcati
   outcome_unknown.
- Un file canary nei target dimostra che un restore fallito non ha applicato
   modifiche parziali.

 ────────────────────────────────────────────────────────────────────────────────

 Fase 3 — Carico concorrente e funzionalità mancanti

### Fix 10 — Admission control e identificatori unici per Pi

 Questo intervento non applica il fix 6.

 Azione

- Aggiungere UUID a nomi dei log e ref degli snapshot; non dipendere da timestamp
   e PID.
- Introdurre nel JobStore condiviso un admission control transazionale:
  - limite globale iniziale: 2 task Pi attivi;
  - un solo task attivo per progetto;
  - conteggio e creazione del job nella stessa transazione BEGIN IMMEDIATE.
- Oltre il limite, restituire un errore retryable esplicito invece di creare una
   coda illimitata.
- Per un progetto occupato, restituire PROJECT_BUSY.
- Applicare la stessa ownership per progetto alle operazioni Git coordinate dal
   fix 3.
- Non modificare flag -a, sandbox, permessi del processo o fallback
   dell’approvatore.

 Perché è importante

 Il carico previsto comprende cinque task simultanei. Senza backpressure condiviso,
 più processi possono avviare lavori pesanti e modificare lo stesso progetto; nomi
 basati sul secondo perdono log e recovery point.

 Cosa risolve

 Elimina le collisioni di output e snapshot, limita il carico anche tra processi
 diversi e impedisce modifiche Pi concorrenti sullo stesso progetto.

 Accettazione

 Con processi e provider simulati:

- Cinque richieste non producono mai più di due task attivi.
- Le richieste rifiutate hanno un errore chiaro, non un falso successo.
- Lo stesso progetto non esegue due task contemporaneamente.
- Ogni output lungo produce un file distinto.
- Due snapshot nello stesso secondo e nello stesso PID mantengono ref distinti e
   contenuti recuperabili.
- Non viene avviato alcun modello reale.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 11 — Implementare la consegna reale dei reminder

 Azione

- Avviare un singolo loop di reminder nel percorso main()/serve, usando un thread
   supervisionato e uno Event di stop; nessun secondo poller Hermes.
- Rendere obbligatorio il sender: sender_func=None non può più produrre delivered.
- Introdurre un claim atomico prima dell’invio:
     1. scheduled → sending in transazione;
     2. invio fuori dalla transazione;
     3. stato finale delivered, delivery_failed o delivery_uncertain.
- All’avvio, trasformare eventuali record rimasti in sending in esito incerto,
   senza retry cieco.
- Collegare un sender che consegni al chat associato all’actor_id.
- Non riutilizzare direttamente il sender Pi attuale: sceglie il primo chat ID
   disponibile e restituisce soltanto un booleano, insufficiente per destinatario
   ed esito dei reminder.
- Aggiornare ADR 0014 con il nuovo stato transitorio.

 Perché è importante

 Un reminder ha valore solo se arriva al destinatario corretto e al momento
 richiesto. Inoltre, segnare delivered senza un invio equivale a un falso successo.

 Cosa risolve

 Aggiunge il percorso di consegna oggi assente, evita doppi invii tra dispatcher
 concorrenti e distingue correttamente consegna, fallimento ed esito incerto.

 Accettazione

- Un reminder che scade mentre il server è attivo viene consegnato una volta.
- Due dispatcher concorrenti producono un solo invio.
- Sender assente: nessuno stato delivered.
- Timeout dopo una richiesta potenzialmente inviata: delivery_uncertain, non retry
   automatico.
- Crash durante sending: riconciliazione come incerto all’avvio.
- Un solo smoke test Telegram reale, autorizzato e con contenuto di test, prima
   della chiusura dell’issue host.

 ────────────────────────────────────────────────────────────────────────────────

 Fase 4 — Limiti di risorsa e prestazioni

### Fix 12 — Limitare connessioni e download IMAP

 Azione

- Aggiungere un timeout esplicito alla connessione IMAP.
- Leggere RFC822.SIZE prima di scaricare il messaggio.
- Introdurre un limite documentato MAX_MESSAGE_BYTES, inizialmente 5 MiB.
- Restituire MESSAGE_TOO_LARGE senza eseguire (RFC822) completo oltre tale limite.
- Mantenere il limite sul corpo restituito come seconda protezione, non come
   sostituto del controllo sul download.

 Perché è importante

 Un server lento o un messaggio molto grande non deve poter bloccare
 indefinitamente il worker o consumare memoria prima che entrino in gioco i limiti
 sul testo.

 Cosa risolve

 Pone un limite temporale alla connessione e un limite ai dati scaricati,
 trasformando blocchi e picchi di memoria in errori controllati.

 Accettazione

- Una connessione simulata che non risponde termina entro il timeout.
- Un messaggio oltre soglia non viene scaricato integralmente.
- Un messaggio normale conserva parsing e troncamento del corpo.
- Nessuna connessione IMAP reale è richiesta nei test unitari.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 13 — Rendere il profilo completo, aggiornato ed efficiente

 Azione

- Restituire complete, truncated e skipped nel risultato di get_profile.
- Non scartare più silenziosamente errori e limiti.
- Sostituire il limite implicito delle prime 500 note con un indice in-memory dei
   metadati.
- Invalidare le singole entry tramite (mtime_ns, size), così le modifiche esterne
   da Obsidian vengono rilevate senza rileggere ogni file.
- Proteggere la cache con un lock, perché il server può servire richieste
   concorrenti.
- Continuare a leggere le preferenze esplicite fresh e applicare i limiti sui
   singoli file.

 Perché è importante

 Un profilo incompleto senza warning può far ignorare preferenze o conflitti.
 Rileggere centinaia di note a ogni richiesta rende inoltre il costo dipendente
 dalla dimensione del vault.

 Cosa risolve

 Elimina sia la scomparsa silenziosa delle note oltre il limite sia le riletture
 integrali dei file che non sono cambiati.

 Accettazione

- Una preferenza nella 501ª nota viene inclusa oppure il risultato dichiara
   esplicitamente l’incompletezza.
- Una modifica esterna appare nella chiamata successiva.
- Una seconda lettura senza modifiche non riapre tutti i file.
- Una nota malformata aumenta skipped e non svuota il resto del profilo.
- I test usano vault sintetici, mai il vault personale.

 ────────────────────────────────────────────────────────────────────────────────

 Fase 5 — Contratti, copertura reale e pulizia

### Fix 14 — Correggere annotazioni MCP e inventario

 Azione

- Marcare run_project_workflow come mutativo e potenzialmente open-world.
- Marcare coding_session_init come non read-only, perché può avviare proxy.
- Sostituire il test generico “tutti read-only tranne…” con aspettative esplicite
   per i tool con effetti.
- Rimuovere la colonna automatica che dichiara ogni tool “Implementato & Testato”.
- Generare le informazioni sugli effetti dalle annotazioni effettive, mantenendo
   separata la policy di conferma.
- Aggiornare la descrizione Pi soltanto con le proprietà realmente introdotte dal
   fix 10.
- Eliminare o allineare il numero hardcoded di tool nel README.

 Perché è importante

 Le annotazioni non sono una barriera di sicurezza, ma influenzano client e
 automazioni. Documentazione ottimistica porta sia gli utenti sia gli agent a
 concedere esecuzioni inappropriate.

 Cosa risolve

 Allinea il contratto pubblico agli effetti reali e impedisce che l’inventario
 affermi copertura, lock o assenza di effetti non dimostrati.

 Accettazione

- Le annotazioni dei tool mutativi corrispondono a una tabella esplicita nei test.
- L’inventario non introduce affermazioni generiche sullo stato dei test.
- README e inventario riportano lo stesso numero di tool.
- La generazione dell’inventario resta priva di effetti grazie al fix 8.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 15 — Portare i test sul codice e sulle integrazioni reali

 Azione — Hermes

- Ispezionare la versione Hermes effettivamente installata e la registrazione
   reale di hook e middleware.
- Sostituire i test che definiscono funzioni locali con un test di contratto
   dell’integrazione reale.
- Verificare registrazione, dispatch, selezione modello e iniezione del contesto
   sul host supportato.
- Se Hermes non è disponibile, lasciare l’attività host-dipendente aperta: non
   sostituirla con un altro mock.

 Azione — Travel

- Migrare i 45 test da travel_old.py a travel.py.
- Mantenere i test Pareto già rivolti alla produzione.
- Correggere il modulo reale solo dove le differenze rispetto al legacy violano il
   comportamento atteso; non copiare automaticamente il vecchio codice.
- Quando la suite reale passa, eliminare travel_old.py.

 Perché è importante

 Un test verde ha valore soltanto se esercita il codice o il contratto usato in
 produzione. Surrogati e moduli legacy possono nascondere regressioni reali.

 Cosa risolve

 Collega la suite agli effettivi percorsi Hermes e travel, elimina falsa copertura
 e rende possibile rimuovere circa 1.681 righe legacy.

 Accettazione

- Nessun test Hermes dichiara integrazione tramite hook definiti soltanto nel
   test.
- L’evidenza dell’integrazione reale è riportata sulla GitHub Issue.
- Tutti i test travel rilevanti importano jarvis_hermes.travel.
- Le chiamate provider restano simulate nei test ordinari.
- travel_old.py non è più referenziato né incluso nel package.

 ────────────────────────────────────────────────────────────────────────────────

### Fix 16 — Eliminare il codice email irraggiungibile

 Azione

- Cancellare il blocco successivo al return di stage_email_send().
- Conservare reconcile_email_status() come unica implementazione.
- Non modificare il comportamento della riconciliazione.

 Perché è importante

 Il codice morto aumenta il rischio che una correzione futura venga applicata alla
 copia sbagliata e rende più difficile comprendere il flusso reale.

 Cosa risolve

 Elimina una duplicazione irraggiungibile e lascia un solo punto in cui mantenere
 la logica di riconciliazione.

 Accettazione

- Il blocco dopo il return non esiste più.
- Test email e compilazione passano senza cambiamenti di comportamento.

 ────────────────────────────────────────────────────────────────────────────────

 Gate finali

 Ogni fase viene chiusa soltanto con i test mirati verdi. Prima del merge finale:

 ```bash
   uv sync --locked
   uv run python -m unittest discover -s tests -v
   uv run jarvis doctor --json
 ```

 Servono inoltre:

- soak test con cinque task Pi simulati, senza processi residui o collisioni;
- matrice MCP con elicitation disponibile/assente e versioni/hash stale;
- fault-injection per backup, IMAP e processi;
- accettazione Windows reale per GUI e process tree;
- accettazione Hermes reale per il fix 15;
- un solo reminder Telegram reale autorizzato per il fix 11;
- nessuna nuova dipendenza prevista;
- nessuna modifica a credenziali, vault personale, database runtime o screenshot;
- nessuna chiusura delle issue host-dipendenti basata soltanto sui mock.

 Non ho modificato codice in questa fase. Il rischio del fix 6 rimane
 deliberatamente presente: la working directory di Pi continuerà a non essere un
 sandbox del filesystem o della rete.
