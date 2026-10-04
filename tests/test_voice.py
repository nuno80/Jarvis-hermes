"""Flusso vocale Telegram end-to-end (issue #18 / JARVIS-18, J11, D03, sez. 7).

Seam TDD: ``handle_voice_transcript`` e pura dict-in/dict-out con
``dispatch_fn`` iniettabile (niente Ollama/rete nei test); store reali in
tempdir (sqlite, veloci).
"""
import tempfile
import unittest
from pathlib import Path

from jarvis_hermes.jobs import JobStore
from jarvis_hermes.llm import BudgetTracker, LLMError, STTClient
from jarvis_hermes import voice
from jarvis_hermes.voice import (
    ACK_LATENCY_BUDGET_MS,
    VOICE_MAX_DURATION_SECONDS,
    VoiceError,
    build_ack,
    build_clarification,
    handle_voice_transcript,
)


def _tracker(directory, job_limit_usd=1.00, daily_limit_usd=10.00):
    return BudgetTracker(Path(directory) / "budget.json",
                         job_limit_usd=job_limit_usd, daily_limit_usd=daily_limit_usd)


def _jobs(directory):
    return JobStore(Path(directory) / "jobs.sqlite3")


def _reply_dispatch(text):
    return {"path": "system_1", "target": "deterministic", "intent": "disk_usage",
            "handler": "disk_usage", "confidence": 1.0,
            "reply": "Spazio libero: 10.0 GiB.", "escalation_reason": None}


def _system2_dispatch(text):
    return {"path": "system_2", "target": "reasoning_llm", "intent": "web_search",
            "handler": "web_search", "confidence": 0.5,
            "reply": None, "escalation_reason": "low_confidence"}


def _clarify_dispatch(text):
    return {"path": "system_2", "target": "clarification", "intent": "ambiguous",
            "handler": "ask_clarification", "confidence": 0.4,
            "reply": None, "escalation_reason": "needs_clarification"}


def _run(transcript, dispatch, directory, **kwargs):
    params = {"actor_id": 123456, "budget_tracker": _tracker(directory),
              "job_store": _jobs(directory), "dispatch_fn": dispatch}
    params.update(kwargs)
    return handle_voice_transcript(transcript, **params)


class VoicePluginWiringTests(unittest.TestCase):
    """Cablaggio hook live (fix review #18): durata da raw Telegram,
    actor solo da source, skip su answered, probe non rumoroso."""

    def _import_plugin(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "jarvis_s1_voice_test", "plugins/jarvis-s1/__init__.py")
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        return mod

    def _event(self, text="ciao", mtype="voice", duration=None, user_id="123"):
        from unittest.mock import MagicMock
        event = MagicMock()
        event.text = text
        event.message_type = mtype
        event.media_urls = []
        event.media_types = []
        event.source.platform = "telegram"
        event.source.chat_id = "123"
        event.source.user_id = user_id
        clip = MagicMock() if duration else None
        if clip is not None:
            clip.duration = duration
        event.raw_message.voice = clip
        event.raw_message.audio = None
        return event

    def test_duration_comes_from_telegram_raw_clip(self):
        mod = self._import_plugin()
        self.assertEqual(
            mod._voice_duration_seconds(self._event(duration=42)), 42)
        self.assertIsNone(mod._voice_duration_seconds(self._event()))

    def test_actor_id_never_falls_back_to_allowlist(self):
        mod = self._import_plugin()
        self.assertEqual(mod._voice_actor_id(self._event(user_id="388282337")), 388282337)
        self.assertEqual(mod._voice_actor_id(self._event(user_id=None)), 0)

    def test_answered_returns_skip_not_rewrite(self):
        mod = self._import_plugin()
        from unittest.mock import AsyncMock, MagicMock
        adapter = MagicMock()
        adapter.send = AsyncMock(return_value=MagicMock(success=True))
        gateway = MagicMock()
        gateway.adapters = {"telegram": adapter}
        res = mod.pre_gateway_dispatch_hook(
            self._event("quanto spazio libero ho sul disco"), gateway, None)
        self.assertEqual(res["action"], "skip")
        self.assertEqual(res["reason"], "jarvis_voice_answered")

    def test_probe_log_line_is_present_but_quiet(self):
        import re
        with open("plugins/jarvis-s1/__init__.py", encoding="utf-8") as fh:
            src = fh.read()
        self.assertEqual(len(re.findall(r"jarvis voice probe", src)), 1)


class VoicePipelineTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.directory = self.tmp.name

    def tearDown(self):
        self.tmp.cleanup()

    def test_fast_path_answers_with_transcript_prefix(self):
        res = _run("quanto spazio libero ho sul disco", _reply_dispatch, self.directory)
        self.assertEqual(res["status"], "answered")
        self.assertIn("Spazio libero", res["reply"])
        self.assertIn("🎙️", res["reply"])
        self.assertLess(res["ack_latency_ms"], ACK_LATENCY_BUDGET_MS)
        job = _jobs(self.directory).get_job(res["job_id"])
        self.assertEqual(job["status"], "succeeded")

    def test_system2_gets_working_ack_without_claiming_outcomes(self):
        res = _run("cercami voli per Bali a novembre", _system2_dispatch, self.directory)
        self.assertEqual(res["status"], "working")
        ack = res["ack"]
        self.assertIn(res["job_id"], ack)
        self.assertIn("🎙️", ack)
        for claimed in ("completato", "fatto", "inviato", "eseguito", "ecco i voli"):
            self.assertNotIn(claimed, ack.lower())
        self.assertLess(res["ack_latency_ms"], ACK_LATENCY_BUDGET_MS)
        job = _jobs(self.directory).get_job(res["job_id"])
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["last_step"], "voice_ack_sent")

    def test_protected_hint_asks_clarification_before_any_effect(self):
        res = _run("cancella tutto dal server", _system2_dispatch, self.directory)
        self.assertEqual(res["status"], "needs_clarification")
        self.assertIn("Non ho eseguito nulla", res["reply"])
        job = _jobs(self.directory).get_job(res["job_id"])
        self.assertEqual(job["status"], "running")
        self.assertEqual(job["last_step"], "clarification_requested")
        self.assertEqual(job["effects_count"], 0)

    def test_ambiguous_dispatch_asks_clarification(self):
        res = _run("fai quella cosa lì", _clarify_dispatch, self.directory)
        self.assertEqual(res["status"], "needs_clarification")

    def test_too_long_voice_fails_closed_and_records_usage(self):
        tracker = _tracker(self.directory)
        jobs = _jobs(self.directory)
        with self.assertRaises(VoiceError) as ctx:
            handle_voice_transcript("x" * 10, actor_id=1, job_id="long-job",
                                    duration_seconds=VOICE_MAX_DURATION_SECONDS + 1,
                                    budget_tracker=tracker, job_store=jobs,
                                    dispatch_fn=_system2_dispatch)
        self.assertEqual(ctx.exception.code, "VOICE_TOO_LONG")
        self.assertGreater(tracker.get_job_usage("long-job")["calls_count"], 0)

    def test_boundary_duration_is_accepted(self):
        res = _run("ciao", _system2_dispatch, self.directory,
                   duration_seconds=VOICE_MAX_DURATION_SECONDS)
        self.assertEqual(res["status"], "working")

    def test_empty_transcript_asks_resend_without_job(self):
        res = _run("   ", _system2_dispatch, self.directory)
        self.assertEqual(res["status"], "needs_resend")
        self.assertIsNone(res["job_id"])
        self.assertIn("Reinvia", res["reply"])

    def test_stt_consumption_recorded_per_job(self):
        tracker = _tracker(self.directory)
        res = handle_voice_transcript("ciao", actor_id=1, job_id="stt-job",
                                      duration_seconds=30, budget_tracker=tracker,
                                      job_store=_jobs(self.directory),
                                      dispatch_fn=_system2_dispatch)
        usage = tracker.get_job_usage("stt-job")
        self.assertGreaterEqual(usage["calls_count"], 1)
        self.assertGreater(usage["total_tokens"], 0)
        self.assertIn("provider", res["stt_usage"])

    def test_budget_exhausted_raises_without_simulation(self):
        tracker = _tracker(self.directory, job_limit_usd=0.0)
        with self.assertRaises(LLMError) as ctx:
            handle_voice_transcript("ciao", actor_id=1, job_id="broke-job",
                                    budget_tracker=tracker,
                                    job_store=_jobs(self.directory),
                                    dispatch_fn=_system2_dispatch)
        self.assertEqual(ctx.exception.code, "BUDGET_EXCEEDED")

    def test_dispatch_failure_degrades_to_working(self):
        def _boom(text):
            raise RuntimeError("s1 down")
        res = _run("ciao", _boom, self.directory)
        self.assertEqual(res["status"], "working")
        self.assertEqual(res["dispatch"]["escalation_reason"], "dispatch_failed")

    def test_audio_retention_explicit_and_no_approval_in_job(self):
        res = _run("ciao", _system2_dispatch, self.directory)
        job = _jobs(self.directory).get_job(res["job_id"])
        self.assertIn("audio_bytes_stored_by_jarvis=false", job["metadata"]["audio_retention"])
        self.assertEqual(job["metadata"]["transcript_source"], "voice_stt_untrusted")
        self.assertNotIn("approval_token", job["metadata"])
        self.assertGreater(job["metadata"]["transcript_chars"], 0)

    def test_module_creates_no_approvals(self):
        import re
        with open(voice.__file__, encoding="utf-8") as fh:
            src = fh.read()
        for banned in ("ApprovalStore", "approval_token", "elicit", "store.decide",
                         "store.request", "simulate_with_approval"):
            self.assertNotIn(banned, src)
        self.assertNotRegex(src, re.compile(r"\bapprove\s*\(", re.IGNORECASE))


