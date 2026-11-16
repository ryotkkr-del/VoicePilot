"""Roadmap P0-2 and P0-3: deterministic fail-closed and privacy regressions.

All tests use mocks: never run a real desktop operation, connect a microphone,
transmit to cloud providers, modify user files or write private transcripts.
"""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent, RunOutcome
from app.main import VoicePilotApp
from app.verification import Evidence

SECRET = "FAKE_API_KEY_12345_KEEP_PRIVATE"


def agent_for(*, log=None, planner=None, bridge=None, windows=None):
    planner = planner or Mock(provider="ollama")
    bridge = bridge or Mock(connected=False)
    windows = windows or Mock()
    agent = VoiceAgent(bridge, windows, planner, log or Mock(), Mock(return_value=True),
                       False, threading.Event())
    agent._context = Mock(return_value={"windows": {"window_ref": "101", "controls": [],
                                                    "windows": [], "active_title": "Notepad"}})
    return agent, planner, windows


class AmbiguousEffectFailClosedTests(unittest.TestCase):
    def test_timeout_after_effect_never_starts_a_second_effect(self):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            {"action": "windows_launch", "args": {"app": "notepad"}, "message": SECRET},
            {"action": "windows_launch", "args": {"app": "calculator"}, "message": "second"},
        ]
        log = Mock()
        agent, planner, _ = agent_for(log=log, planner=planner)
        agent._execute = Mock(side_effect=TimeoutError("remote timed out: " + SECRET))
        outcome = agent.run("アプリの準備をしてください")
        self.assertEqual(outcome.status, "unverified")
        self.assertIn("再実行せず", outcome.summary)
        self.assertEqual(agent._execute.call_count, 1)
        self.assertEqual(planner.next_action.call_count, 1)
        self.assertNotIn(SECRET, str(log.call_args_list))

    def test_rejected_effect_stops_without_retry(self):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            {"action": "windows_launch", "args": {"app": "notepad"}, "message": "first"},
            {"action": "windows_launch", "args": {"app": "calculator"}, "message": "second"},
        ]
        agent, planner, _ = agent_for(planner=planner)
        agent._execute = Mock(side_effect=PermissionError("denied: " + SECRET))
        outcome = agent.run("作業環境の準備をしてください")
        self.assertEqual(outcome.status, "incomplete")
        agent._execute.assert_called_once()
        self.assertEqual(planner.next_action.call_count, 1)

    def test_cancel_after_action_does_not_plan_another_one(self):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            {"action": "windows_launch", "args": {"app": "notepad"}, "message": "first"},
            {"action": "windows_launch", "args": {"app": "calculator"}, "message": "second"},
        ]
        agent, planner, _ = agent_for(planner=planner)
        def interrupted(*_):
            agent.stop_event.set()
            return "action may have finished"
        agent._execute = Mock(side_effect=interrupted)
        with patch("app.agent.assess", return_value=Evidence("unverified", "unknown")):
            outcome = agent.run("アプリの準備をしてください")
        self.assertEqual(outcome.status, "interrupted")
        self.assertEqual(planner.next_action.call_count, 1)
        agent._execute.assert_called_once()


class PrivacyRegressions(unittest.TestCase):
    def test_page_and_window_exception_messages_do_not_enter_ai_context(self):
        bridge = Mock(connected=True)
        bridge.call.side_effect = TimeoutError("page error: " + SECRET)
        windows = Mock()
        windows.is_chrome_foreground.return_value = True
        windows.snapshot.side_effect = RuntimeError("Windows private title " + SECRET)
        agent = VoiceAgent(bridge, windows, Mock(provider="ollama"), Mock(),
                           Mock(), False, threading.Event())
        context = agent._context()
        self.assertNotIn(SECRET, str(context))
        self.assertIn("browser_error", context)
        self.assertIn("windows_error", context)

    def test_model_message_and_tool_result_are_never_logged_or_reprompted(self):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            {"action": "windows_launch", "args": {"app": "notepad"}, "message": SECRET},
            {"action": "done", "args": {}, "message": SECRET},
        ]
        log = Mock()
        agent, planner, _ = agent_for(log=log, planner=planner)
        agent._execute = Mock(return_value={"result": SECRET})
        with patch("app.agent.assess", return_value=Evidence("verified", "checked")):
            outcome = agent.run("アプリを準備してください")
        self.assertEqual(outcome.status, "completed")
        self.assertNotIn(SECRET, str(log.call_args_list))
        second_history = planner.next_action.call_args_list[1].args[2]
        self.assertNotIn(SECRET, str(second_history))
        self.assertEqual(second_history[0]["verification"], "verified")

    def test_model_done_cannot_echo_secret_into_operation_log(self):
        planner = Mock(provider="ollama")
        planner.next_action.return_value = {"action": "done", "args": {}, "message": SECRET}
        log = Mock()
        agent, _, _ = agent_for(log=log, planner=planner)
        result = agent.run("画面の内容を調べてください")
        self.assertEqual(result.status, "incomplete")
        self.assertNotIn(SECRET, str(log.call_args_list))
        self.assertNotIn(SECRET, result.summary)

    def test_text_ui_does_not_log_submitted_instruction(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()
        app.busy = False
        app.recording = False
        app.command = Mock()
        app.command.get.return_value = "検索して " + SECRET
        app.log = Mock()
        app.stop_event = threading.Event()
        app.runtime_settings = Mock(return_value={"whisper_model": "small"})
        app.dictation_target = None
        app.set_phase = Mock()
        with patch("app.main.threading.Thread") as worker:
            app.send_text()
        worker.assert_called_once()
        self.assertNotIn(SECRET, str(app.log.call_args_list))

    def test_voice_ui_does_not_log_recognized_instruction(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.recognizer = Mock(model_name="small")
        app.recognizer.transcribe.return_value = "検索して " + SECRET
        app.log = Mock()
        app.show_transcript = Mock()
        app.mark_idle = Mock()
        app._speak_result = Mock()
        app.stop_event = threading.Event()
        app.dictation_target = None
        app._run_agent = Mock(return_value=RunOutcome("completed", "complete"))
        app.transcribe_then_execute(samples=Mock(), runtime={"whisper_model": "small"})
        app._run_agent.assert_called_once()
        app.show_transcript.assert_called_once()
        self.assertNotIn(SECRET, str(app.log.call_args_list))


if __name__ == "__main__":
    unittest.main()
