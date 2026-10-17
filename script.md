# Guida rapida: Git Worktree per Issue e Agenti Paralleli

Questo flusso permette di lavorare su più issue in parallelo senza sporcare la cartella principale del progetto e senza conflitti di dipendenze.

Gli script globali si trovano in `/home/nuno/script` (già nel `$PATH` di WSL) e possono essere invocati con i comandi **`wt-new`** e **`wt-end`** da qualsiasi terminale.

---

## 1. Creare un nuovo worktree (`wt-new`)

Dalla root del repository principale (o da qualsiasi sua sotto-cartella):

```bash
wt-new 42
# oppure in modalità interattiva:
wt-new
```

### Cosa fa in automatico

1. Esegue `git fetch origin` per avere i riferimenti remoti aggiornati.
2. Crea una cartella dedicata in `../<repo>-worktrees/issue-42`.
3. Crea e aggancia il branch `feat/issue-42` partendo da `origin/main` (o lo aggancia se esiste già).
4. Copia tutti i file `.env*` dalla root alla cartella del worktree (evitando problemi di configurazione).
5. Rileva il gestore pacchetti e installa le dipendenze:
   - Python: esegue `uv sync --locked`
   - Node.js: rileva `pnpm`, `yarn`, `bun` o `npm` ed esegue l'install
6. **Apre automaticamente una nuova finestra di VS Code** puntata sul worktree (`code .`).
7. Ti posiziona direttamente dentro la cartella del worktree con una nuova shell pronta all'uso.

---

## 2. Lavorare all'interno del worktree

Dentro la cartella del worktree (es. `~/programmazione/Jarvis-hermes-worktrees/issue-42`):

- Lancia i tuoi test, avvia l'agente o fai modifiche con VS Code.
- Fai i tuoi commit normalmente su `feat/issue-42`:

  ```bash
  git add .
  git commit -m "feat: implementa ..."
  ```

---

## 3. Concludere e chiudere il worktree (`wt-end`)

Quando hai terminato e tutti i file sono committati, lancia **da dentro la cartella del worktree**:

```bash
wt-end
```

### Cosa fa in automatico

1. Verifica che non ci siano modifiche dimenticate o non tracciate.
2. Si sposta nella cartella principale del repository.
3. Sincronizza `main` con `origin/main`.
4. Fa il merge del branch (`feat/issue-42`) dentro `main` con `--no-ff`.
5. Esegue il `git push origin main`.
6. Rimuove la cartella del worktree (pulendo anche i file `.env`).
7. Cancella il branch locale (e quello remoto su GitHub se era stato pushato).
8. Ti riposiziona con la shell nella cartella principale.

---

## Script di riferimento

- Comandi globali: `/home/nuno/script/wt-new` e `/home/nuno/script/wt-end`
- Copia nel repo: `scripts/setup-worktree.sh` e `scripts/wt-end.sh`
