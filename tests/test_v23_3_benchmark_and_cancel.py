"""Safe, mocked regressions for opt-in benchmarking and last-moment cancellation."""
from __future__ import annotations

import threading
import unittest
from pathlib import Path
import sys
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
sys.path.insert(0, str(Path(__file__).parent))
import windows_safety_check as diag


class MicrophoneBenchmarkTests(unittest.TestCase):
    def test_three_trial_benchmark_reuses_recognizer_and_does_not_print_transcripts(self):
        microphones = []
        for _ in range(3):
            mic = Mock()
            mic.activity.return_value = {"heard_voice": True, "silence": 1.8}
            mic.stop.return_value = Mock(size=32000)
            microphones.append(mic)
        recognizer = Mock()
        recognizer.transcribe.side_effect = [
            "音声のテストです。",
            "異なる機密文字列ABC",
            "音声テストです",
        ]
        # Each trial uses five monotonic ticks: start, loop, silence, ASR start, ASR end.
        clocks = iter(float(i) for i in range(15))
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=False), \
             patch("builtins.input", return_value="YES"), \
             patch("app.audio.Microphone", side_effect=microphones) as factory, \
             patch("app.audio.SpeechRecognizer", return_value=recognizer) as model, \
             patch.object(diag.time, "monotonic", side_effect=lambda: next(clocks)), \
             patch.object(diag.time, "sleep"), \
             patch("builtins.print") as printed:
            result = diag.interactive_microphone(trials=3)
        self.assertEqual(result, "FAIL:2/3 phrase matches")
        self.assertEqual(factory.call_count, 3)
        model.assert_called_once_with("small")
        self.assertEqual(recognizer.transcribe.call_count, 3)
        for mic in microphones:
            mic.start.assert_called_once()
            mic.stop.assert_called_once()
        output = " ".join(str(c) for c in printed.call_args_list)
        self.assertIn("2/3", output)
        self.assertIn("中央値", output)
        self.assertNotIn("異なる機密文字列ABC", output)
        self.assertNotIn("音声のテストです。", output)

    def test_benchmark_refusal_never_opens_microphone(self):
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=False), \
             patch("builtins.input", return_value="N"), \
             patch("app.audio.Microphone") as microphone, \
             patch("app.audio.SpeechRecognizer") as recognizer, \
             patch("builtins.print"):
            result = diag.interactive_microphone(trials=3)
        self.assertEqual(result, "SKIP:no consent")
        microphone.assert_not_called()
        recognizer.assert_not_called()

    def test_admin_and_non_windows_deny_before_microphone(self):
        with patch.object(diag.os, "name", "nt"), \
             patch.object(diag.win, "current_process_elevated", return_value=True), \
             patch("builtins.input") as input_mock:
            self.assertEqual(diag.interactive_microphone(trials=3), "FAIL:admin execution")
        input_mock.assert_not_called()
        with patch.object(diag.os, "name", "posix"), \
             patch("builtins.input") as input_mock:
            self.assertEqual(diag.interactive_microphone(trials=3), "SKIP:Windows only")
        input_mock.assert_not_called()

    def test_invalid_trial_count_denied(self):
        with self.assertRaises(ValueError):
            diag.interactive_microphone(trials=100)


class FinalDispatchCancellationTests(unittest.TestCase):
    def test_cancelled_actions_never_reach_windows_browser_or_file_executor(self):
        cancel = threading.Event()
        cancel.set()
        bridge = Mock(connected=True)
        windows = Mock()
        agent = VoiceAgent(bridge, windows, Mock(provider="ollama"), Mock(),
                           Mock(), False, cancel)
        for action, args in (
            ("windows_hotkey", {"keys": "ctrl+z"}),
            ("browser_click", {"id": "1"}),
            ("browser_open", {"url": "https://example.org"}),
            ("files_open", {"path": "C:/fake/test.txt"}),
            ("screen_click", {"x": 0.5, "y": 0.5}),
        ):
            with self.subTest(action=action), self.assertRaises(InterruptedError):
                agent._execute(action, args)
        bridge.call.assert_not_called()
        windows.act.assert_not_called()


if __name__ == "__main__":
    unittest.main()
