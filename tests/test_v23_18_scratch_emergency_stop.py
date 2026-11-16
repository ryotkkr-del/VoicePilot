"""v0.23.18: opt-in scratch Notepad Escape/timeout stop regressions.

All test input is mocked. No real keyboard, desktop, microphone or personal
files are touched, and no global keyboard hook is installed.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import windows_safety_check as smoke
from app import win_actions


class ScratchStopRulesTests(unittest.TestCase):
    def test_unpressed_escape_and_short_elapsed_time_are_allowed(self):
        calls = Mock(return_value=False)
        smoke.assert_scratch_input_active(100.0, key_down=calls,
                                          now=lambda: 100.05)
        calls.assert_called_once()

    def test_physical_escape_stops_without_rechecking_input_target(self):
        target = smoke.ScratchNotepadTarget(
            90, "VoicePilot_Test_0000000001", "Document:1,2,3")
        controller = Mock()
        with patch.object(smoke, "_physical_escape_down", return_value=True):
            with self.assertRaisesRegex(InterruptedError, "Esc"):
                smoke.guard_scratch_input(controller, target, 0.0)
        controller.foreground_hwnd.assert_not_called()
        controller.dictation_probe.assert_not_called()

    def test_timeout_prevents_further_user_input_without_key_probe(self):
        pressed = Mock(return_value=False)
        with self.assertRaisesRegex(InterruptedError, "制限時間"):
            smoke.assert_scratch_input_active(
                10.0, key_down=pressed,
                now=lambda: 10.0 + smoke.SCRATCH_INPUT_TIMEOUT_SECONDS + 0.01
            )
        pressed.assert_not_called()

    def test_clock_anomaly_fails_closed(self):
        pressed = Mock(return_value=False)
        with self.assertRaises(InterruptedError):
            smoke.assert_scratch_input_active(
                12.0, key_down=pressed, now=lambda: 11.0
            )
        pressed.assert_not_called()

    def test_unknown_key_state_fails_closed(self):
        def inaccessible():
            raise PermissionError("keyboard state denied")
        with self.assertRaises(PermissionError):
            smoke.assert_scratch_input_active(
                0.0, key_down=inaccessible, now=lambda: 1.0
            )

    def test_pressed_escape_refuses_input_one_unit_before_send(self):
        controller = win_actions.WindowsController.__new__(win_actions.WindowsController)
        controller.dictation_probe = Mock(return_value="Edit:1,2,3")
        typed = []
        # Model a keyboard key transition after 4 harmless UTF-16 code units.
        def mock_escape():
            return len(typed) >= 4
        target = smoke.ScratchNotepadTarget(
            90, "VoicePilot_Test_0123456789", "Edit:1,2,3")
        fake_controller = Mock()
        fake_controller.foreground_hwnd.return_value = 90
        fake_controller._foreground_process_name.return_value = "notepad.exe"
        fake_controller.active_window_title.return_value = target.token + ".txt - Notepad"
        fake_controller.dictation_probe.return_value = target.control

        def fake_send(text, on_chunk=None, *, chunk_units=64):
            self.assertEqual(chunk_units, 1)
            for character in text:
                on_chunk()
                typed.append(character)

        with patch("app.win_actions._send_literal_unicode", side_effect=fake_send), \
                patch.object(smoke, "_physical_escape_down", side_effect=mock_escape):
            with self.assertRaisesRegex(InterruptedError, "Esc"):
                controller.dictation_insert(
                    "ABCDEFGH", target.control,
                    extra_guard=lambda: smoke.guard_scratch_input(
                        fake_controller, target, 0.0
                    ),
                )
        self.assertEqual("".join(typed), "ABCD")
        self.assertEqual(fake_controller.foreground_hwnd.call_count, 4)

    def test_guard_detects_focus_change_if_escape_not_held(self):
        target = smoke.ScratchNotepadTarget(
            12, "VoicePilot_Test_0000000002", "Document:1,2,3"
        )
        controller = Mock()
        controller.foreground_hwnd.return_value = 777
        with patch.object(smoke, "_physical_escape_down", return_value=False):
            with self.assertRaisesRegex(PermissionError, "ウィンドウ"):
                smoke.guard_scratch_input(controller, target, 0.0)

    def test_diagnostic_never_installs_os_wide_keyboard_hook(self):
        from pathlib import Path
        content = Path(smoke.__file__).read_text("utf-8")
        self.assertIn("GetAsyncKeyState", content)
        self.assertNotIn("SetWindowsHookEx", content)
        self.assertNotIn("keyboard.hook", content)


if __name__ == "__main__":
    unittest.main()
