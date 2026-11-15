"""v0.23 Windows privacy safety and explicitly consented microphone diagnostics.

All tests are mocked: no live microphone, desktop input, personal files or
cloud communication. These regressions run on both Linux and Windows CI.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.screen_understanding import ScreenAssistant
from app.win_actions import WindowsController

sys.path.insert(0, str(Path(__file__).parent))
import windows_safety_check as diag


class ProtectedWindowDisclosureTests(unittest.TestCase):
    def controller(self, title="Private terminal 1234", exe="cmd.exe"):
        c = WindowsController.__new__(WindowsController)
        c._foreground_hwnd = Mock(return_value=101)
        c.active_window_title = Mock(return_value=title)
        c._foreground_process_name = Mock(return_value=exe)
        c.is_chrome_foreground = Mock(return_value=False)
        c.allow_chrome_uia_fallback = False
        return c

    def test_terminal_snapshot_exposes_no_window_titles_or_controls(self):
        c = self.controller()
        with patch("app.win_actions.os.name", "nt"), \
             patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.visible_window_titles") as enumeration:
            snapshot = c.snapshot()
        self.assertEqual(snapshot, {
            "windows": [], "active_title": "", "controls": [], "window_ref": ""})
        enumeration.assert_not_called()
        self.assertEqual(c._visible_controls, [])
        self.assertIsNone(c._last_control_window)

    def test_unknown_process_snapshot_is_also_blank(self):
        c = self.controller(title="secret file 8675309", exe=None)
        with patch("app.win_actions.os.name", "nt"), \
             patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.visible_window_titles") as enumeration:
            snapshot = c.snapshot()
        self.assertNotIn("secret", str(snapshot))
        enumeration.assert_not_called()

    def test_elevated_snapshot_is_blank_even_in_safe_app(self):
        c = self.controller(title="My sensitive notes", exe="notepad.exe")
        with patch("app.win_actions.os.name", "nt"), \
             patch("app.win_actions.current_process_elevated", return_value=True), \
             patch("app.win_actions.visible_window_titles") as enumeration:
            snapshot = c.snapshot()
        self.assertEqual(snapshot["windows"], [])
        self.assertEqual(snapshot["active_title"], "")
        enumeration.assert_not_called()

    def test_protected_quick_state_cannot_leak_window_titles(self):
        c = self.controller()
        with patch("app.win_actions.os.name", "nt"), \
             patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.visible_window_titles") as enumeration:
            state = c.quick_state()
        self.assertEqual(state, {"active_title": "", "windows": []})
        enumeration.assert_not_called()

    def test_focus_shift_discards_partly_collected_accessibility(self):
        c = self.controller(title="Notepad", exe="notepad.exe")
        c._foreground_hwnd.side_effect = [101, 202]
        active = Mock()
        active.children.return_value = [Mock(
            window_text=Mock(return_value="Secret untrusted label"),
            is_visible=Mock(return_value=True),
            element_info=SimpleNamespace(control_type="Button"),
        )]
        fake_module = SimpleNamespace(Desktop=Mock(return_value=Mock(window=Mock(return_value=active))))
        with patch("app.win_actions.os.name", "nt"), \
             patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.visible_window_titles", return_value=[{"title": "Secret other app"}]), \
             patch.dict(sys.modules, {"pywinauto": fake_module}):
            snapshot = c.snapshot()
        self.assertEqual(snapshot["windows"], [])
        self.assertEqual(snapshot["controls"], [])
        self.assertEqual(c._visible_controls, [])
        self.assertIsNone(c._last_control_window)

    def test_text_only_cloud_screen_question_stops_after_focus_switch(self):
        windows = Mock(spec=WindowsController)
        windows.foreground_hwnd.side_effect = [111, 222]
        planner = Mock(provider="gemini")
        screen = ScreenAssistant(
            windows=windows, bridge=Mock(connected=False), planner=planner,
            context_provider=Mock(return_value={"windows": {"active_title": "Secret"}}),
            stop_event=threading.Event(), allow_cloud_ui=True,
            allow_cloud_image=False,
        )
        with self.assertRaises(PermissionError):
            screen.answer("画面を説明して")
        planner.explain_screen.assert_not_called()


class BackgroundBrowserGuardTests(unittest.TestCase):
    def test_browser_mutations_refuse_background_chrome(self):
        from app.agent import VoiceAgent
        bridge = Mock(connected=True)
        windows = Mock(spec=WindowsController)
        windows.is_chrome_foreground.return_value = False
        agent = VoiceAgent(bridge, windows, Mock(provider="ollama"), Mock(),
                           Mock(return_value=True), False, threading.Event())
        for action, args in (
            ("browser_click", {"id": "target"}),
            ("browser_fill", {"id": "field", "text": "test"}),
            ("browser_scroll", {"direction": "down"}),
            ("browser_back", {}),
            ("browser_switch_tab", {"title": "private tab"}),
        ):
            with self.subTest(action=action), self.assertRaises(RuntimeError):
                agent._execute(action, args)
        bridge.call.assert_not_called()

    def test_explicit_browser_navigation_can_still_open_url(self):
        from app.agent import VoiceAgent
        bridge = Mock(connected=True)
        windows = Mock(spec=WindowsController)
        windows.is_chrome_foreground.return_value = False
        agent = VoiceAgent(bridge, windows, Mock(provider="ollama"), Mock(),
                           Mock(return_value=True), False, threading.Event())
        agent._execute("browser_open", {"url": "https://example.org"})
        bridge.call.assert_called_once()


class OptionalMicrophoneTests(unittest.TestCase):
    def test_scratch_phrase_comparison_does_not_require_model(self):
        self.assertTrue(diag._voice_phrase_matches("音声のテストです。"))
        self.assertTrue(diag._voice_phrase_matches("音声テストです"))
        self.assertFalse(diag._voice_phrase_matches("やめて"))
        self.assertFalse(diag._voice_phrase_matches("音声のテストです、明日も"))

    def test_non_windows_microphone_is_never_started(self):
        with patch.object(diag.os, "name", "posix"), patch("builtins.input") as ask:
            self.assertEqual(diag.interactive_microphone(), "SKIP:Windows only")
        ask.assert_not_called()

    def test_no_consent_does_not_start_microphone(self):
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=False), \
             patch("builtins.input", return_value="no"), \
             patch("app.audio.Microphone") as mic:
            outcome = diag.interactive_microphone()
        self.assertEqual(outcome, "SKIP:no consent")
        mic.assert_not_called()

    def test_admin_denied_before_consent_or_microphone(self):
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=True), \
             patch("builtins.input") as ask, \
             patch("app.audio.Microphone") as mic:
            outcome = diag.interactive_microphone()
        self.assertEqual(outcome, "FAIL:admin execution")
        ask.assert_not_called(); mic.assert_not_called()

    def test_spoken_test_phrase_is_recognized_without_actions_or_transcript_output(self):
        mic = Mock()
        mic.activity.return_value = {"heard_voice": True, "silence": 1.8}
        mic.stop.return_value = SimpleNamespace(size=32000)
        asr = Mock()
        asr.transcribe.return_value = "音声のテストです。"
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=False), \
             patch("builtins.input", return_value="YES"), \
             patch("app.audio.Microphone", return_value=mic), \
             patch("app.audio.SpeechRecognizer", return_value=asr), \
             patch.object(diag.time, "monotonic", side_effect=[0, 2.0, 2.01, 3.0, 4.0]), \
             patch.object(diag.time, "sleep"), \
             patch("builtins.print") as printed:
            result = diag.interactive_microphone()
        self.assertEqual(result, "PASS")
        asr.transcribe.assert_called_once()
        output = " ".join(str(x) for call in printed.call_args_list for x in call.args)
        self.assertNotIn("音声のテストです。", output)  # Phrase is printed as instruction but without punctuation.
        self.assertNotIn("^s", output)
        mic.start.assert_called_once(); mic.stop.assert_called_once()


if __name__ == "__main__":
    unittest.main()