class VoiceTextTests(unittest.TestCase):
    def test_ack_mentions_plan_and_job(self):
        ack = build_ack("quanto spazio libero ho", _reply_dispatch(""), "job-abc")
        self.assertIn("stato del disco", ack)
        self.assertIn("job-abc", ack)

    def test_ack_generic_plan_for_unknown_intent(self):
        ack = build_ack("bla bla", _system2_dispatch(""), "job-x")
        self.assertIn("job-x", ack)

    def test_clarification_quotes_and_denies_effects(self):
        msg = build_clarification("cancella tutto subito per favore")
        self.assertIn("cancella tutto", msg)
        self.assertIn("Non ho eseguito nulla", msg)


class STTClientTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.tracker = _tracker(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_transcription_registers_estimated_cost(self):
        rec = STTClient(self.tracker, model_id="m").record_transcription("j1", 60)
        self.assertEqual(rec.provider, "stt")
        self.assertTrue(rec.is_estimated)
        self.assertGreater(rec.cost_usd, 0.0)
        usage = self.tracker.get_job_usage("j1")
        self.assertEqual(usage["total_tokens"], 240)

    def test_negative_duration_rejected(self):
        with self.assertRaises(LLMError) as ctx:
            STTClient(self.tracker).record_transcription("j1", -5)
        self.assertEqual(ctx.exception.code, "STT_INVALID_DURATION")

    def test_legacy_transcribe_still_budgets(self):
        text = STTClient(self.tracker).transcribe(b"x" * 64000, job_id="j2")
        self.assertEqual(text, "")
        self.assertGreater(self.tracker.get_job_usage("j2")["calls_count"], 0)


if __name__ == "__main__":
    unittest.main()
