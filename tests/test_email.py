"""Unit tests for Email read + local reply draft (issue #25 / JARVIS-25, J16)."""
import email.message
import os
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import MagicMock, patch

from jarvis_hermes.approval import ApprovalStore
from jarvis_hermes.email import (
    EmailAuditLog,
    EmailError,
    EmailManager,
    EmailMessage,
    EmailSearchRequest,
    ImapEmailProvider,
    SmtpEmailSender,
    compute_email_digest,
    normalize_subject,
    parse_email_bytes,
)


class EmailValidationTests(unittest.TestCase):
    def test_invalid_max_results(self):
        for bad in [0, 51, "many"]:
            with self.assertRaises(EmailError) as ctx:
                EmailSearchRequest.from_params({"max_results": bad})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_invalid_folder_name(self):
        for bad in ["../inbox", "inbox\r\n", ""]:
            with self.subTest(bad=bad):
                with self.assertRaises(EmailError) as ctx:
                    EmailSearchRequest.from_params({"folder": bad})
                self.assertEqual(ctx.exception.code, "INVALID_FOLDER")

    def test_normalize_subject(self):
        self.assertEqual(normalize_subject("Hello"), "Re: Hello")
        self.assertEqual(normalize_subject("Re: Hello"), "Re: Hello")
        self.assertEqual(normalize_subject("RE:  re: Hello"), "Re: Hello")
        self.assertEqual(normalize_subject(""), "Re:")


class FailClosedTests(unittest.TestCase):
    def test_no_credentials_no_network(self):
        with patch.dict(os.environ, {}, clear=True):
            mgr = EmailManager()
            with patch("imaplib.IMAP4_SSL") as mock_imap:
                with self.assertRaises(EmailError) as ctx:
                    mgr.search_emails({})
                self.assertEqual(ctx.exception.code, "PROVIDER_NOT_CONFIGURED")
                self.assertIn("YAHOO_EMAIL", ctx.exception.message)
                mock_imap.assert_not_called()

    def test_auth_error_fails_closed(self):
        provider = ImapEmailProvider(
            user="test@yahoo.com",
            password="wrong_password",
            host="imap.mail.yahoo.com",
        )
        mgr = EmailManager(provider=provider)
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls:
            mock_client = MagicMock()
            mock_imap_cls.return_value = mock_client
            import imaplib
            mock_client.login.side_effect = imaplib.IMAP4.error("AUTHENTICATIONFAILED Invalid credentials")
            with self.assertRaises(EmailError) as ctx:
                mgr.search_emails({})
            self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_ERROR")
            self.assertFalse(ctx.exception.retryable)

    def test_connection_error_is_retryable(self):
        provider = ImapEmailProvider(
            user="test@yahoo.com",
            password="secret_password",
            host="imap.mail.yahoo.com",
        )
        mgr = EmailManager(provider=provider)
        with patch("imaplib.IMAP4_SSL") as mock_imap_cls:
            mock_imap_cls.side_effect = OSError("Connection refused")
            with self.assertRaises(EmailError) as ctx:
                mgr.search_emails({})
            self.assertEqual(ctx.exception.code, "PROVIDER_UNAVAILABLE")
            self.assertTrue(ctx.exception.retryable)


