"""Regression tests for confirmation parity in deterministic hotkeys.

All Windows actions are mocked. These tests never interact with the real
clipboard, active window, keyboard, microphone or files.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from app.verification import Evidence


class FastHotkeyApprovalTests(unittest.TestCase):
    def make_agent(self, confirm):
        planner = Mock(provider="ollama")
        agent = VoiceAgent(
            bridge=Mock(connected=False),
            windows=Mock(),
            planner=planner,
            log=Mock(),
            confirm=confirm,
            allow_cloud_ui=False,
            stop_event=threading.Event(),
        )
        agent._execute = Mock(return_value={"ok": True})
        return agent, planner

    def test_fast_undo_requires_approval_and_refusal_prevents_input(self):
        confirm = Mock(return_value=False)
        agent, planner = self.make_agent(confirm)
        result = agent.run("アプリ内の編集を元に戻して")
        self.assertEqual(result.status, "interrupted")
        self.assertIn("元に戻しますか", confirm.call_args.args[0])
        agent._execute.assert_not_called()
        planner.next_action.assert_not_called()

    def test_fast_undo_approved_is_exactly_one_hotkey(self):
        confirm = Mock(return_value=True)
        agent, planner = self.make_agent(confirm)
        with patch("app.agent.assess", return_value=Evidence("verified", "確認済み")):
            result = agent.run("このアプリの編集を取り消して")
        self.assertEqual(result.status, "completed")
        confirm.assert_called_once()
        agent._execute.assert_called_once_with("windows_hotkey", {"keys": "ctrl+z"})
        planner.next_action.assert_not_called()

    def test_cancellation_while_approval_dialog_is_open_stops_undo(self):
        agent, _ = self.make_agent(None)
        def confirm_and_stop(_):
            agent.stop_event.set()
            return True
        agent.confirm = confirm_and_stop
        result = agent.run("アプリ内の編集を元に戻して")
        self.assertEqual(result.status, "interrupted")
        agent._execute.assert_not_called()

    def test_paste_still_requires_approval(self):
        confirm = Mock(return_value=False)
        agent, planner = self.make_agent(confirm)
        self.assertEqual(agent.run("貼り付けて").status, "interrupted")
        self.assertIn("貼り付けますか", confirm.call_args.args[0])
        agent._execute.assert_not_called()
        planner.next_action.assert_not_called()

    def test_copy_does_not_gain_an_unnecessary_prompt(self):
        confirm = Mock(return_value=False)
        agent, _ = self.make_agent(confirm)
        with patch("app.agent.assess", return_value=Evidence("verified", "確認済み")):
            result = agent.run("コピーして")
        self.assertEqual(result.status, "completed")
        confirm.assert_not_called()
        agent._execute.assert_called_once_with("windows_hotkey", {"keys": "ctrl+c"})


if __name__ == "__main__":
    unittest.main()
