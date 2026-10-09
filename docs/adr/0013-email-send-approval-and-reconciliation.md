# ADR 0013: Invio Email protetto con consenso monouso ed esito verificato per J16 / Issue #26

- **Stato:** Accettato (2026-10-09)
- **Autore:** Jarvis Agent / nuno80
- **Tracciabilità specifica:** J16; D07; AT04; Issue #26 (bloccata da #3, #4, #25)

## Contesto

La specifica J16 (§2 priorità 5, §5 `personal-mcp`, §12.3) e la decisione architetturale D07 richiedono che l'invio di email sia un'azione protetta:
1. Il proprietario deve approvare destinatari esatti (`to`, `cc`, `bcc`), oggetto, corpo e allegati.
2. Niente invio su consenso scaduto o rifiutato.
3. Message ID registrato ed esito verificato; callback duplicate gestite senza doppio invio.
4. In caso di riavvio o timeout durante la trasmissione esterna (AT04), produrre riconciliazione o `OUTCOME_UNKNOWN`, mai un retry cieco.
5. Nessun segreto (password applicativa o token) nei log di audit.

## Decisione

1. **Protocollo SMTP via Standard Library (`smtplib` ed `email.message`)**:
   - Connessione SMTP sicura su porta 465 (SSL) o 587 (STARTTLS).
   - Supporto nativo per Yahoo Mail (`smtp.mail.yahoo.com:465`) o server configurabile (`JARVIS_EMAIL_SMTP_HOST`, `JARVIS_EMAIL_SMTP_PORT`).
   - Credenziali lette esclusivamente dalle variabili d'ambiente protette (`YAHOO_EMAIL` / `JARVIS_EMAIL_USER`, `YAHOO_APP_PASSWORD` / `JARVIS_EMAIL_PASSWORD`).

2. **Digest crittografico SHA-256 e gate monouso (`ApprovalStore`)**:
   - Calcolo deterministico di `compute_email_digest` su `to`, `cc`, `bcc`, `subject`, `body` e metadati normalizzati degli allegati (`filename`, `sha256`, `size_bytes`).
   - Il target di approvazione è vincolato a `email_send:<destinatari>` e gli argomenti includono il digest esatto.
   - Fail-closed: se l'utente rifiuta, il token scade o uno qualsiasi dei campi viene modificato, l'invio fallisce con eccezione tipizzata (`APPROVAL_DENIED`, `APPROVAL_EXPIRED`, `APPROVAL_DECLINED`) e la rete non viene toccata.

3. **Audit Log SQLite permanente e Idempotenza (AT04)**:
   - Registro permanente `sent_emails` in SQLite locale (`$XDG_STATE_HOME/jarvis-hermes/email_audit.sqlite3`).
   - Registrazione di `message_id`, mittente, destinatari, oggetto, digest, stato (`SENT`, `OUTCOME_UNKNOWN`) e timestamp.
   - Prima dell'invio, verifica la presenza del `message_id`: in caso di callback duplicata restituisce `status: "ALREADY_SENT"`, `reconciled: True`, senza richiamare il server SMTP.
   - Se la connessione cade o si verifica un timeout durante l'invio SMTP, l'operazione fallisce con `OUTCOME_UNKNOWN` e registra lo stato incerto nell'audit log senza alcun retry automatico.
   - Nuovo tool di lettura `reconcile_email(message_id)` per consultare lo stato senza rischiare doppi invii.

4. **Assenza di segreti in audit**:
   - Nessun campo password o chiave applicativa è presente nella tabella di audit né nei log o payload restituiti all'MCP.

## Conseguenze

- Modulo `src/jarvis_hermes/email.py` arricchito con `SmtpEmailSender`, `EmailAuditLog`, `compute_email_digest`, `send_email`, `stage_email_send` e `reconcile_email_status`.
- Tool MCP registrati: `email_send` (con annotazione `destructive` e workflow di elicitazione) e `reconcile_email` (`readonly`).
- CLI: estesa `jarvis email-demo` per dimostrare la verifica locale dell'invio protetto con approval token e digest.
- Test: 20 test unitari in `test_email.py` e test integrato in `test_mcp.py`.