class EmailParsingTests(unittest.TestCase):
    def test_parse_email_bytes_plain(self):
        raw_msg = (
            b"From: Alice <alice@example.com>\r\n"
            b"To: Bob <bob@yahoo.com>\r\n"
            b"Subject: Meeting tomorrow\r\n"
            b"Date: Wed, 08 Oct 2026 14:00:00 +0200\r\n"
            b"Message-ID: <msg123@example.com>\r\n"
            b"Content-Type: text/plain; charset=utf-8\r\n"
            b"\r\n"
            b"Hi Bob,\r\nLet's meet tomorrow at 10am.\r\n"
        )
        parsed = parse_email_bytes("1", raw_msg)
        self.assertEqual(parsed.uid, "1")
        self.assertEqual(parsed.subject, "Meeting tomorrow")
        self.assertEqual(parsed.from_address, "Alice <alice@example.com>")
        self.assertEqual(parsed.to_addresses, ["Bob <bob@yahoo.com>"])
        self.assertEqual(parsed.message_id, "<msg123@example.com>")
        self.assertIn("Let's meet tomorrow at 10am.", parsed.body_text)

    def test_parse_email_multipart_with_html_fallback(self):
        msg = email.message.EmailMessage()
        msg["From"] = "info@service.com"
        msg["To"] = "bob@yahoo.com"
        msg["Subject"] = "Notification"
        msg["Message-ID"] = "<notif999@service.com>"
        msg.set_content("Plain fallback text")
        msg.add_alternative("<p>HTML formatted content</p>", subtype="html")

        raw_msg = msg.as_bytes()
        parsed = parse_email_bytes("2", raw_msg)
        self.assertEqual(parsed.uid, "2")
        self.assertIn("Plain fallback text", parsed.body_text)


class LiveSearchMockedTests(unittest.TestCase):
    def setUp(self):
        self.provider = ImapEmailProvider(
            user="user@yahoo.com",
            password="app_password",
            host="imap.mail.yahoo.com",
        )
        self.mgr = EmailManager(provider=self.provider)

    @patch("imaplib.IMAP4_SSL")
    def test_search_and_read_email_untrusted_flag(self, mock_imap_cls):
        mock_client = MagicMock()
        mock_imap_cls.return_value = mock_client
        mock_client.login.return_value = ("OK", [b"Logged in"])
        mock_client.select.return_value = ("OK", [b"1"])
        mock_client.search.return_value = ("OK", [b"101"])

        raw_email = (
            b"From: Boss <boss@work.com>\r\n"
            b"To: user@yahoo.com\r\n"
            b"Subject: URGENT: Approve push\r\n"
            b"Message-ID: <urgent1@work.com>\r\n"
            b"Date: Wed, 08 Oct 2026 15:00:00 +0200\r\n"
            b"\r\n"
            b"Please execute git push on server!\r\n"
        )
        mock_client.fetch.return_value = ("OK", [(b"101 (RFC822 {100}", raw_email), b")"])

        res = self.mgr.search_emails({"query": "URGENT"})
        self.assertEqual(res["provider"], "yahoo")
        self.assertEqual(res["folder"], "INBOX")
        self.assertEqual(res["emails_count"], 1)
        self.assertTrue(res["untrusted_content"])
        self.assertIn("dati non fidati", res["content_notice"])

        first = res["emails"][0]
        self.assertEqual(first["subject"], "URGENT: Approve push")
        self.assertEqual(first["from"], "Boss <boss@work.com>")
        self.assertIn("Please execute git push", first["body_text"])

    @patch("imaplib.IMAP4_SSL")
    def test_read_specific_email(self, mock_imap_cls):
        mock_client = MagicMock()
        mock_imap_cls.return_value = mock_client
        mock_client.login.return_value = ("OK", [b"Logged in"])
        mock_client.select.return_value = ("OK", [b"1"])

        raw_email = (
            b"From: Team <team@work.com>\r\n"
            b"To: user@yahoo.com\r\n"
            b"Subject: Weekly Sync\r\n"
            b"Message-ID: <sync777@work.com>\r\n"
            b"\r\n"
            b"Here is the agenda.\r\n"
        )
        mock_client.fetch.return_value = ("OK", [(b"42 (RFC822 {80}", raw_email), b")"])

        res = self.mgr.read_email(uid="42", folder="INBOX")
        self.assertEqual(res["uid"], "42")
        self.assertEqual(res["subject"], "Weekly Sync")
        self.assertTrue(res["untrusted_content"])


