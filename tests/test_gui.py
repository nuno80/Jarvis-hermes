import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

from jarvis_hermes.gui import (
    GuiAutomationManager,
    GuiError,
    GuiSessionState,
)


class GuiAutomationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp(prefix="jarvis-gui-tests-")
        self.state_dir = Path(self.temp_dir) / "state"
        self.screenshots_dir = self.state_dir / "screenshots"
        self.manager = GuiAutomationManager(
            screenshots_dir=self.screenshots_dir,
            lock_path=self.state_dir / "gui_job.lock",
        )

    def tearDown(self):
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_desktop_locked_or_unavailable_fails_explicitly(self):
        """If desktop is locked (AT10), raise DESKTOP_UNAVAILABLE; never report fictitious success."""
        with patch.object(self.manager, "check_desktop_interactive", return_value=(False, "DESKTOP_LOCKED")):
            with self.assertRaises(GuiError) as cm:
                self.manager.execute_gui_action(
                    app_name="notepad",
                    action="open_and_inspect",
                )
            self.assertEqual(cm.exception.code, "DESKTOP_UNAVAILABLE")
            self.assertIn("DESKTOP_LOCKED", cm.exception.message)

    def test_single_concurrent_gui_job_enforced(self):
        """Only one GUI job can execute at any time; concurrent attempts fail with CONCURRENT_GUI_JOB."""
        # Hold the lock
        lock_file = self.state_dir / "gui_job.lock"
        lock_file.parent.mkdir(parents=True, exist_ok=True)
        lock_file.touch()

        with patch.object(self.manager, "check_desktop_interactive", return_value=(True, "DESKTOP_INTERACTIVE")):
            with patch.object(self.manager, "_try_acquire_lock", return_value=False):
                with self.assertRaises(GuiError) as cm:
                    self.manager.execute_gui_action(app_name="notepad", action="open_and_inspect")
                self.assertEqual(cm.exception.code, "CONCURRENT_GUI_JOB")

    def test_disallow_elevated_execution(self):
        """Applications requiring admin/elevation or system paths are rejected."""
        dangerous_apps = ["powershell.exe", "cmd.exe", "regedit.exe", "mmc.exe", "bash.exe"]
        for app in dangerous_apps:
            with self.assertRaises(GuiError) as cm:
                self.manager.execute_gui_action(app_name=app, action="open_and_inspect")
            self.assertEqual(cm.exception.code, "PERMISSION_DENIED")

    def test_unregistered_app_rejected(self):
        """Only registered non-elevated applications can be launched."""
        with self.assertRaises(GuiError) as cm:
            self.manager.execute_gui_action(app_name="malicious_downloader.exe", action="open_and_inspect")
        self.assertEqual(cm.exception.code, "PERMISSION_DENIED")

    def test_unregistered_action_rejected(self):
        """Only benign observable actions are allowed."""
        with self.assertRaises(GuiError) as cm:
            self.manager.execute_gui_action(app_name="notepad", action="send_external_http_payload")
        self.assertEqual(cm.exception.code, "ACTION_NOT_PERMITTED")

    def test_benign_gui_action_produces_before_after_screenshots(self):
        """Benign observable GUI action produces before & after screenshots and reports observed state."""
        with patch.object(self.manager, "check_desktop_interactive", return_value=(True, "DESKTOP_INTERACTIVE")), \
             patch.object(self.manager, "_take_screenshot") as mock_screen, \
             patch.object(self.manager, "_launch_and_interact") as mock_interact:
            mock_screen.side_effect = ["/state/screenshots/before_1.png", "/state/screenshots/after_1.png"]
            mock_interact.return_value = {
                "window_title": "Untitled - Notepad",
                "process_id": 9999,
                "action_performed": "open_and_inspect",
                "observed_state": "window_opened_ready",
            }

            result = self.manager.execute_gui_action(
                app_name="notepad",
                action="open_and_inspect",
            )

            self.assertTrue(result["ok"])
            self.assertEqual(result["app_name"], "notepad")
            self.assertEqual(result["screenshots"]["before"], "/state/screenshots/before_1.png")
            self.assertEqual(result["screenshots"]["after"], "/state/screenshots/after_1.png")
            self.assertEqual(result["interaction"]["observed_state"], "window_opened_ready")

    def test_desktop_status_inspection(self):
        """Desktop status reports interactive vs locked without running any action."""
        with patch.object(self.manager, "check_desktop_interactive", return_value=(True, "DESKTOP_INTERACTIVE")), \
             patch.object(self.manager, "_get_active_window_info", return_value={"title": "Desktop", "process": "explorer.exe"}):
            status = self.manager.get_gui_status()
            self.assertEqual(status["session_state"], "interactive")
            self.assertEqual(status["active_window"]["title"], "Desktop")
