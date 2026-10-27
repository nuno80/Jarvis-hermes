"""Unit and acceptance tests for persistent reminders, timezone/DST handling, cancellation, and restart recovery."""
import os
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from jarvis_hermes.reminder import (
    ReminderStore,
    ReminderError,
    create_reminder,
    list_reminders,
    cancel_reminder,
    reconcile_reminders_on_startup,
    dispatch_due_reminders,
    normalize_due_datetime,
)


class ReminderTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temp_dir.name) / "reminders.sqlite3"
        self.store = ReminderStore(self.db_path)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_create_and_list_reminder(self):
        """Pianificazione di un reminder esplicito con timezone e consultazione."""
        due_dt = datetime.now(timezone.utc) + timedelta(hours=2)
        res = self.store.create(
            actor_id=123456,
            text="Prendere il treno",
            due_at=due_dt.isoformat(),
            timezone_str="Europe/Rome",
        )
        self.assertTrue(res["reminder_id"].startswith("rem-"))
        self.assertEqual(res["text"], "Prendere il treno")
        self.assertEqual(res["status"], "scheduled")
        self.assertEqual(res["timezone"], "Europe/Rome")
        self.assertEqual(res["delivery_status"], "pending")

        # Verifica list_reminders
        active = self.store.list_reminders(actor_id=123456, status="scheduled")
        self.assertEqual(len(active), 1)
        self.assertEqual(active[0]["reminder_id"], res["reminder_id"])

    def test_cancel_reminder(self):
        """Annullamento di un reminder pianificato prima della scadenza."""
        due_dt = datetime.now(timezone.utc) + timedelta(hours=1)
        created = self.store.create(
            actor_id=123456,
            text="Riunione da annullare",
            due_at=due_dt.isoformat(),
            timezone_str="Europe/Rome",
        )
        rem_id = created["reminder_id"]

        cancelled = self.store.cancel(reminder_id=rem_id, actor_id=123456)
        self.assertEqual(cancelled["status"], "cancelled")

        # Verifichiamo che non sia più tra gli attivi
        active = self.store.list_reminders(actor_id=123456, status="scheduled")
        self.assertEqual(len(active), 0)

        # Non si può annullare di nuovo
        with self.assertRaises(ReminderError) as ctx:
            self.store.cancel(reminder_id=rem_id, actor_id=123456)
        self.assertEqual(ctx.exception.code, "INVALID_STATE")

    def test_timezone_and_dst_transition(self):
        """AT12: Test cambio ora legale (DST transition).

        Verifica che una scadenza fissata attraverso il cambio dell'ora legale
        mantenga l'istante UTC esatto e l'orario locale atteso senza salti.
        In Europe/Rome:
        - Fine marzo (es. 2026-03-29): da UTC+1 a UTC+2
        - Fine ottobre (es. 2026-10-25): da UTC+2 a UTC+1
        """
        # Creazione prima del passaggio all'ora legale (29 marzo 2026 ore 08:00 ora italiana = UTC+2)
        rome_tz = ZoneInfo("Europe/Rome")
        local_target = datetime(2026, 3, 29, 8, 0, 0, tzinfo=rome_tz)
        
        utc_dt, local_iso = normalize_due_datetime("2026-03-29T08:00:00", "Europe/Rome")
        # Il 29 marzo 2026 alle 08:00 l'Italia è già a UTC+2 (il cambio avviene alle 02:00 -> 03:00)
        self.assertEqual(utc_dt.tzinfo, timezone.utc)
        self.assertEqual(utc_dt.hour, 6)  # 08:00 - 2h = 06:00 UTC
        self.assertEqual(utc_dt.day, 29)

        # Test passaggio autunnale (25 ottobre 2026 ore 08:00 ora italiana = UTC+1)
        utc_autumn, _ = normalize_due_datetime("2026-10-25T08:00:00", "Europe/Rome")
        # Il 25 ottobre 2026 alle 08:00 l'Italia è tornata a UTC+1 (il cambio avviene alle 03:00 -> 02:00)
        self.assertEqual(utc_autumn.hour, 7)  # 08:00 - 1h = 07:00 UTC

        # Memorizzazione nel database
        res = self.store.create(
            actor_id=123456,
            text="Sveglia dopo cambio ora",
            due_at=utc_dt.isoformat(),
            timezone_str="Europe/Rome",
        )
        self.assertEqual(res["due_at_utc"], utc_dt.isoformat())

    def test_reconcile_reminders_on_startup_after_downtime(self):
        """AT12: Reminder scaduto durante lo spegnimento segue la policy dichiarata (missed_during_downtime)."""
        # Creiamo un reminder scaduto nel passato (simulando che il nodo era spento alla scadenza)
        past_utc = datetime.now(timezone.utc) - timedelta(minutes=15)
        rem = self.store.create(
            actor_id=123456,
            text="Compito durante il riavvio",
            due_at=past_utc.isoformat(),
            timezone_str="Europe/Rome",
        )
        self.assertEqual(rem["status"], "scheduled")

        # Riavvio del nodo -> esecuzione reconcile
        reconciled = self.store.reconcile_on_startup(now_utc=datetime.now(timezone.utc))
        self.assertEqual(len(reconciled), 1)
        self.assertEqual(reconciled[0]["reminder_id"], rem["reminder_id"])
        self.assertEqual(reconciled[0]["status"], "missed_during_downtime")
        self.assertEqual(reconciled[0]["delivery_status"], "pending")

        # Riapertura store da nuovo oggetto (persistenza SQLite verificata)
        new_store = ReminderStore(self.db_path)
        fetched = new_store.get(rem["reminder_id"])
        self.assertEqual(fetched["status"], "missed_during_downtime")

    def test_delivery_dispatch_and_uncertainty(self):
        """Consegna Telegram con gestione esito incerto (no exactly-once millantato)."""
        # 1. Consegna riuscita
        due_now = datetime.now(timezone.utc) - timedelta(seconds=5)
        rem1 = self.store.create(
            actor_id=123456,
            text="Reminder 1",
            due_at=due_now.isoformat(),
            timezone_str="Europe/Rome",
        )

        def mock_sender_ok(actor_id, text):
            return {"ok": True, "message_id": 99901}

        dispatched = self.store.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=mock_sender_ok)
        self.assertEqual(len(dispatched), 1)
        self.assertEqual(dispatched[0]["status"], "fired")
        self.assertEqual(dispatched[0]["delivery_status"], "delivered")

        # 2. Consegna con timeout o errore di rete incerto (es. 504 / ConnectionReset)
        rem2 = self.store.create(
            actor_id=123456,
            text="Reminder 2",
            due_at=due_now.isoformat(),
            timezone_str="Europe/Rome",
        )

        def mock_sender_uncertain(actor_id, text):
            raise TimeoutError("Telegram gateway timeout")

        dispatched2 = self.store.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=mock_sender_uncertain)
        self.assertEqual(len(dispatched2), 1)
        self.assertEqual(dispatched2[0]["status"], "fired")
        self.assertEqual(dispatched2[0]["delivery_status"], "delivery_uncertain")
        self.assertIn("Telegram gateway timeout", dispatched2[0]["warnings"])

        # Verifica che delivery_uncertain non venga ri-tentato automaticamente da un secondo dispatch_due
        dispatched_retry = self.store.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=mock_sender_ok)
        self.assertEqual(len(dispatched_retry), 0)


    def test_atomic_claim_and_concurrent_dispatchers(self):
        """Due dispatcher concorrenti producono un solo invio tramite claim atomico."""
        import threading

        due_now = datetime.now(timezone.utc) - timedelta(seconds=5)
        rem = self.store.create(
            actor_id=123456,
            text="Reminder claim atomico",
            due_at=due_now.isoformat(),
            timezone_str="Europe/Rome",
        )

        call_count = 0
        lock = threading.Lock()

        def slow_sender(actor_id, text):
            import time
            nonlocal call_count
            with lock:
                call_count += 1
            time.sleep(0.05)
            return {"ok": True, "message_id": 1001}

        res1, res2 = [], []

        def worker1():
            s = ReminderStore(self.db_path)
            res1.extend(s.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=slow_sender))

        def worker2():
            s = ReminderStore(self.db_path)
            res2.extend(s.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=slow_sender))

        t1 = threading.Thread(target=worker1)
        t2 = threading.Thread(target=worker2)
        t1.start()
        t2.start()
        t1.join()
        t2.join()

        self.assertEqual(call_count, 1)
        dispatched_total = len(res1) + len(res2)
        self.assertEqual(dispatched_total, 1)
        final = self.store.get(rem["reminder_id"])
        self.assertEqual(final["status"], "fired")
        self.assertEqual(final["delivery_status"], "delivered")

    def test_reconcile_sending_after_crash(self):
        """Un record rimasto in sending dopo un crash viene riconciliato come incerto all'avvio."""
        now_dt = datetime.now(timezone.utc)
        rem = self.store.create(
            actor_id=123456,
            text="Reminder interrupted during sending",
            due_at=now_dt.isoformat(),
            timezone_str="Europe/Rome",
        )
        # Forziamo lo stato in 'sending' per simulare un crash del processo durante la chiamata al sender
        with self.store._connection() as conn:
            conn.execute(
                "UPDATE reminders SET status = 'sending', delivery_status = 'sending' WHERE reminder_id = ?",
                (rem["reminder_id"],),
            )
            conn.commit()

        # Riavvio e riconciliazione all'avvio
        reconciled = self.store.reconcile_on_startup(now_utc=datetime.now(timezone.utc))
        rec_ids = [r["reminder_id"] for r in reconciled]
        self.assertIn(rem["reminder_id"], rec_ids)

        final = self.store.get(rem["reminder_id"])
        # Uno stato che rappresenta la riconciliazione deterministica come incerto senza retry cieco
        self.assertEqual(final["status"], "fired")
        self.assertEqual(final["delivery_status"], "delivery_uncertain")
        self.assertIn("crash", final["warnings"].lower())

    def test_sender_mandatory_for_dispatch(self):
        """Sender assente non può produrre delivered (solleva ValueError / ReminderError o fallisce)."""
        due_now = datetime.now(timezone.utc) - timedelta(seconds=5)
        rem = self.store.create(
            actor_id=123456,
            text="Reminder test mandatory sender",
            due_at=due_now.isoformat(),
            timezone_str="Europe/Rome",
        )
        with self.assertRaises((ValueError, ReminderError)):
            self.store.dispatch_due(now_utc=datetime.now(timezone.utc), sender_func=None)

        # Il reminder deve rimanere 'scheduled' e non 'delivered'
        current = self.store.get(rem["reminder_id"])
        self.assertEqual(current["status"], "scheduled")
        self.assertEqual(current["delivery_status"], "pending")

    def test_reminder_scheduler_loop(self):
        """Un reminder che scade a server attivo viene inviato una volta tramite scheduler."""
        import time

        sent = []
        def test_sender(actor_id, text):
            sent.append((actor_id, text))
            return {"ok": True, "message_id": 123}

        from jarvis_hermes.reminder import ReminderScheduler
        scheduler = ReminderScheduler(self.store, sender_func=test_sender, poll_interval_seconds=0.05)
        self.assertFalse(scheduler.is_running())
        scheduler.start()
        self.assertTrue(scheduler.is_running())

        # Creiamo un reminder che scade quasi subito (0.1s nel futuro)
        due_soon = datetime.now(timezone.utc) + timedelta(milliseconds=100)
        rem = self.store.create(
            actor_id=123456,
            text="Reminder via scheduler",
            due_at=due_soon.isoformat(),
            timezone_str="Europe/Rome",
        )

        # Attendiamo che lo scheduler lo consegni
        start_t = time.time()
        while time.time() - start_t < 2.0 and not sent:
            time.sleep(0.05)

        scheduler.stop()
        self.assertFalse(scheduler.is_running())

        self.assertEqual(len(sent), 1)
        self.assertEqual(sent[0], (123456, "Reminder via scheduler"))
        final = self.store.get(rem["reminder_id"])
        self.assertEqual(final["status"], "fired")
        self.assertEqual(final["delivery_status"], "delivered")

    def test_no_spontaneous_monitoring_without_explicit_request(self):
        """D11: Nessun monitoraggio spontaneo; il database non accetta reminder privi di testo o attore."""
        with self.assertRaises(ReminderError) as ctx:
            self.store.create(
                actor_id=0,
                text="",
                due_at=datetime.now(timezone.utc).isoformat(),
                timezone_str="Europe/Rome",
            )
        self.assertEqual(ctx.exception.code, "INVALID_REQUEST")


if __name__ == "__main__":
    unittest.main()
