"""v0.22 regression and real Windows *scratch-only* safety checks.

Never clicks the desktop, synthesizes keys or writes to actual user folders.
The Windows CI runner executes tests against a real filesystem using only a
TemporaryDirectory; subprocesses and UI mutators are never invoked.
"""
from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from app.main import main
from app.screen_understanding import ScreenAssistant
from app.win_actions import (WindowsController, current_process_elevated,
                             move_file, rename_file)


class IdentityGuardTests(unittest.TestCase):
    def setUp(self):
        self.controller = WindowsController.__new__(WindowsController)
        self.controller.active_window_title = Mock(return_value="VoicePilot - Google Chrome")
        self.controller._foreground_process_name = Mock(return_value="cmd.exe")

    def test_spoofed_window_title_does_not_impersonate_chrome(self):
        self.assertFalse(self.controller.is_chrome_foreground())
        self.controller._foreground_process_name.return_value = None
        self.assertFalse(self.controller.is_chrome_foreground())
        self.controller._foreground_process_name.return_value = "chrome.exe"
        self.assertTrue(self.controller.is_chrome_foreground())
        self.controller.active_window_title.assert_not_called()

    def test_unsafe_screen_observation_is_blocked(self):
        with patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.os.name", "nt"):
            with self.assertRaises(PermissionError):
                self.controller.assert_safe_observation()
        self.controller._foreground_process_name.return_value = "notepad.exe"
        with patch("app.win_actions.current_process_elevated", return_value=False), \
             patch("app.win_actions.os.name", "nt"):
            self.controller.assert_safe_observation()

    def test_elevated_input_is_denied_even_in_notepad(self):
        self.controller._foreground_process_name.return_value = "notepad.exe"
        with patch("app.win_actions.current_process_elevated", return_value=True):
            with self.assertRaisesRegex(ValueError, "管理者権限"):
                self.controller._ensure_not_terminal()

    def test_unknown_windows_elevation_fails_closed(self):
        # If WinAPI privilege inspection fails, don't assume standard-user.
        with patch("app.win_actions.os.name", "nt"), patch("app.win_actions.ctypes.windll", create=True) as windll:
            windll.shell32.IsUserAnAdmin.side_effect = OSError("access denied")
            self.assertTrue(current_process_elevated())

    def test_non_windows_is_not_elevated(self):
        with patch("app.win_actions.os.name", "posix"):
            self.assertFalse(current_process_elevated())

    def test_main_denies_elevated_start_before_gui(self):
        with patch("app.main.sys.platform", "win32"), \
             patch("app.main.current_process_elevated", return_value=True), \
             patch("app.main.tk.Tk") as root:
            with self.assertRaises(SystemExit):
                main()
            root.assert_not_called()

    def test_readonly_screen_question_does_not_forward_blocked_view(self):
        windows = Mock(spec=WindowsController)
        windows.assert_safe_observation.side_effect = PermissionError("not safe")
        planner = Mock(provider="ollama")
        bridge = Mock(connected=False)
        assistant = ScreenAssistant(
            bridge=bridge, windows=windows, planner=planner,
            context_provider=Mock(), stop_event=threading.Event(),
            allow_cloud_ui=False, allow_cloud_image=False)
        with self.assertRaises(PermissionError):
            assistant.answer("この画面を説明して")
        assistant.context_provider.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_readonly_screenshot_is_discarded_after_focus_changes(self):
        windows = Mock(spec=WindowsController)
        windows.foreground_hwnd.side_effect = [100, 200]
        planner = Mock(provider="ollama")
        assistant = ScreenAssistant(
            bridge=Mock(connected=False), windows=windows, planner=planner,
            context_provider=Mock(return_value={"windows": {"active_title": "scratch"}}),
            stop_event=threading.Event(), allow_cloud_ui=False,
            allow_cloud_image=False)
        with patch("app.screen_understanding.capture_screen") as capture:
            capture.return_value.jpeg = b"mock image"
            with self.assertRaises(PermissionError):
                assistant.answer("この画面を説明して")
            capture.assert_called_once_with(100)
        planner.explain_screen.assert_not_called()

    def test_agent_discards_image_if_window_switches(self):
        windows = Mock(spec=WindowsController)
        windows.foreground_hwnd.side_effect = [100, 200]
        planner = Mock(provider="ollama")
        planner.next_action.return_value = {"action": "screen_observe", "args": {}, "message": "inspect"}
        agent = VoiceAgent(Mock(connected=False), windows, planner, Mock(),
                           Mock(return_value=True), False, threading.Event())
        agent._context = Mock(return_value={})
        with patch("app.agent.capture_screen") as capture:
            capture.return_value.jpeg = b"mock image"
            result = agent.run("画面を見て")
        self.assertEqual(result.status, "incomplete")
        self.assertIsNone(agent._frame)
        self.assertEqual(planner.next_action.call_count, 1)

    def test_agent_rejects_screenshot_before_capture(self):
        windows = Mock(spec=WindowsController)
        windows.assert_safe_observation.side_effect = PermissionError("not safe")
        planner = Mock(provider="ollama")
        planner.next_action.return_value = {"action": "screen_observe", "args": {}, "message": "see"}
        agent = VoiceAgent(Mock(connected=False), windows, planner, Mock(), Mock(return_value=True),
                           False, threading.Event())
        agent._context = Mock(return_value={})
        with patch("app.agent.capture_screen") as capture:
            # PermissionError is caught by the agent's safe incomplete/error path
            # or propagated, but must NEVER cause a screenshot.
            try:
                agent.run("今の画面を見て")
            except PermissionError:
                pass
        capture.assert_not_called()


