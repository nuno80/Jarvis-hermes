"""Unit tests for Google Calendar read + local draft (issue #23 / JARVIS-23, J16)."""
import json
import os
import unittest
from unittest.mock import MagicMock, patch
import urllib.error

from jarvis_hermes.calendar import (
    CalendarError,
    CalendarManager,
    CalendarSearchRequest,
    GoogleCalendarProvider,
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


if __name__ == "__main__":
    unittest.main()
