# Guida Operativa: Backup e Restore di JARVIS (Issue #29 / J18)

Questa guida illustra la procedura operativa per il backup coerente dei dati operativi e del vault di JARVIS, il restore in un ambiente pulito, la politica di retention e la separazione dei segreti.

---

## 1. Principi e Confini di Sicurezza (D08, D14, B3)

1. **Snapshot Coerente dei Dati Runtime:**
   - I database SQLite (`jobs.sqlite3`, `approvals.sqlite3`, `reminders.sqlite3`, `routing_decisions.sqlite3`, `verifications.sqlite3`, ecc.) vengono acquisiti in maniera transazionalmente coerente (usando `VACUUM INTO` / SQLite backup API) per evitare corruzioni da scritture concorrenti o WAL non sincronizzati.
   - I checkpoint di lavoro e gli audit log sono archiviati preservando i relativi digest SHA-256.

2. **Conservazione e Ricostruzione del Vault:**
   - Note Markdown (`.md`) e preferenze esplicite (`preferences.yaml`) sono archiviate.
   - Al momento del restore in una directory pulita, l'indice dei metadati e il profilo utente vengono ricostruiti senza richiedere una seconda copia duplicata dello storage.

3. **Separazione Rigorosa dei Segreti:**
   - `.env`, chiavi SSH, certificati, token e credenziali **NON vengono mai inclusi** nei backup archiviati.
   - I segreti devono essere gestiti e ripristinati separatamente tramite il gestore chiavi sicuro dell'ambiente host.

4. **Neutralizzazione di Sicurezza al Restore:**
   - **Job interrotti:** qualsiasi job non terminato (`running`, `received`, `waiting_approval`) viene marcato `outcome_unknown` con avviso nel log. Non viene rieseguito automaticamente alla cieca per evitare effetti collaterali indesiderati.
   - **Approvazioni pendenti:** eventuali consensi o token di approvazione pendenti registrati nel database vengono cancellati (`cancelled`), impedendone il consumo accidentale dopo il ripristino.

---

## 2. Creazione di un Backup

Per creare un backup completo di runtime e vault:

```bash
uv run jarvis backup --label manual-snap --retention-days 14
```

Output:
```text
Backup created: jarvis_backup_20261017_120000Z_manual-snap
Archive: ~/.local/state/jarvis-hermes/backups/jarvis_backup_20261017_120000Z_manual-snap.tar.gz
Manifest: ~/.local/state/jarvis-hermes/backups/jarvis_backup_20261017_120000Z_manual-snap_manifest.json
Files archived: 12 (secrets excluded: 2)
```

In formato JSON (per automazione o script):
```bash
uv run jarvis backup --json
```

---

## 3. Ripristino in un Ambiente Pulito

Per eseguire il restore da un archivio di backup in una cartella pulita:

```bash
uv run jarvis restore <percorso-archivio.tar.gz> \
  --target-vault /path/to/clean_vault \
  --target-state /path/to/clean_state
```

> **Avvertenza di Sicurezza:**
> Il comando rifiuta di sovrascrivere una cartella di vault esistente non vuota, a meno che non venga specificato esplicitamente il flag `--allow-existing`.

---

## 4. Dimostrazione Automatica Isolata

È disponibile un comando per verificare l'intero ciclo in un ambiente temporaneo e isolato (senza toccare né sovrascrivere il vault o il runtime reale):

```bash
uv run jarvis backup-demo
```
oppure con `--json`:
```bash
uv run jarvis backup-demo --json
```

La demo verifica:
- Creazione snapshot e archivio compresso.
- Esclusione comprovata dei file di segreti (`.env`).
- Restore in directory pulita.
- Neutralizzazione di job pendenti e approvazioni.
- Ricostruzione dell'indice delle note e verifica delle preferenze utente (`preferences.yaml`).

---

## 5. Politica di Retention e Cancellazione

- Ogni backup è accompagnato da un file `_manifest.json` che descrive la retention applicabile (default: 14 giorni), la lista e l'hash di ogni file contenuto, e l'elenco dei segreti esclusi.
- La cancellazione e la pulizia dei backup scaduti è gestita dal metodo `prune_backups()` di `BackupManager`, che rimuove in modo sicuro archivi e manifest che hanno superato il periodo di retention stabilito.
