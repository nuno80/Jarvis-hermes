# Roadmap JARVIS V1

30 issue pubblicate da `to-tickets`, dopo approvazione dell’utente. Questo indice non duplica lo stato corrente: leggere le issue su GitHub.

La label `ready-for-agent` indica task revisionato; iniziare soltanto quando i blocker sono conclusi. Dipendenze in testo nelle issue, perché il connettore disponibile non espone relazioni native di blocco.

Subito avviabili: #1 (diagnostica, parzialmente predisposta) e #20 (verifica provider travel). La prova completa sul PC non è stata eseguita; nessuna issue è stata chiusa automaticamente.

| Task | Issue | Bloccato da |
|---|---|---|
| JARVIS-01 | [Diagnostica locale riproducibile](https://github.com/nuno80/Jarvis-hermes/issues/1) | Nessuno |
| JARVIS-02 | [Stato disco da Telegram attraverso MCP](https://github.com/nuno80/Jarvis-hermes/issues/2) | [#1](https://github.com/nuno80/Jarvis-hermes/issues/1) |
| JARVIS-03 | [Conferma monouso di una azione simulata](https://github.com/nuno80/Jarvis-hermes/issues/3) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-04 | [Consultare e annullare un lavoro lungo](https://github.com/nuno80/Jarvis-hermes/issues/4) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3) |
| JARVIS-05 | [Leggere log e progetti Windows o WSL](https://github.com/nuno80/Jarvis-hermes/issues/5) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-06 | [Modificare e ripristinare un file senza perdere lavoro](https://github.com/nuno80/Jarvis-hermes/issues/6) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3), [#5](https://github.com/nuno80/Jarvis-hermes/issues/5) |
| JARVIS-07 | [Eseguire un workflow di progetto e creare un commit](https://github.com/nuno80/Jarvis-hermes/issues/7) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#6](https://github.com/nuno80/Jarvis-hermes/issues/6) |
| JARVIS-08 | [Push del commit esatto approvato da telefono](https://github.com/nuno80/Jarvis-hermes/issues/8) | [#7](https://github.com/nuno80/Jarvis-hermes/issues/7) |
| JARVIS-09 | [Comando amministrativo avanzato con policy sugli effetti](https://github.com/nuno80/Jarvis-hermes/issues/9) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#6](https://github.com/nuno80/Jarvis-hermes/issues/6) |
| JARVIS-10 | [Usare una applicazione Windows tramite GUI](https://github.com/nuno80/Jarvis-hermes/issues/10) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3), [#5](https://github.com/nuno80/Jarvis-hermes/issues/5) |
| JARVIS-11 | [Interagire con una pagina web con consenso sugli invii](https://github.com/nuno80/Jarvis-hermes/issues/11) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3) |
| JARVIS-12 | [Cercare e leggere note nel vault configurato](https://github.com/nuno80/Jarvis-hermes/issues/12) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-13 | [Applicare subito una preferenza esplicita modificata](https://github.com/nuno80/Jarvis-hermes/issues/13) | [#12](https://github.com/nuno80/Jarvis-hermes/issues/12) |
| JARVIS-14 | [Imparare e correggere una memoria con evidenza](https://github.com/nuno80/Jarvis-hermes/issues/14) | [#6](https://github.com/nuno80/Jarvis-hermes/issues/6), [#13](https://github.com/nuno80/Jarvis-hermes/issues/13) |
| JARVIS-15 | [Usare Gemini configurato e registrare consumo per job](https://github.com/nuno80/Jarvis-hermes/issues/15) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-16 | [Instradare con Jev e fallback conservativo](https://github.com/nuno80/Jarvis-hermes/issues/16) | [#15](https://github.com/nuno80/Jarvis-hermes/issues/15) |
| JARVIS-17 | [Eseguire un task semplice su modello locale](https://github.com/nuno80/Jarvis-hermes/issues/17) | [#15](https://github.com/nuno80/Jarvis-hermes/issues/15) |
| JARVIS-18 | [Inviare un vocale Telegram e ottenere il risultato](https://github.com/nuno80/Jarvis-hermes/issues/18) | [#15](https://github.com/nuno80/Jarvis-hermes/issues/15) |
| JARVIS-19 | [Rispondere a una ricerca web con fonti verificabili](https://github.com/nuno80/Jarvis-hermes/issues/19) | [#15](https://github.com/nuno80/Jarvis-hermes/issues/15) |
| JARVIS-20 | [Validare un provider travel con una offerta live](https://github.com/nuno80/Jarvis-hermes/issues/20) | Nessuno |
| JARVIS-21 | [Cercare date flessibili con budget e cache](https://github.com/nuno80/Jarvis-hermes/issues/21) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#13](https://github.com/nuno80/Jarvis-hermes/issues/13), [#20](https://github.com/nuno80/Jarvis-hermes/issues/20) |
| JARVIS-22 | [Confrontare offerte e spiegare le alternative](https://github.com/nuno80/Jarvis-hermes/issues/22) | [#15](https://github.com/nuno80/Jarvis-hermes/issues/15), [#21](https://github.com/nuno80/Jarvis-hermes/issues/21) |
| JARVIS-23 | [Leggere agenda e preparare un evento](https://github.com/nuno80/Jarvis-hermes/issues/23) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-24 | [Creare un evento approvato senza duplicati](https://github.com/nuno80/Jarvis-hermes/issues/24) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3), [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#23](https://github.com/nuno80/Jarvis-hermes/issues/23) |
| JARVIS-25 | [Leggere email e preparare una risposta](https://github.com/nuno80/Jarvis-hermes/issues/25) | [#2](https://github.com/nuno80/Jarvis-hermes/issues/2) |
| JARVIS-26 | [Inviare la risposta email approvata](https://github.com/nuno80/Jarvis-hermes/issues/26) | [#3](https://github.com/nuno80/Jarvis-hermes/issues/3), [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#25](https://github.com/nuno80/Jarvis-hermes/issues/25) |
| JARVIS-27 | [Consegnare un reminder esplicito dopo un riavvio](https://github.com/nuno80/Jarvis-hermes/issues/27) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4) |
| JARVIS-28 | [Ripartire automaticamente e verificare lo stato del nodo](https://github.com/nuno80/Jarvis-hermes/issues/28) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#9](https://github.com/nuno80/Jarvis-hermes/issues/9), [#10](https://github.com/nuno80/Jarvis-hermes/issues/10) |
| JARVIS-29 | [Ripristinare dati operativi e vault da backup](https://github.com/nuno80/Jarvis-hermes/issues/29) | [#4](https://github.com/nuno80/Jarvis-hermes/issues/4), [#14](https://github.com/nuno80/Jarvis-hermes/issues/14) |
| JARVIS-30 | [Verificare il rilascio V1 completo](https://github.com/nuno80/Jarvis-hermes/issues/30) | [#8](https://github.com/nuno80/Jarvis-hermes/issues/8), [#11](https://github.com/nuno80/Jarvis-hermes/issues/11), [#14](https://github.com/nuno80/Jarvis-hermes/issues/14), [#16](https://github.com/nuno80/Jarvis-hermes/issues/16), [#17](https://github.com/nuno80/Jarvis-hermes/issues/17), [#18](https://github.com/nuno80/Jarvis-hermes/issues/18), [#19](https://github.com/nuno80/Jarvis-hermes/issues/19), [#22](https://github.com/nuno80/Jarvis-hermes/issues/22), [#24](https://github.com/nuno80/Jarvis-hermes/issues/24), [#26](https://github.com/nuno80/Jarvis-hermes/issues/26), [#27](https://github.com/nuno80/Jarvis-hermes/issues/27), [#28](https://github.com/nuno80/Jarvis-hermes/issues/28), [#29](https://github.com/nuno80/Jarvis-hermes/issues/29) |
| JARVIS-31 | [Installare, configurare ed iniettare Jev in Hermes](https://github.com/nuno80/Jarvis-hermes/issues/31) | [#16](https://github.com/nuno80/Jarvis-hermes/issues/16), [#17](https://github.com/nuno80/Jarvis-hermes/issues/17) |
| JARVIS-35 | [run_command senza shell=True e scritture confinate](https://github.com/nuno80/Jarvis-hermes/issues/37) | Nessuno |
| JARVIS-37 | [Punti di estensione Hermes e pre-turn injection](https://github.com/nuno80/Jarvis-hermes/issues/35) | Nessuno |

## Sequenza utile

1. Diagnostica e Telegram/MCP (#1–#2).
2. Consenso e job persistenti (#3–#4), poi PC/dev e memoria in parallelo.
3. Routing, vocali e ricerca web; travel dopo disponibilità del provider.
4. Agenda/email e reminder espliciti.
5. Autostart, restore e verifica complessiva (#28–#30).

Il bot @nuno_agent_bot esiste già: la #2 verifica l’installazione reale prima di modificarla.

## Incremento MCP remoto

#2 e #12: implementate le parti server stdio e lettura vault con test di protocollo su directory temporanee. Restano aperte: collegamento Hermes/Telegram, allowlist e verifica sul PC dell’utente. Nessun blocker end-to-end è considerato risolto dal solo test sul runner.