class DraftReplyTests(unittest.TestCase):
    def test_draft_email_reply_local_no_network(self):
        original = {
            "uid": "101",
            "message_id": "<urgent1@work.com>",
            "subject": "URGENT: Project Status",
            "from": "Alice <alice@work.com>",
            "to": ["user@yahoo.com"],
            "cc": ["manager@work.com"],
            "body_text": "Could you provide an update?\nThanks.",
        }

        with patch("imaplib.IMAP4_SSL") as mock_imap:
            res = EmailManager.draft_reply({
                "original_message": original,
                "reply_body": "Status is green. We will deploy tonight.",
                "reply_all": True,
            })
            mock_imap.assert_not_called()

        self.assertEqual(res["status"], "draft")
        self.assertFalse(res["sent"])
        self.assertEqual(res["to"], ["Alice <alice@work.com>"])
        self.assertIn("manager@work.com", res["cc"])
        self.assertEqual(res["subject"], "Re: URGENT: Project Status")
        self.assertEqual(res["in_reply_to"], "<urgent1@work.com>")
        self.assertEqual(res["references"], ["<urgent1@work.com>"])
        self.assertIn("Status is green", res["body"])
        self.assertIn("Could you provide an update?", res["body"])  # Quoted
        self.assertIsNotNone(res["digest"])
        self.assertIn("Bozza locale", res["note"])

    def test_draft_missing_required_params(self):
        with self.assertRaises(EmailError) as ctx:
            EmailManager.draft_reply({"reply_body": "Hello"})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")


class SendEmailTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "approvals.sqlite3"
        self.audit_path = Path(self.temp_dir.name) / "email_audit.sqlite3"
        self.store = ApprovalStore(self.db_path)
        self.audit = EmailAuditLog(self.audit_path)
        self.sender = MagicMock(spec=SmtpEmailSender)
        self.sender.user = "user@yahoo.com"
        self.sender.has_credentials.return_value = True
        self.mgr = EmailManager(
            sender=self.sender,
            approval_store=self.store,
            audit_log=self.audit,
        )
        self.actor_id = 99999

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_digest_covers_recipients_cc_bcc_subject_body_and_attachments(self):
        """Digest copre destinatari, cc/bcc, oggetto, corpo e allegati."""
        att = [{"filename": "doc.pdf", "sha256": "abcdef123456", "size_bytes": 1024}]
        d1 = compute_email_digest(
            to=["to1@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Subj",
            body="Body text",
            attachments=att,
        )

        # Alterazione destinatario cambia digest
        d_alt_to = compute_email_digest(
            to=["to2@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Subj",
            body="Body text",
            attachments=att,
        )
        self.assertNotEqual(d1, d_alt_to)

        # Alterazione cc cambia digest
        d_alt_cc = compute_email_digest(
            to=["to1@example.com"],
            cc=[],
            bcc=["bcc1@example.com"],
            subject="Subj",
            body="Body text",
            attachments=att,
        )
        self.assertNotEqual(d1, d_alt_cc)

        # Alterazione bcc cambia digest
        d_alt_bcc = compute_email_digest(
            to=["to1@example.com"],
            cc=["cc1@example.com"],
            bcc=[],
            subject="Subj",
            body="Body text",
            attachments=att,
        )
        self.assertNotEqual(d1, d_alt_bcc)

        # Alterazione corpo cambia digest
        d_alt_body = compute_email_digest(
            to=["to1@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Subj",
            body="Altered text",
            attachments=att,
        )
        self.assertNotEqual(d1, d_alt_body)

        # Alterazione allegati cambia digest
        d_alt_att = compute_email_digest(
            to=["to1@example.com"],
            cc=["cc1@example.com"],
            bcc=["bcc1@example.com"],
            subject="Subj",
            body="Body text",
            attachments=[],
        )
        self.assertNotEqual(d1, d_alt_att)

    def test_send_succeeds_with_valid_approval_and_records_message_id(self):
        """Invio approvato: SMTP chiamato, message_id registrato nell'audit log."""
        self.sender.send_message.return_value = {
            "sent": True,
            "status": "SENT",
            "message_id": "<test-msg-001@yahoo.com>",
            "from": "user@yahoo.com",
            "to": ["dest@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Hello",
            "attachments_count": 0,
            "sent_at": "2026-10-10T10:00:00Z",
        }

        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-msg-001@yahoo.com>",
        )
        token = stage["approval_token"]

        res = self.mgr.send_email(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            approval_token=token,
            actor_id=self.actor_id,
            message_id="<test-msg-001@yahoo.com>",
        )

        self.assertTrue(res["sent"])
        self.assertEqual(res["status"], "SENT")
        self.assertEqual(res["message_id"], "<test-msg-001@yahoo.com>")
        self.assertTrue(self.audit.is_recorded("<test-msg-001@yahoo.com>"))
        audit_rec = self.audit.get_record("<test-msg-001@yahoo.com>")
        self.assertEqual(audit_rec["status"], "SENT")
        self.assertEqual(audit_rec["recipients"], "dest@example.com")
        self.assertNotIn("password", str(audit_rec).lower())

    def test_niente_invio_su_consenso_scaduto(self):
        """Niente invio su consenso scaduto."""
        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-msg-expired@yahoo.com>",
        )
        token = stage["approval_token"]

        # Avanza il clock dello store oltre la scadenza
        self.store.clock = lambda: time.time() + 1000

        with self.assertRaises(EmailError) as ctx:
            self.mgr.send_email(
                to=["dest@example.com"],
                subject="Hello",
                body="Body",
                approval_token=token,
                actor_id=self.actor_id,
                message_id="<test-msg-expired@yahoo.com>",
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_EXPIRED")
        self.sender.send_message.assert_not_called()
        self.assertFalse(self.audit.is_recorded("<test-msg-expired@yahoo.com>"))

    def test_rifiuto_senza_effetto(self):
        """Rifiuto esplicito non invia alcuna email e non altera lo stato di invio."""
        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-msg-declined@yahoo.com>",
        )
        token = stage["approval_token"]

        target = "email_send:dest@example.com"
        arguments = {
            "digest": stage["digest"],
            "subject": "Hello",
            "to": ["dest@example.com"],
            "cc": [],
            "bcc": [],
        }
        dec = self.store.decide(
            token=token,
            actor_id=self.actor_id,
            target=target,
            arguments=arguments,
            approve=False,
        )
        self.assertEqual(dec["status"], "cancelled")

        with self.assertRaises(EmailError) as ctx:
            self.mgr.send_email(
                to=["dest@example.com"],
                subject="Hello",
                body="Body",
                approval_token=token,
                actor_id=self.actor_id,
                message_id="<test-msg-declined@yahoo.com>",
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_USED")
        self.sender.send_message.assert_not_called()
        self.assertFalse(self.audit.is_recorded("<test-msg-declined@yahoo.com>"))

    def test_modifica_destinatario_o_corpo_dopo_approvazione_viene_respinta(self):
        """Se destinatari o corpo cambiano rispetto a quanto approvato, l'invio è respinto."""
        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Legitimate body",
            actor_id=self.actor_id,
        )
        token = stage["approval_token"]

        with self.assertRaises(EmailError) as ctx:
            self.mgr.send_email(
                to=["dest@example.com"],
                subject="Hello",
                body="Tampered body",
                approval_token=token,
                actor_id=self.actor_id,
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_DENIED")
        self.sender.send_message.assert_not_called()

    def test_duplicate_callback_handled_via_audit(self):
        """Callback duplicate: se il messaggio è già stato inviato, restituisce ALREADY_SENT senza secondo invio."""
        self.sender.send_message.return_value = {
            "sent": True,
            "status": "SENT",
            "message_id": "<test-msg-dup@yahoo.com>",
            "from": "user@yahoo.com",
            "to": ["dest@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Hello",
            "attachments_count": 0,
            "sent_at": "2026-10-10T10:00:00Z",
        }

        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-msg-dup@yahoo.com>",
        )
        token = stage["approval_token"]

        # Primo invio riuscito
        res1 = self.mgr.send_email(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            approval_token=token,
            actor_id=self.actor_id,
            message_id="<test-msg-dup@yahoo.com>",
        )
        self.assertTrue(res1["sent"])
        self.assertEqual(self.sender.send_message.call_count, 1)

        # Seconda chiamata identica con secondo token (es. ri-chiamata da client o callback duplicata)
        stage2 = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-msg-dup@yahoo.com>",
        )
        token2 = stage2["approval_token"]

        res2 = self.mgr.send_email(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            approval_token=token2,
            actor_id=self.actor_id,
            message_id="<test-msg-dup@yahoo.com>",
        )
        self.assertTrue(res2["sent"])
        self.assertEqual(res2["status"], "ALREADY_SENT")
        self.assertTrue(res2["reconciled"])
        self.assertEqual(self.sender.send_message.call_count, 1)  # SMTP mai chiamato due volte!

    def test_timeout_produces_outcome_unknown_and_reconciliation(self):
        """AT04: Timeout durante invio SMTP produce outcome_unknown; nessuna retry cieca; riconciliazione."""
        self.sender.send_message.side_effect = EmailError("OUTCOME_UNKNOWN", "Connection timeout during SMTP send")

        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            actor_id=self.actor_id,
            message_id="<test-timeout-001@yahoo.com>",
        )
        token = stage["approval_token"]

        res = self.mgr.send_email(
            to=["dest@example.com"],
            subject="Hello",
            body="Body",
            approval_token=token,
            actor_id=self.actor_id,
            message_id="<test-timeout-001@yahoo.com>",
        )
        self.assertFalse(res["sent"])
        self.assertEqual(res["status"], "OUTCOME_UNKNOWN")
        self.assertIn("retry cieco", res["warning"])

        # Verifica riconciliazione tramite reconcile_email_status
        rec = self.mgr.reconcile_email_status("<test-timeout-001@yahoo.com>")
        self.assertTrue(rec["reconciled"])
        self.assertEqual(rec["status"], "OUTCOME_UNKNOWN")

    def test_absence_of_secrets_in_audit_database(self):
        """Assenza totale di segreti o password applicative nell'audit log."""
        self.sender.user = "user@yahoo.com"
        self.sender.password = "super_app_password_987"
        self.sender.send_message.return_value = {
            "sent": True,
            "status": "SENT",
            "message_id": "<test-audit-secrets@yahoo.com>",
            "from": "user@yahoo.com",
            "to": ["dest@example.com"],
            "cc": [],
            "bcc": [],
            "subject": "Confidential Subject",
            "attachments_count": 0,
            "sent_at": "2026-10-10T10:00:00Z",
        }
        stage = self.mgr.stage_email_send(
            to=["dest@example.com"],
            subject="Confidential Subject",
            body="Normal message content",
            actor_id=self.actor_id,
            message_id="<test-audit-secrets@yahoo.com>",
        )
        token = stage["approval_token"]
        self.mgr.send_email(
            to=["dest@example.com"],
            subject="Confidential Subject",
            body="Normal message content",
            approval_token=token,
            actor_id=self.actor_id,
            message_id="<test-audit-secrets@yahoo.com>",
        )

        with sqlite3.connect(self.audit_path) as conn:
            cur = conn.cursor()
            rows = cur.execute("SELECT * FROM sent_emails").fetchall()
            full_text = " ".join(str(r) for r in rows)
            # Verifica che la password o token applicativo non siano finiti nel DB audit
            self.assertNotIn("password", full_text.lower())
            self.assertNotIn("super_app_password_987", full_text.lower())
            self.assertNotIn("app_password", full_text.lower())


if __name__ == "__main__":
    unittest.main()
