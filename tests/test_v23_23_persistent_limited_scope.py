"""Regression tests for the explicit, persistent browser/file-only scope."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from app.control_permit import DesktopControlLocked, DesktopControlPermit
from app.main import VoicePilotApp


class PersistentLimitedScopeTests(unittest.TestCase):
    def setUp(self):
        self.permit = DesktopControlPermit(clock=lambda: 100.0)
        self.bridge = Mock()
        self.bridge.connected = True
        self.windows = Mock()
        self.windows.is_chrome_foreground.return_value = True
        self.planner = Mock(provider="ollama")
        self.agent = VoiceAgent(
            self.bridge, self.windows, self.planner, log=Mock(),
            confirm=Mock(return_value=True), allow_cloud_ui=False,
            stop_event=threading.Event(), control_permit=self.permit,
            persistent_browser_file_access=True,
        )

    def test_locked_permanent_scope_allows_only_existing_chrome_action(self):
        self.bridge.call.return_value = {"ok": True}
        self.assertEqual(
            self.agent._execute("browser_click", {"id": "safe-button"}),
            {"ok": True},
        )
        self.bridge.call.assert_called_once_with("browser_click", {"id": "safe-button"})

    def test_locked_permanent_scope_allows_bounded_file_search(self):
        with patch("app.agent.find_files", return_value=["C:/Users/me/Downloads/readme.txt"]):
            result = self.agent._execute("files_find", {"query": "readme"})
        self.assertEqual(result["matches"], ["C:/Users/me/Downloads/readme.txt"])

    def test_everything_outside_scope_still_requires_temporary_permit(self):
        for action, args in (
            ("browser_open", {"url": "https://example.com/"}),
            ("windows_launch", {"app": "notepad"}),
            ("windows_type", {"text": "hello"}),
            ("screen_click", {"x": "1", "y": "1", "target": "OK"}),
        ):
            with self.subTest(action=action), self.assertRaises(DesktopControlLocked):
                self.agent._execute(action, args)
        self.bridge.call.assert_not_called()
        self.windows.act.assert_not_called()

    def test_screen_capture_cannot_start_under_persistent_scope(self):
        self.agent._context = Mock(return_value={})
        self.planner.next_action.return_value = {
            "action": "screen_observe", "args": {}, "message": "",
        }
        with patch("app.agent.capture_screen") as capture:
            outcome = self.agent.run("今の画面を見て")
        self.assertEqual(outcome.status, "incomplete")
        self.assertIn("5分間", outcome.summary)
        capture.assert_not_called()

    def test_file_move_and_rename_keep_individual_confirmation(self):
        self.assertTrue(self.agent._requires_confirmation(
            "files_move", {"path": "C:/Users/me/Downloads/a.txt", "folder": "documents"}, {},
        ))
        self.assertTrue(self.agent._requires_confirmation(
            "files_rename", {"path": "C:/Users/me/Downloads/a.txt", "new_name": "b.txt"}, {},
        ))


class PersistentScopeUIBoundaryTests(unittest.TestCase):
    def test_only_text_commands_may_reach_the_limited_scope_without_timer(self):
        view = VoicePilotApp.__new__(VoicePilotApp)
        view.control_permit = DesktopControlPermit(clock=lambda: 100.0)
        view.config = {"persistent_browser_file_access": True}
        view.log = Mock()
        view._refresh_control_lock_ui = Mock()

        self.assertFalse(view._control_locked(allow_persistent_browser_file=True))
        self.assertTrue(view._control_locked())

    def test_agent_receives_persisted_scope_but_not_a_broader_permit(self):
        view = VoicePilotApp.__new__(VoicePilotApp)
        view.control_permit = DesktopControlPermit(clock=lambda: 100.0)
        view.config = {"persistent_browser_file_access": True}
        view.handsfree = Mock(enabled=False)
        view.recognizer = None
        view.set_phase = Mock()
        view.restore_target = Mock()
        view.bridge = Mock()
        view.windows = Mock()
        view.confirm = Mock()
        view.stop_event = threading.Event()
        view.session = {}
        with patch("app.main.Planner"), patch("app.main.VoiceAgent") as agent:
            agent.return_value.run.return_value = Mock(status="incomplete")
            view._run_agent("スクロールして", {
                "provider": "ollama", "key": "", "gemini_model": "gemini",
                "ollama_model": "local", "allow_cloud_ui": False,
                "allow_cloud_image": False, "persistent_browser_file_access": True,
            })
        self.assertIs(agent.call_args.kwargs["control_permit"], view.control_permit)
        self.assertTrue(agent.call_args.kwargs["persistent_browser_file_access"])


if __name__ == "__main__":
    unittest.main()