class RealFilesystemSandboxTests(unittest.TestCase):
    def test_rename_move_reject_overwrite_and_external_folder(self):
        # On Windows CI these ARE real NTFS renames, not mocked filesystem I/O.
        # Only the scope of allowed roots is substituted with private temp dirs.
        with tempfile.TemporaryDirectory(prefix="VoicePilotSafety-") as folder:
            root = Path(folder)
            documents, downloads = root / "Documents", root / "Downloads"
            documents.mkdir(); downloads.mkdir()
            src = documents / "original.txt"
            src.write_text("SAFE-DUMMY", encoding="utf-8")
            with patch("app.win_actions.user_roots", return_value=[documents, downloads]):
                rename_file(str(src), "renamed.txt")
                src = documents / "renamed.txt"
                self.assertEqual(src.read_text(encoding="utf-8"), "SAFE-DUMMY")
                collision = downloads / src.name
                collision.write_text("DO-NOT-OVERWRITE", encoding="utf-8")
                with self.assertRaises(FileExistsError):
                    move_file(str(src), "downloads")
                self.assertEqual(collision.read_text(encoding="utf-8"), "DO-NOT-OVERWRITE")
                collision.unlink()
                move_file(str(src), "downloads")
                self.assertFalse(src.exists())
                self.assertEqual(collision.read_text(encoding="utf-8"), "SAFE-DUMMY")
                with self.assertRaises(ValueError):
                    rename_file(str(collision), "changed.exe")
                external = root / "external.txt"
                external.write_text("DO-NOT-TOUCH", encoding="utf-8")
                with self.assertRaises(ValueError):
                    rename_file(str(external), "untrusted.txt")
                self.assertEqual(external.read_text(encoding="utf-8"), "DO-NOT-TOUCH")

    @unittest.skipUnless(os.name == "nt", "Windows Win32 smoke only")
    def test_windows_read_only_process_inspection_never_sends_input(self):
        # Windows CI may have no interactive desktop. Unknown foreground must
        # simply report None and never trigger a click/key/shell process.
        windows = WindowsController()
        name = windows._foreground_process_name()
        self.assertTrue(name is None or (isinstance(name, str) and name.endswith('.exe')))
        self.assertIsInstance(windows.is_chrome_foreground(), bool)


if __name__ == "__main__":
    unittest.main()