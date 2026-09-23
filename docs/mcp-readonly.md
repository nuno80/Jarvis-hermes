# MCP locale in sola lettura

Incremento per #2 e #12. Espone `device_status`, `disk_usage`, `search_notes`, `read_note` attraverso il protocollo MCP stdio. Il client MCP avvia il processo; stdout è riservato al protocollo. Nessun endpoint HTTP, secondo gateway Telegram o comando remoto è avviato.

## Avvio e configurazione

```bash
uv sync --locked
uv run jarvis serve
```

L'avvio manuale resta in ascolto su stdin: è normale che non stampi una risposta. Per uso reale il client deve avviarlo come sottoprocesso. Dopo aver verificato la versione e il profilo Hermes già in uso, aggiungere un server MCP con:

- command: percorso assoluto all'interprete Python della `.venv` di questo progetto;
- args: `-m`, `jarvis_hermes`, `serve`;
- env `JARVIS_DEVICE_ID`: identificatore locale, default `local`;
- env `JARVIS_VAULT_PATH`: percorso assoluto del vault, nello stesso ambiente del processo.

I parametri sono una descrizione del collegamento, non un file Hermes da sostituire alla cieca. Se Hermes gira su Windows e il server in WSL, prima validare il bridge fra i due ambienti. Nessuna modifica alla configurazione o al token del bot esistente è stata effettuata.

`device_id` nei tool macchina deve corrispondere al valore configurato. Il server descrive solo il proprio host; `disk_usage` riguarda il filesystem della directory di lavoro, non tutti i dischi Windows.

## Contratti e limiti

Ogni risultato applicativo contiene `ok`, `data`, `error`, `schema_version`, `request_id`, `device_id`, `observed_at` e provenienza. Un errore applicativo ha `ok=false`; errori di protocollo/schema sono gestiti dal SDK. `request_id` è un identificatore di correlazione, non credenziale o autorizzazione.

Note visibili `.md`, UTF-8, entro 256 KiB; risposta paginata in massimo 8.000 caratteri con `next_offset` e versione SHA-256. Il testo restituito normalizza i fine riga a LF, mentre SHA-256 identifica i byte originali; il file non viene riscritto. Ogni lettura è fresca; una modifica manuale nella stessa sessione cambia il contenuto e la versione restituiti. Non c'è un indice persistente da sincronizzare.

Ricerca case-insensitive per sottostringa su massimo 500 note e 5.000 voci di directory, 20 risultati per richiesta. `truncated` segnala una copertura limitata o ulteriori risultati; `skipped_entries` segnala note/directory non leggibili. Nessuna ricerca semantica, modifica o apprendimento delle preferenze è implementato qui.

File/cartelle nascosti, symlink, junction, hardlink, file non regolari e percorsi esterni sono esclusi. Nessun percorso assoluto compare nei risultati. Il contenuto delle note è marcato non fidato: non può concedere permessi. Questi controlli applicativi assumono un filesystem locale sotto controllo del proprietario, senza processi ostili che sostituiscono simultaneamente le directory; non sono un sandbox OS contro race su cartelle.

La fiducia è nel processo che avvia il server e nella configurazione locale. Il server non autentica utenti Telegram: l'allowlist e il flusso completo del gateway restano da verificare in #2. Non esporre stdio tramite un bridge remoto non autenticato.

## Verifica

`uv run python -m unittest discover -s tests -v` avvia un vero client MCP e un sottoprocesso server, usa soltanto vault temporanei e prova catalogo, status/disco, errori, letture fresche, paginazione, ricerca oltre la prima pagina, traversal, file nascosti, link e limiti dimensionali. Un test symlink può risultare skipped se l'OS non concede tale capacità all'account: leggere il riepilogo CI.

Il test sul runner non dimostra il collegamento a @nuno_agent_bot, né l'accesso al vault reale. #2 e #12 restano aperte fino alle prove end-to-end richieste.
