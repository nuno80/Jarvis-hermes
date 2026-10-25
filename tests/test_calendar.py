"""Unit tests for Google Calendar read + local draft (issue #23 / JARVIS-23, J16)."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from jarvis_hermes.approval import ApprovalStore
from jarvis_hermes.calendar import (
    CalendarAuditLog,
    CalendarError,
    CalendarManager,
    CalendarSearchRequest,
    GoogleCalendarProvider,
    compute_calendar_digest,
    normalize_event,
)


def _ctx(method, url="http://x", code=401):
    return urllib.error.HTTPError(url, code, "reason", {}, None)


class SearchValidationTests(unittest.TestCase):
    def test_invalid_timezone(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarSearchRequest.from_params({"time_zone": "Mars/Olympus"})
        self.assertEqual(ctx.exception.code, "INVALID_TIMEZONE")

    def test_inverted_range(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarSearchRequest.from_params({
                "time_min": "2026-11-02T10:00:00+01:00",
                "time_max": "2026-11-01T10:00:00+01:00",
            })
        self.assertEqual(ctx.exception.code, "INVALID_TIME_RANGE")

    def test_bad_max_results(self):
        for bad in [0, 51, "many"]:
            with self.assertRaises(CalendarError) as ctx:
                CalendarSearchRequest.from_params({"max_results": bad})
            self.assertEqual(ctx.exception.code, "INVALID_ARGUMENT")

    def test_bad_calendar_id(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarSearchRequest.from_params({"calendar_id": "../evil"})
        self.assertEqual(ctx.exception.code, "INVALID_CALENDAR_ID")

    def test_bad_time_format(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarSearchRequest.from_params({"time_min": "domani mattina"})
        self.assertEqual(ctx.exception.code, "INVALID_TIME_FORMAT")


class NormalizeTests(unittest.TestCase):
    def test_normalize_timed_and_allday(self):
        timed = normalize_event({
            "id": "abc", "summary": "Call", "status": "confirmed",
            "start": {"dateTime": "2026-11-01T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-01T11:00:00+01:00"},
            "location": "Roma",
            "description": "x" * 600,
            "attendees": [{"email": "a@b.it", "responseStatus": "accepted"}],
            "organizer": {"email": "me@gmail.com"},
            "htmlLink": "https://calendar.google.com/event?x",
        })
        self.assertFalse(timed.all_day)
        self.assertTrue(timed.description_truncated)
        self.assertEqual(len(timed.description), 500)
        self.assertEqual(timed.attendees[0]["email"], "a@b.it")
        self.assertNotIn("attendees", timed.unknown_fields)

        allday = normalize_event({
            "id": "d", "summary": "Festa", "start": {"date": "2026-12-25"},
            "end": {"date": "2026-12-26"},
        })
        self.assertTrue(allday.all_day)
        self.assertIn("location", allday.unknown_fields)
        self.assertIn("attendees", allday.unknown_fields)


class FailClosedTests(unittest.TestCase):
    def test_no_credentials_no_network(self):
        with patch.dict(os.environ, {}, clear=True):
            mgr = CalendarManager()
            with patch("urllib.request.urlopen") as mock_url:
                with self.assertRaises(CalendarError) as ctx:
                    mgr.search_events({})
                self.assertEqual(ctx.exception.code, "PROVIDER_NOT_CONFIGURED")
                self.assertIn("GOOGLE_CALENDAR_ACCESS_TOKEN", ctx.exception.message)
                mock_url.assert_not_called()


class LiveSearchTests(unittest.TestCase):
    def _mgr(self):
        return CalendarManager(GoogleCalendarProvider(access_token="ya29.test"))

    def test_lists_events_with_timezone_and_notice(self):
        mgr = self._mgr()
        payload = {"timeZone": "Europe/Rome", "items": [{
            "id": "e1", "summary": "Dentista",
            "start": {"dateTime": "2026-11-01T10:00:00+01:00"},
            "end": {"dateTime": "2026-11-01T11:00:00+01:00"},
            "status": "confirmed"}]}
        with patch("urllib.request.urlopen") as mock_url:
            resp = MagicMock()
            resp.read.return_value = json.dumps(payload).encode("utf-8")
            mock_url.return_value.__enter__.return_value = resp
            res = mgr.search_events({"q": "dentista", "max_results": 5})
        self.assertEqual(res["provider"], "google")
        self.assertEqual(res["events_count"], 1)
        self.assertEqual(res["events"][0]["summary"], "Dentista")
        self.assertTrue(res["untrusted_content"])
        sent_url = mock_url.call_args[0][0].full_url
        self.assertIn("singleEvents=true", sent_url)
        self.assertNotIn("ya29.test", sent_url)

    def test_empty_result_is_valid(self):
        mgr = self._mgr()
        with patch("urllib.request.urlopen") as mock_url:
            resp = MagicMock()
            resp.read.return_value = json.dumps({"items": []}).encode("utf-8")
            mock_url.return_value.__enter__.return_value = resp
            res = mgr.search_events({})
        self.assertEqual(res["events_count"], 0)
        self.assertEqual(res["events"], [])

    def test_revoked_access_is_auth_error(self):
        mgr = self._mgr()
        with patch("urllib.request.urlopen", side_effect=_ctx("GET", code=401)):
            with self.assertRaises(CalendarError) as ctx:
                mgr.search_events({})
        self.assertEqual(ctx.exception.code, "PROVIDER_AUTH_ERROR")
        self.assertFalse(ctx.exception.retryable)

    def test_unknown_calendar(self):
        mgr = self._mgr()
        with patch("urllib.request.urlopen", side_effect=_ctx("GET", code=404)):
            with self.assertRaises(CalendarError) as ctx:
                mgr.search_events({"calendar_id": "missing@group.calendar.google.com"})
        self.assertEqual(ctx.exception.code, "CALENDAR_NOT_FOUND")

    def test_500_is_retryable(self):
        mgr = self._mgr()
        with patch("urllib.request.urlopen", side_effect=_ctx("GET", code=503)):
            with self.assertRaises(CalendarError) as ctx:
                mgr.search_events({})
        self.assertEqual(ctx.exception.code, "PROVIDER_UNAVAILABLE")
        self.assertTrue(ctx.exception.retryable)

    def test_refresh_token_flow(self):
        env = {"GOOGLE_CALENDAR_CLIENT_ID": "cid", "GOOGLE_CALENDAR_CLIENT_SECRET": "csec",
               "GOOGLE_CALENDAR_REFRESH_TOKEN": "rtok"}
        with patch.dict(os.environ, env, clear=True):
            mgr = CalendarManager()
            with patch("urllib.request.urlopen") as mock_url:
                tok = MagicMock()
                tok.read.return_value = json.dumps({"access_token": "fresh"}).encode("utf-8")
                ev = MagicMock()
                ev.read.return_value = json.dumps({"items": []}).encode("utf-8")
                mock_url.return_value.__enter__.side_effect = [tok, ev]
                res = mgr.search_events({})
            self.assertEqual(res["events_count"], 0)
            auth_header = mock_url.call_args_list[1][0][0].get_header("Authorization")
            self.assertEqual(auth_header, "Bearer fresh")

    def test_search_does_not_leak_token_in_error(self):
        mgr = self._mgr()
        with patch("urllib.request.urlopen", side_effect=_ctx("GET", code=500)):
            with self.assertRaises(CalendarError):
                mgr.search_events({})
        # l'header Authorization non appare nel messaggio
        try:
            with patch("urllib.request.urlopen", side_effect=_ctx("GET", code=500)):
                mgr.search_events({})
        except CalendarError as exc:
            self.assertNotIn("ya29.test", exc.message)


class DraftTests(unittest.TestCase):
    def test_draft_is_local_no_network(self):
        with patch("urllib.request.urlopen") as mock_url:
            res = CalendarManager.draft_event({
                "summary": "Call dentista",
                "start": "2026-11-01T10:00:00+01:00",
                "end": "2026-11-01T10:30:00+01:00",
                "attendees": ["dentista@example.it"],
            })
            mock_url.assert_not_called()
        self.assertEqual(res["status"], "draft")
        self.assertFalse(res["created_event"])
        self.assertEqual(res["duration"], {"minutes": 30})
        self.assertFalse(res["dst_transition"])
        self.assertTrue(res["digest"])

    def test_draft_dst_transition_flag(self):
        res = CalendarManager.draft_event({
            "summary": "Notte legale-solare",
            "start": "2026-10-25T00:30:00+02:00",
            "end": "2026-10-25T04:30:00+01:00",
        })
        self.assertTrue(res["dst_transition"])

    def test_draft_all_day(self):
        res = CalendarManager.draft_event({
            "summary": "Festa", "start": "2026-12-25", "end": "2026-12-26",
        })
        self.assertTrue(res["all_day"])
        self.assertEqual(res["duration"], {"days": 1})

    def test_draft_missing_summary(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarManager.draft_event({"start": "2026-12-25", "end": "2026-12-26"})
        self.assertEqual(ctx.exception.code, "MISSING_PARAMETER")

    def test_draft_end_before_start(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarManager.draft_event({
                "summary": "x", "start": "2026-11-01T11:00:00+01:00",
                "end": "2026-11-01T10:00:00+01:00"})
        self.assertEqual(ctx.exception.code, "INVALID_TIME_RANGE")

    def test_draft_bad_attendee(self):
        with self.assertRaises(CalendarError) as ctx:
            CalendarManager.draft_event({
                "summary": "x", "start": "2026-11-01T10:00:00+01:00",
                "end": "2026-11-01T11:00:00+01:00", "attendees": ["non-una-mail"]})
        self.assertEqual(ctx.exception.code, "INVALID_ATTENDEE")


class ProviderUnitTests(unittest.TestCase):
    def test_direct_token_no_oauth_call(self):
        with patch.dict(os.environ, {"GOOGLE_CALENDAR_ACCESS_TOKEN": "ya29.direct"}, clear=True):
            prov = GoogleCalendarProvider()
            self.assertTrue(prov.has_credentials())
            self.assertEqual(prov._access_token(), "ya29.direct")


class CalendarCreateEventTests(unittest.TestCase):
    """Test di approvazione, digest, rifiuto, duplicati e riconciliazione per creazione eventi (Issue #24)."""

    def setUp(self):
        import tempfile
        self.temp_dir = tempfile.TemporaryDirectory()
        self.approval_store = ApprovalStore(os.path.join(self.temp_dir.name, "approval.sqlite3"))
        self.audit_log = CalendarAuditLog(os.path.join(self.temp_dir.name, "calendar_audit.sqlite3"))
        self.actor = 42

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_digest_covers_calendar_summary_start_end_and_attendees(self):
        d1 = compute_calendar_digest(
            calendar_id="primary",
            summary="Meeting",
            start="2026-11-01T10:00:00+01:00",
            end="2026-11-01T11:00:00+01:00",
            time_zone="Europe/Rome",
            attendees=["b@example.com", "a@example.com"],
        )
        # L'ordine iniziale degli invitati non deve alterare il digest se normalizzato
        d2 = compute_calendar_digest(
            calendar_id="primary",
            summary="Meeting",
            start="2026-11-01T10:00:00+01:00",
            end="2026-11-01T11:00:00+01:00",
            time_zone="Europe/Rome",
            attendees=["a@example.com", "b@example.com"],
        )
        self.assertEqual(d1, d2)

        # Modifica alla ricorrenza cambia il digest
        d4 = compute_calendar_digest(
            calendar_id="primary",
            summary="Meeting",
            start="2026-11-01T10:00:00+01:00",
            end="2026-11-01T11:00:00+01:00",
            time_zone="Europe/Rome",
            attendees=["a@example.com", "b@example.com"],
            recurrence=["RRULE:FREQ=YEARLY"],
        )
        self.assertNotEqual(d1, d4)

    def test_recurrence_normalization_and_payload_in_create(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Compleanno Annuale",
            start="2026-12-18",
            end="2026-12-19",
            actor_id=self.actor,
            recurrence=["FREQ=YEARLY"],
        )
        self.assertIn("RRULE:FREQ=YEARLY", stage["summary_text"])

        fake_resp = {
            "id": "recur_evt_1",
            "summary": "Compleanno Annuale",
            "start": {"date": "2026-12-18"},
            "end": {"date": "2026-12-19"},
            "recurrence": ["RRULE:FREQ=YEARLY"],
        }
        mock_http = MagicMock()
        mock_http.read.return_value = json.dumps(fake_resp).encode("utf-8")
        mock_http.__enter__.return_value = mock_http

        with patch("urllib.request.urlopen", return_value=mock_http) as mock_urlopen:
            res = mgr.create_event(
                calendar_id="primary",
                summary="Compleanno Annuale",
                start="2026-12-18",
                end="2026-12-19",
                approval_token=stage["approval_token"],
                actor_id=self.actor,
                recurrence=["FREQ=YEARLY"],
            )

        self.assertTrue(res["created"])
        self.assertEqual(res["event_id"], "recur_evt_1")
        # Verifica payload inviato a Google
        req_sent = mock_urlopen.call_args[0][0]
        sent_body = json.loads(req_sent.data.decode("utf-8"))
        self.assertEqual(sent_body["recurrence"], ["RRULE:FREQ=YEARLY"])

    def test_draft_with_recurrence(self):
        draft = CalendarManager.draft_event({
            "summary": "Meeting ricorrente",
            "start": "2026-11-01T10:00:00+01:00",
            "end": "2026-11-01T11:00:00+01:00",
            "recurrence": ["FREQ=WEEKLY;BYDAY=MO,WE,FR"],
        })
        self.assertEqual(draft["recurrence"], ["RRULE:FREQ=WEEKLY;BYDAY=MO,WE,FR"])
        self.assertIsInstance(draft["digest"], str)
        self.assertEqual(len(draft["digest"]), 64)

    def test_create_fails_without_approval_token(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        with self.assertRaises(CalendarError) as ctx:
            mgr.create_event(
                calendar_id="primary",
                summary="Evento non autorizzato",
                start="2026-11-01T10:00:00+01:00",
                end="2026-11-01T11:00:00+01:00",
                approval_token=None,
                actor_id=self.actor,
            )
        self.assertEqual(ctx.exception.code, "APPROVAL_REQUIRED")

    def test_modifica_parametri_dopo_approvazione_invalida_consenso(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Riunione Originale",
            start="2026-11-01T10:00:00+01:00",
            end="2026-11-01T11:00:00+01:00",
            actor_id=self.actor,
        )

        with patch("urllib.request.urlopen") as mock_url:
            with self.assertRaises(CalendarError) as ctx:
                mgr.create_event(
                    calendar_id="primary",
                    summary="Riunione MODIFICATA",  # Parametro manomesso
                    start="2026-11-01T10:00:00+01:00",
                    end="2026-11-01T11:00:00+01:00",
                    approval_token=stage["approval_token"],
                    actor_id=self.actor,
                )
            self.assertEqual(ctx.exception.code, "APPROVAL_DENIED")
            mock_url.assert_not_called()

    def test_rifiuto_non_scrive_e_registra_nessun_evento(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Riunione da Rifiutare",
            start="2026-11-01T10:00:00+01:00",
            end="2026-11-01T11:00:00+01:00",
            actor_id=self.actor,
        )

        # L'utente rifiuta: decide() imposta status='cancelled'
        self.approval_store.decide(
            token=stage["approval_token"],
            actor_id=self.actor,
            target="calendar_create:primary",
            arguments={"digest": stage["digest"], "calendar_id": "primary", "summary": "Riunione da Rifiutare",
                       "start": "2026-11-01T10:00:00+01:00", "end": "2026-11-01T11:00:00+01:00",
                       "time_zone": "Europe/Rome", "location": "", "description": "", "attendees": [],
                       "recurrence": []},
            approve=False,
        )

        # Tentativo successivo di usare il token rifiutato solleva APPROVAL_USED (non più pending)
        with patch("urllib.request.urlopen") as mock_url:
            with self.assertRaises(CalendarError) as ctx:
                mgr.create_event(
                    calendar_id="primary",
                    summary="Riunione da Rifiutare",
                    start="2026-11-01T10:00:00+01:00",
                    end="2026-11-01T11:00:00+01:00",
                    approval_token=stage["approval_token"],
                    actor_id=self.actor,
                )
            self.assertEqual(ctx.exception.code, "APPROVAL_USED")
            mock_url.assert_not_called()
        self.assertFalse(self.audit_log.is_recorded(stage["digest"]))

    def test_creazione_riuscita_con_consenso_valido_e_audit(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Pranzo con Collega",
            start="2026-11-02T13:00:00+01:00",
            end="2026-11-02T14:00:00+01:00",
            attendees=["collega@example.com"],
            actor_id=self.actor,
        )

        api_response = {
            "id": "cal_ev_999",
            "summary": "Pranzo con Collega",
            "start": {"dateTime": "2026-11-02T13:00:00+01:00"},
            "end": {"dateTime": "2026-11-02T14:00:00+01:00"},
            "status": "confirmed",
            "htmlLink": "https://calendar.google.com/event?eid=999",
        }

        with patch("urllib.request.urlopen") as mock_url:
            resp = MagicMock()
            resp.read.return_value = json.dumps(api_response).encode("utf-8")
            mock_url.return_value.__enter__.return_value = resp

            result = mgr.create_event(
                calendar_id="primary",
                summary="Pranzo con Collega",
                start="2026-11-02T13:00:00+01:00",
                end="2026-11-02T14:00:00+01:00",
                attendees=["collega@example.com"],
                approval_token=stage["approval_token"],
                actor_id=self.actor,
            )

        self.assertTrue(result["created"])
        self.assertEqual(result["event_id"], "cal_ev_999")
        self.assertEqual(result["status"], "CREATED")

        # Verifica persistenza su SQLite audit
        rec = self.audit_log.get_record(stage["digest"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["event_id"], "cal_ev_999")
        self.assertEqual(rec["status"], "CREATED")

    def test_callback_duplicata_non_duplica_evento_e_riconcilia(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Evento Idempotente",
            start="2026-11-03T10:00:00+01:00",
            end="2026-11-03T11:00:00+01:00",
            actor_id=self.actor,
        )

        # Pre-popoliamo l'audit log come se fosse già stato creato
        self.audit_log.record_created(
            calendar_id="primary",
            event_id="existing_event_777",
            summary="Evento Idempotente",
            start_time="2026-11-03T10:00:00+01:00",
            end_time="2026-11-03T11:00:00+01:00",
            digest=stage["digest"],
            status="CREATED",
        )

        with patch("urllib.request.urlopen") as mock_url:
            result = mgr.create_event(
                calendar_id="primary",
                summary="Evento Idempotente",
                start="2026-11-03T10:00:00+01:00",
                end="2026-11-03T11:00:00+01:00",
                approval_token=stage["approval_token"],
                actor_id=self.actor,
            )
            mock_url.assert_not_called()

        self.assertTrue(result["created"])
        self.assertEqual(result["status"], "ALREADY_CREATED")
        self.assertEqual(result["event_id"], "existing_event_777")
        self.assertTrue(result["reconciled"])

    def test_timeout_durante_creazione_produce_outcome_unknown_e_riconciliazione(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)
        stage = mgr.stage_create_event(
            calendar_id="primary",
            summary="Evento Rischio Timeout",
            start="2026-11-04T10:00:00+01:00",
            end="2026-11-04T11:00:00+01:00",
            actor_id=self.actor,
        )

        # Simula TimeoutError
        with patch("urllib.request.urlopen", side_effect=TimeoutError("Request timed out")):
            result = mgr.create_event(
                calendar_id="primary",
                summary="Evento Rischio Timeout",
                start="2026-11-04T10:00:00+01:00",
                end="2026-11-04T11:00:00+01:00",
                approval_token=stage["approval_token"],
                actor_id=self.actor,
            )

        self.assertFalse(result["created"])
        self.assertEqual(result["status"], "OUTCOME_UNKNOWN")
        self.assertIn("warning", result)

        # Verifica che l'audit registri OUTCOME_UNKNOWN (AT04)
        rec = self.audit_log.get_record(stage["digest"])
        self.assertIsNotNone(rec)
        self.assertEqual(rec["status"], "OUTCOME_UNKNOWN")

        # Riconciliazione successiva senza retry cieco
        recon = mgr.reconcile_calendar_event(stage["digest"])
        self.assertTrue(recon["reconciled"])
        self.assertEqual(recon["status"], "OUTCOME_UNKNOWN")

    def test_update_and_delete_event_with_approval(self):
        prov = GoogleCalendarProvider(access_token="test_token")
        mgr = CalendarManager(provider=prov, approval_store=self.approval_store, audit_log=self.audit_log)

        # 1. Update test
        stage_up = mgr.stage_update_event(
            calendar_id="primary",
            event_id="evt123",
            actor_id=self.actor,
            start="2026-10-21T08:00:00+02:00",
            end="2026-10-21T15:00:00+02:00",
            summary="Iml Spostato",
        )
        fake_resp = {
            "id": "evt123",
            "summary": "Iml Spostato",
            "start": {"dateTime": "2026-10-21T08:00:00+02:00"},
            "end": {"dateTime": "2026-10-21T15:00:00+02:00"},
        }
        mock_http = MagicMock()
        mock_http.read.return_value = json.dumps(fake_resp).encode("utf-8")
        mock_http.__enter__.return_value = mock_http

        with patch("urllib.request.urlopen", return_value=mock_http):
            res_up = mgr.update_event(
                calendar_id="primary",
                event_id="evt123",
                approval_token=stage_up["approval_token"],
                actor_id=self.actor,
                start="2026-10-21T08:00:00+02:00",
                end="2026-10-21T15:00:00+02:00",
                summary="Iml Spostato",
            )
        self.assertTrue(res_up["updated"])
        self.assertEqual(res_up["status"], "UPDATED")

        # 2. Delete test
        stage_del = mgr.stage_delete_event(
            calendar_id="primary",
            event_id="evt123",
            actor_id=self.actor,
        )
        with patch("urllib.request.urlopen", return_value=mock_http):
            res_del = mgr.delete_event(
                calendar_id="primary",
                event_id="evt123",
                approval_token=stage_del["approval_token"],
                actor_id=self.actor,
            )
        self.assertTrue(res_del["deleted"])
        self.assertEqual(res_del["status"], "DELETED")




if __name__ == "__main__":
    unittest.main()
