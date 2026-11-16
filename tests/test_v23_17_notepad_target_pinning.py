"""P0-1/P0-5: fail-closed scratch Notepad focus and per-unit typing checks.

All tests use mocks and do NOT type, start Notepad, capture screens,
read actual user files, or make network calls.
"""
from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

import windows_safety_check as smoke
from app import win_actions


class ScratchNotepadFocusTests(unittest.TestCase):
    def setUp(self):
        self.token = "VoicePilot_Test_0123456789"
        self.controller = Mock()
        self.controller.foreground_hwnd.return_value = 103
        self.controller._foreground_process_name.return_value = "notepad.exe"
        self.controller.active_window_title.return_value = self.token + ".txt - Notepad"
        self.controller.dictation_probe.return_value = "Document:11,22,33"

    def pin(self):
        return smoke.pin_scratch_notepad_target(self.controller, self.token)

    def test_verified_scratch_document_is_pinned_to_hwnd_and_control(self):
        target = self.pin()
        self.assertEqual(target.hwnd, 103)
        self.assertEqual(target.control, "Document:11,22,33")
        self.assertEqual(target.token, self.token)
        smoke.assert_scratch_notepad_target(self.controller, target)

    def test_wrong_process_is_rejected_before_reading_other_editors(self):
        for process in ("cmd.exe", "chrome.exe", "python.exe", None, "NOTEPAD.EXE"):
            with self.subTest(process=process):
                self.controller._foreground_process_name.return_value = process
                self.controller.dictation_probe.reset_mock()
                with self.assertRaises(PermissionError):
                    self.pin()
                self.controller.dictation_probe.assert_not_called()

    def test_title_spoofed_by_other_process_cannot_authorize(self):
        self.controller._foreground_process_name.return_value = "powershell.exe"
        self.controller.active_window_title.return_value = self.token + ".txt - Notepad"
        with self.assertRaises(PermissionError):
            self.pin()

    def test_window_switch_is_rejected_even_when_control_identity_is_same(self):
        target = self.pin()
        self.controller.foreground_hwnd.return_value = 104
        with self.assertRaisesRegex(PermissionError, "ウィンドウ"):
            smoke.assert_scratch_notepad_target(self.controller, target)

    def test_other_notepad_document_rejected_with_same_hwnd(self):
        target = self.pin()
        self.controller.active_window_title.return_value = "my notes.txt - Notepad"
        with self.assertRaisesRegex(PermissionError, "テスト文書"):
            smoke.assert_scratch_notepad_target(self.controller, target)

    def test_same_hwnd_and_file_but_different_input_control_rejected(self):
        target = self.pin()
        self.controller.dictation_probe.return_value = "Document:100,200"
        with self.assertRaisesRegex(PermissionError, "入力欄"):
            smoke.assert_scratch_notepad_target(self.controller, target)

    def test_missing_hwnd_or_unrelated_token_is_rejected(self):
        self.controller.foreground_hwnd.return_value = 0
        with self.assertRaises(PermissionError):
            self.pin()
        self.controller.foreground_hwnd.return_value = 103
        with self.assertRaises(PermissionError):
            smoke.pin_scratch_notepad_target(self.controller, "private.txt")

    def test_guard_never_includes_personal_title_in_error_message(self):
        target = self.pin()
        secret = "FAKE_SECRET_WINDOW_TITLE_NEVER_LOG_427"
        self.controller.active_window_title.return_value = secret + " - Notepad"
        try:
            smoke.assert_scratch_notepad_target(self.controller, target)
        except PermissionError as exc:
            self.assertNotIn(secret, str(exc))
        else:
            self.fail("title mismatch must be rejected")


class LiteralInputChunkGuardTests(unittest.TestCase):
    def setUp(self):
        self.controller = win_actions.WindowsController.__new__(win_actions.WindowsController)
        self.controller.dictation_probe = Mock(return_value="Edit:1,2,3")
        self.payload = "VoicePilot harmless ^s +{DELETE} %{F4}"

    def test_scratch_extra_guard_runs_before_each_utf16_unit(self):
        guard = Mock()
        checked = []
        def fake_unicode(text, on_chunk=None, *, chunk_units=64):
            self.assertEqual(text, self.payload)
            self.assertEqual(chunk_units, 1)
            for _ in text.encode("utf-16-le")[::2]:
                on_chunk()
                checked.append("sent")
        with patch("app.win_actions._send_literal_unicode", side_effect=fake_unicode) as send:
            self.controller.dictation_insert(
                self.payload, "Edit:1,2,3", extra_guard=guard,
            )
        self.assertEqual(guard.call_count, len(self.payload))
        self.assertEqual(len(checked), len(self.payload))
        self.assertGreaterEqual(self.controller.dictation_probe.call_count,
                                len(self.payload))
        send.assert_called_once()

    def test_focus_switch_refuses_remaining_input_immediately(self):
        old_control = "Edit:1,2,3"
        allowed = []
        def guard():
            if len(allowed) >= 5:
                raise PermissionError("scratch target changed")
        def fake_unicode(text, on_chunk=None, *, chunk_units=64):
            self.assertEqual(chunk_units, 1)
            for char in text:
                on_chunk()
                allowed.append(char)
        with patch("app.win_actions._send_literal_unicode", side_effect=fake_unicode):
            with self.assertRaisesRegex(PermissionError, "changed"):
                self.controller.dictation_insert(
                    self.payload, old_control, extra_guard=guard,
                )
        self.assertEqual(len(allowed), 5)
        self.assertEqual("".join(allowed), self.payload[:5])

    def test_normal_dictation_keeps_64_unit_batching(self):
        with patch("app.win_actions._send_literal_unicode") as send:
            self.controller.dictation_insert("harmless", "Edit:1,2,3")
        self.assertEqual(send.call_args.kwargs["chunk_units"], 64)
        self.assertTrue(callable(send.call_args.kwargs["on_chunk"]))

    def test_invalid_chunk_sizes_rejected_without_sendinput(self):
        for value in (0, -1, 65, None, 1.5, True):
            with self.subTest(value=value), self.assertRaises(ValueError):
                win_actions._send_literal_unicode("a", chunk_units=value)


if __name__ == "__main__":
    unittest.main()
