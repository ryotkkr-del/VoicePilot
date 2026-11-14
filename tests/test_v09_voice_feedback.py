"""No speaker or Windows installation required: fake COM backend tests timing."""
from __future__ import annotations

import threading
import time
import unittest
from unittest.mock import Mock

from app.voice_feedback import SpeechFeedback, result_announcement
from app.main import VoicePilotApp


def until(predicate, timeout=1.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


class VoiceCopyTests(unittest.TestCase):
    def test_verified_completion_only_gets_success_copy(self):
        self.assertIn("完了", result_announcement("completed"))
        for status in ("unverified", "incomplete", "error", "no_speech"):
            self.assertNotIn("完了しました", result_announcement(status))
        self.assertIsNone(result_announcement("interrupted"))
        self.assertIsNone(result_announcement("unexpected"))

    def test_copy_does_not_expose_instruction_or_file_names(self):
        # Deliberately takes only result status; never arbitrary model text.
        self.assertEqual(result_announcement("completed"), "操作が完了しました。")
        self.assertEqual(result_announcement("completed", "windows_launch"), "アプリを開きました。")
        self.assertEqual(result_announcement("completed", "files_rename"), "ファイル名を変更しました。")
        self.assertEqual(result_announcement("unverified", "windows_launch"),
                         "操作しましたが、結果は確認できませんでした。")


class SpeechWorkerTests(unittest.TestCase):
    def test_backend_is_lazy(self):
        factory = Mock()
        speaker = SpeechFeedback(backend_factory=factory)
        self.assertEqual(factory.call_count, 0)
        speaker.close()
        self.assertFalse(speaker.say("再生するな"))
        self.assertEqual(factory.call_count, 0)

    def test_speaker_completes_and_waits_without_polling(self):
        played = []
        class Backend:
            def speak(self, text, cancelled):
                played.append(text)
            def close(self):
                pass
        speaker = SpeechFeedback(backend_factory=Backend)
        try:
            self.assertTrue(speaker.say("完了です"))
            self.assertTrue(until(lambda: played == ["完了です"]))
            self.assertTrue(speaker.say("もう一度"))
            self.assertTrue(until(lambda: played == ["完了です", "もう一度"]))
        finally:
            speaker.close()

    def test_stop_cancels_active_speech(self):
        started = threading.Event()
        cancelled = threading.Event()
        class Backend:
            def speak(self, text, is_cancelled):
                started.set()
                if until(is_cancelled):
                    cancelled.set()
            def close(self):
                pass
        speaker = SpeechFeedback(backend_factory=Backend)
        try:
            speaker.say("長い音声")
            self.assertTrue(started.wait(1.0))
            speaker.stop()
            self.assertTrue(cancelled.wait(1.0))
        finally:
            speaker.close()

    def test_newest_message_replaces_pending_queue(self):
        started = threading.Event()
        observed = []
        class Backend:
            def speak(self, text, is_cancelled):
                observed.append(text)
                if len(observed) == 1:
                    started.set()
                    until(is_cancelled)
            def close(self):
                pass
        speaker = SpeechFeedback(backend_factory=Backend)
        try:
            speaker.say("旧い報告")
            self.assertTrue(started.wait(1))
            speaker.say("途中の報告")
            speaker.say("最新の報告")
            self.assertTrue(until(lambda: "最新の報告" in observed))
            self.assertEqual(observed, ["旧い報告", "最新の報告"])
        finally:
            speaker.close()

    def test_backend_failure_only_reported_once(self):
        errors = []
        def broken():
            raise RuntimeError("voice unavailable")
        speaker = SpeechFeedback(on_error=errors.append, backend_factory=broken)
        try:
            speaker.say("試験")
            self.assertTrue(until(lambda: bool(errors)))
            self.assertFalse(speaker.say("再試行"))
            self.assertEqual(len(errors), 1)
        finally:
            speaker.close()

    def test_rejects_unbounded_tts_content(self):
        speaker = SpeechFeedback(backend_factory=lambda: None)
        try:
            self.assertFalse(speaker.say(""))
            self.assertFalse(speaker.say("a"*101))
            self.assertIsNone(speaker._worker_thread)
        finally:
            speaker.close()


class ApplicationIntegrationTests(unittest.TestCase):
    def test_voice_only_and_user_setting(self):
        app = object.__new__(VoicePilotApp)
        app.config = {"voice_feedback": True}
        app.stop_event = threading.Event()
        app.speaker = Mock()
        app._speak_result("completed", "windows_launch")
        app.speaker.say.assert_called_once_with("アプリを開きました。")
        app.speaker.reset_mock()
        app.config["voice_feedback"] = False
        app._speak_result("completed")
        app.speaker.say.assert_not_called()
        app.config["voice_feedback"] = True
        app.stop_event.set()
        app._speak_result("completed")
        app.speaker.say.assert_not_called()

    def test_no_speech_after_cancellation(self):
        app = object.__new__(VoicePilotApp)
        app.config = {"voice_feedback": True}
        app.stop_event = threading.Event()
        app.speaker = Mock()
        app._speak_result("interrupted")
        app.speaker.say.assert_not_called()


if __name__ == '__main__':
    unittest.main()