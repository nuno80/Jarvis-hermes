# ADR 0003: Prima integrazione MCP locale in sola lettura

Stato: adottato nell'incremento remoto; integrazione Hermes da validare sull'host.

SDK ufficiale Python `mcp>=1.28,<2`, risolto a 1.30.0 nel lockfile. La linea 1.x è una scelta esplicita per FastMCP e ClientSession; non dichiariamo che sia la major più recente. La documentazione ufficiale v1.x guida questa integrazione. Un aggiornamento major richiederà test e revisione del contratto.

Un solo processo stdio espone quattro tool in sola lettura. Proviamo il protocollo con un client SDK reale senza dipendere da Telegram o da credenziali. I blocker host-dependent delle issue restano validi per la loro chiusura; questo incremento prepara e verifica le parti eseguibili da remoto.

Il vault è un percorso esterno autorizzato dalla configurazione del processo, non un argomento del modello. Nessuna memoria personale viene clonata o copiata nel repo. Ricerca live bounded e paginazione precedono un eventuale indice; si espone copertura parziale esplicita.

Limite: non è un servizio multiutente e non garantisce isolamento da processi ostili sullo stesso filesystem. Conferme, scritture, routing, identità Telegram e nodi remoti non sono implementati in questa slice.

Fonte: https://github.com/modelcontextprotocol/python-sdk/tree/v1.x
