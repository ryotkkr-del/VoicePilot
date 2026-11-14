"""Accuracy regressions, runnable offline without a microphone or Windows."""
import threading
import unittest
import types
import sys
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import Mock

import numpy as np

from app.agent import VoiceAgent
from app.audio import SpeechRecognizer


class RecognitionAccuracy(unittest.TestCase):
    def test_near_silence_does_not_run_whisper(self):
        recognizer = SpeechRecognizer("small")
        recognizer.model = Mock()
        result = recognizer.transcribe(np.zeros(16000, dtype=np.float32))
        self.assertEqual(result, "")
        recognizer.model.transcribe.assert_not_called()

    def test_speech_with_low_volume_is_not_rejected_by_rms_only(self):
        recognizer = SpeechRecognizer("small")
        recognizer.model = Mock()
        recognizer.model.transcribe.return_value = (
            iter([SimpleNamespace(text="Chromeを開いて", avg_logprob=-0.3,
                                  no_speech_prob=0.12)]), None
        )
        samples = np.zeros(16000, dtype=np.float32)
        samples[5000:5100] = 0.015
        self.assertEqual(recognizer.transcribe(samples), "Chromeを開いて")

    def test_very_low_confidence_no_speech_segment_is_dropped(self):
        recognizer = SpeechRecognizer("small")
        recognizer.model = Mock()
        recognizer.model.transcribe.return_value = (
            iter([SimpleNamespace(text="ご視聴ありがとうございました", avg_logprob=-1.4,
                                  no_speech_prob=0.98),
                  SimpleNamespace(text="メモ帳を開いて", avg_logprob=-0.4,
                                  no_speech_prob=0.12)]), None
        )
        samples = np.full(16000, 0.02, dtype=np.float32)
        self.assertEqual(recognizer.transcribe(samples), "メモ帳を開いて")


class PlannerAccuracy(unittest.TestCase):
    def make_agent(self, proposed):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = proposed
        agent = VoiceAgent(Mock(), Mock(), planner, Mock(), Mock(), False, threading.Event())
        return agent, planner

    def test_single_retry_for_invalid_action_without_execution(self):
        agent, planner = self.make_agent([
            {"action": "unknown_tool", "args": {}},
            {"action": "browser_scroll", "args": {"direction": "down"}, "message": "下へ"},
        ])
        action, args, message = agent._plan_checked("スクロール", {}, [], None)
        self.assertEqual((action, args), ("browser_scroll", {"direction": "down"}))
        self.assertEqual(planner.next_action.call_count, 2)

    def test_fails_closed_after_second_invalid_proposal(self):
        agent, planner = self.make_agent([{"action": "unknown", "args": {}},
                                          {"action": "unknown", "args": {}}])
        with self.assertRaises(ValueError):
            agent._plan_checked("操作", {}, [], None)
        self.assertEqual(planner.next_action.call_count, 2)

    def test_network_failure_is_not_retried(self):
        agent, planner = self.make_agent([RuntimeError("No connection")])
        with self.assertRaises(RuntimeError):
            agent._plan_checked("操作", {}, [], None)
        self.assertEqual(planner.next_action.call_count, 1)

    def test_unnamed_web_button_requires_confirmation(self):
        agent, _ = self.make_agent([])
        context = {"browser": {"elements": [{"id": "a", "type": "button", "label": ""}]}}
        self.assertTrue(agent._requires_confirmation("browser_click", {"id": "a"}, context))

    def test_unnamed_windows_control_requires_confirmation(self):
        agent, _ = self.make_agent([])
        context = {"windows": {"controls": [{"id": "0", "name": ""}]}}
        self.assertTrue(agent._requires_confirmation("windows_click", {"id": "0"}, context))



class WindowsTargetAccuracy(unittest.TestCase):
    def test_uia_control_repurposed_since_snapshot_is_rejected(self):
        from app.win_actions import WindowsController
        controller = object.__new__(WindowsController)
        controller._last_control_window = 101
        controller._foreground_hwnd = lambda: 101
        controller._ensure_not_terminal = lambda: None
        ctrl = Mock()
        ctrl.is_visible.return_value = True
        ctrl.window_text.return_value = "削除"
        ctrl.element_info.control_type = "Button"
        controller._visible_controls = [ctrl]
        controller._visible_metadata = [("次へ", "Button")]
        fake = types.ModuleType("pywinauto")
        fake.Desktop, fake.keyboard = Mock(), Mock()
        with patch.dict(sys.modules, {"pywinauto": fake}):
            with self.assertRaises(RuntimeError):
                controller.act("windows_click", {"id": "0"})
        ctrl.invoke.assert_not_called()

    def test_uia_disabled_control_is_not_clicked(self):
        from app.win_actions import WindowsController
        controller = object.__new__(WindowsController)
        controller._last_control_window = 101
        controller._foreground_hwnd = lambda: 101
        controller._ensure_not_terminal = lambda: None
        ctrl = Mock()
        ctrl.is_visible.return_value = True
        ctrl.is_enabled.return_value = False
        ctrl.window_text.return_value = "続ける"
        ctrl.element_info.control_type = "Button"
        controller._visible_controls = [ctrl]
        controller._visible_metadata = [("続ける", "Button")]
        fake = types.ModuleType("pywinauto")
        fake.Desktop, fake.keyboard = Mock(), Mock()
        with patch.dict(sys.modules, {"pywinauto": fake}):
            with self.assertRaises(RuntimeError):
                controller.act("windows_click", {"id": "0"})
        ctrl.invoke.assert_not_called()

if __name__ == "__main__":
    unittest.main()