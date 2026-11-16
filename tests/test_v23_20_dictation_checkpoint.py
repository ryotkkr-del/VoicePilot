"""P0-2 regressions: stop/relock must block late dictation effects.

Mocks only: never starts a microphone, Chrome, Windows UIA, or real keystrokes.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.control_permit import DesktopControlLocked, DesktopControlPermit
from app.dictation import DictationController, DictationTarget
from app.main import VoicePilotApp
from app.win_actions import WindowsController


class DictationControllerCheckpointTests(unittest.TestCase):
    def make(self, *, chrome=True):
        windows = Mock()
        windows.foreground_hwnd.return_value = 777
        windows.is_chrome_foreground.return_value = chrome
        windows.dictation_probe.return_value = "Edit:1,2,3"
        bridge = Mock()
        bridge.connected = True
        return DictationController(windows, bridge), windows, bridge

    def test_stop_before_start_never_inspects_editor(self):
        controller, windows, bridge = self.make()
        with self.assertRaises(InterruptedError):
            controller.start(checkpoint=lambda: (_ for _ in ()).throw(InterruptedError()))
        windows.foreground_hwnd.assert_not_called()
        bridge.call.assert_not_called()

    def test_browser_stop_at_last_preflight_never_sends_text(self):
        controller, windows, bridge = self.make()
        target = DictationTarget("browser", 777, "dummy-token", 12)
        checks = iter((None, InterruptedError("stopped")))
        def checkpoint():
            outcome = next(checks)
            if outcome:
                raise outcome
        with self.assertRaises(InterruptedError):
            controller.insert(target, "dummy text", checkpoint=checkpoint)
        bridge.call.assert_not_called()

    def test_browser_relock_before_send_never_sends_text(self):
        controller, _, bridge = self.make()
        target = DictationTarget("browser", 777, "dummy-token", 12)
        permit = DesktopControlPermit(clock=lambda: 50)
        permit.arm()
        checks = [0]
        def checkpoint():
            checks[0] += 1
            if checks[0] == 2:
                permit.revoke()
            permit.require()
        with self.assertRaises(DesktopControlLocked):
            controller.insert(target, "dummy", checkpoint=checkpoint)
        bridge.call.assert_not_called()

    def test_windows_per_unit_guard_is_forwarded_not_used_for_legacy_calls(self):
        controller, windows, _ = self.make(chrome=False)
        target = DictationTarget("windows", 777, "Edit:1,2,3")
        permit = DesktopControlPermit(clock=lambda: 50)
        permit.arm()
        controller.insert(target, "abc", checkpoint=permit.require)
        call = windows.dictation_insert.call_args
        self.assertEqual(call.args, ("abc", "Edit:1,2,3"))
        guard = call.kwargs["extra_guard"]
        guard()
        permit.revoke()
        with self.assertRaises(DesktopControlLocked):
            guard()
        windows.dictation_insert.reset_mock()
        controller.insert(target, "abc")
        windows.dictation_insert.assert_called_once_with("abc", "Edit:1,2,3")

    def test_native_transport_rechecks_every_utf16_unit(self):
        # Replace the actual SendInput primitive with a deterministic mock.
        windows = WindowsController.__new__(WindowsController)
        windows.dictation_probe = Mock(return_value="Edit:1")
        windows._dictation_check = Mock()
        delivered = []
        requests = [0]
        def checkpoint():
            requests[0] += 1
            if requests[0] > 1:
                raise InterruptedError("stopped after first unit")
        def fake_send(text, *, on_chunk, chunk_units):
            self.assertEqual(chunk_units, 1)
            for char in text:
                on_chunk()
                delivered.append(char)
        with patch("app.win_actions._send_literal_unicode", side_effect=fake_send):
            with self.assertRaises(InterruptedError):
                windows.dictation_insert("abc", "Edit:1", extra_guard=checkpoint)
        self.assertEqual(delivered, ["a"])
        self.assertEqual(windows._dictation_check.call_count, 2)


class AppDictationCheckpointTests(unittest.TestCase):
    def make(self, *, active=True):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.stop_event = threading.Event()
        app.control_permit = DesktopControlPermit(clock=lambda: 100.0)
        app.control_permit.arm()
        app.dictation_target = DictationTarget("windows", 777, "Edit:1") if active else None
        app.dictation = Mock()
        app.restore_target = Mock()
        app.log = Mock()
        return app

    def test_abort_while_restoring_focus_blocks_insert(self):
        app = self.make()
        app.restore_target.side_effect = app.stop_event.set
        result = app._run_dictation_command("ダミーのテスト")
        self.assertEqual(result.status, "interrupted")
        self.assertIsNone(app.dictation_target)
        app.dictation.insert.assert_not_called()

    def test_permission_revoked_while_restoring_focus_blocks_insert(self):
        app = self.make()
        app.restore_target.side_effect = app.control_permit.revoke
        result = app._run_dictation_command("ダミーのテスト")
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(app.dictation_target)
        app.dictation.insert.assert_not_called()

    def test_stop_during_insert_propagates_into_checkpoint(self):
        app = self.make()
        def cancelled_insert(target, text, *, checkpoint):
            app.stop_event.set()
            checkpoint()
        app.dictation.insert.side_effect = cancelled_insert
        result = app._run_dictation_command("ダミーのテスト")
        self.assertEqual(result.status, "interrupted")
        self.assertIsNone(app.dictation_target)
        self.assertFalse(any("文字を挿入しました" in str(c) for c in app.log.call_args_list))

    def test_stopped_start_never_probes_or_keeps_target(self):
        app = self.make(active=False)
        app.restore_target.side_effect = app.control_permit.revoke
        result = app._run_dictation_command("文章入力開始")
        self.assertEqual(result.status, "incomplete")
        app.dictation.start.assert_not_called()
        self.assertIsNone(app.dictation_target)

    def test_expired_permission_mid_insert_fails_closed(self):
        clock = [100.0]
        app = self.make()
        app.control_permit = DesktopControlPermit(clock=lambda: clock[0])
        app.control_permit.arm()
        def expired_insert(target, text, *, checkpoint):
            clock[0] += 301
            checkpoint()
        app.dictation.insert.side_effect = expired_insert
        result = app._run_dictation_command("ダミーのテスト")
        self.assertEqual(result.status, "incomplete")
        self.assertIn("失効", result.summary)
        self.assertIsNone(app.dictation_target)


if __name__ == "__main__":
    unittest.main()
