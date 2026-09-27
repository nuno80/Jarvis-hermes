# Elenco issue aperte — ordine e regole di lavoro

> Documento operativo. L'ordine da seguire è quello delle **onde** sotto
> (dipendenze "Blocked by", non numero issue). Stato issue: solo su GitHub
> (`gh issue view N`); questo file non è un tracker.

## Passo 0 — prepara il worktree (obbligatorio, prima di tutto)

Ogni issue lavora in un worktree dedicato, creato dallo script idempotente:

```bash
./scripts/issue-worktree.sh N   # es. ./scripts/issue-worktree.sh 31
cd ../Jarvis-hermes-N
```

Lo script crea (o riusa se esiste) `../Jarvis-hermes-N` con branch
esattamente `feat/issue-N` (da `origin/main` fresco, o dal branch esistente
locale/remoto). Lancia l'agent **già dentro** quel worktree, così il vincolo
"opera solo qui" è fisico, non solo scritto. Per rimuovere a fine lavoro
(dopo il merge del coordinatore): `git worktree remove ../Jarvis-hermes-N`.

## Contesto che devi conoscere

- Repo `nuno80/Jarvis-hermes`, branch principale `main`, remote `origin`.
- Checkout principale: `/home/nuno/programmazione/Jarvis-hermes`.
- **Agent multipli in parallelo da terminali diversi sulla stessa macchina**:
  ognuno lavora in un worktree dedicato `../Jarvis-hermes-N`
  (es. issue 31 → `/home/nuno/programmazione/Jarvis-hermes-31`).
- Stile merge del repo: merge commit con `--no-ff` su main, PR off.

## Onde di esecuzione

Onda 1 — libere subito, tutte indipendenti tra loro (parallelizzabili):
#18, #19, #20, #23, #25, #27, #28, #29, #31, #39.
Priorità suggerita: #31 prima (sblocca il riscontro immediato di #18),
poi #18/#19, #20 (sblocca la catena travel), #23/#25 (sbloccano #24/#26),
#27/#28/#29, #39 in qualsiasi momento.

Onda 2 (dopo i merge dell'onda 1 indicati):
- #21 dopo #20
- #24 dopo #23
- #26 dopo #25

Onda 3: #22 dopo #21.

Onda 4, ultima: #30 (dipende da quasi tutto).

Dettaglio dipendenze (sezione "Blocked by" di ogni issue, autorevole):

| Issue | Bloccata da (tutte chiuse salvo indicato) |
|---|---|
| #18 vocale Telegram | #15 (chiusa); riscontro immediato usa #31 se disponibile |
| #19 ricerca web | #15 (chiusa) |
| #20 provider travel | nessuna — subito |
| #21 date flessibili | #4, #13 (chiuse), **#20 (onda 1)** |
| #22 confronto offerte | #15 (chiusa), **#21 (onda 2)** |
| #23 agenda lettura | #2 (chiusa) |
| #24 evento approvato | #3, #4 (chiuse), **#23 (onda 1)** |
| #25 email lettura | #2 (chiusa) |
| #26 email invio | #3, #4 (chiuse), **#25 (onda 1)** |
| #27 reminder | #4 (chiusa) |
| #28 autostart | #4, #9, #10 (chiuse) |
| #29 backup/restore | #4, #14 (chiuse) |
| #31 System 1 pre-turno | #37, #17, #38 (chiuse) |
| #39 allineare docs | nessuna — subito |
| #30 rilascio V1 | tutto il resto |

Nota: #30 cita "#40 — Documentazione allineata", ma la #40 reale è
"Dataset e calibrazione" (chiusa); la docs è la #39. Correggere il
riferimento chiudendo la #39.

## Regole vincolanti per l'agent (anti-conflitto)

1. Opera SOLO nel tuo worktree `../Jarvis-hermes-N`, branch esattamente
   `feat/issue-N` (senza slug: un agent = una issue = un branch). Verifica all'avvio: `git status` pulito da lavoro altrui,
   `gh issue view N` con issue OPEN e Blocker chiusi — altrimenti fermati.
2. NON cambiare directory, NON toccare la checkout main né i worktree altrui.
   Mai `git checkout main`, `git worktree add/remove`, branch di altri.
3. Prima del codice leggi: `AGENTS.md`, `CONTEXT.md`,
   `docs/specs/jarvis-v1.md`, l'ADR pertinente in `docs/adr/`.
4. Prima del push: `git fetch origin` + `git rebase origin/main` + verifiche
   di `AGENTS.md` (`uv sync --locked`, `uv run python -m unittest discover -s tests -v`,
   `uv run jarvis doctor --json`). Se il rebase dà conflitti, fermati e riportali.
5. Push SOLO del tuo branch (`git push -u origin HEAD`). Mai push di main.
   Mai `--force` (solo `--force-with-lease` sul tuo branch dopo tuo rebase).
6. NON mergiare su main: il merge lo fa il coordinatore uno alla volta.
   Finisci con branch pushato e pulito, e commento alla issue con evidenze
   (comandi + esiti misurati, SHA dell'head). NON chiudere checkbox non dimostrati.
7. Mai committare: credenziali, contenuti del vault, DB runtime,
   screenshot, file in `results/`.
8. In chiusura rispondi con: stato issue, SHA del branch, scostamenti dall'atteso.

## Letture preliminari per issue (per risparmiare un giro)

- #31: ADR 0005 (hook pre-turno) e 0007 (modello 4b), `src/jarvis_hermes/router.py`,
  `config/routing_thresholds.json`, `tests/data/routing_it.jsonl` (su main).
- #18: #31 se mergiata (riscontro immediato), altrimenti messaggio deterministico.
- #20/#21/#22: appendice E della spec, ADR sul provider se esiste.
- #23/#24/#25/#26: sezione 6 e appendice C (conferme, D14).
- #39: `git log origin/main --oneline -15` per lo stato reale del server.
