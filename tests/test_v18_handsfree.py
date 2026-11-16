"""Hands-free lifecycle, control phrases and microphone/TTS safety; no Windows needed."""
import threading
import unittest
from unittest.mock import Mock

from app.handsfree import HandsFreeSession, handsfree_control
from app.main import VoicePilotApp
from app.voice_feedback import SpeechFeedback


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.now = [100.0]
        self.session = HandsFreeSession(clock=lambda: self.now[0])

    def test_opt_in_and_generation(self):
        self.assertFalse(self.session.enabled)
        token = self.session.start()
        self.assertTrue(self.session.is_current(token))
        self.session.stop()
        self.assertFalse(self.session.is_current(token))
        self.assertFalse(self.session.enabled)
        self.assertNotEqual(token, self.session.start())

    def test_idle_timeout_and_activity(self):
        self.session.start()
        self.now[0] += 179
        self.assertFalse(self.session.expired())
        self.now[0] += 2
        self.assertTrue(self.session.expired())
        self.assertFalse(self.session.expired(speaking=True))
        self.session.activity()
        self.assertFalse(self.session.expired())

    def test_three_errors_disable_rearming_policy(self):
        self.session.start()
        self.assertFalse(self.session.failure())
        self.assertFalse(self.session.failure())
        self.assertTrue(self.session.failure())
        self.session.activity()
        self.assertTrue(self.session.failure())
        self.session.success()
        self.assertFalse(self.session.failure())

    def test_stop_phrases_are_exact(self):
        for spoken in ('ハンズフリー終了。', '連続対話を終了して', '聞き取りを止めて'):
            self.assertEqual('stop', handsfree_control(spoken))
        for spoken in ('停止', '今日はハンズフリー終了の実験をしました', 'ハンズフリー',
                       'ハンズフリー終了する方法', 'これを読んで'):
            self.assertIsNone(handsfree_control(spoken))


class FakeRoot:
    def __init__(self):
        self.callbacks = []
    def after(self, delay, callback):
        self.callbacks.append((delay, callback))
    def flush(self, *, max_iterations=30):
        count = 0
        while self.callbacks and count < max_iterations:
            _, callback = self.callbacks.pop(0)
            callback()
            count += 1
        return count


class AppLifecycleTests(unittest.TestCase):
    def minimal(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()
        app._refresh_control_lock_ui = Mock()
        app.handsfree = HandsFreeSession(clock=lambda: 0)
        app._handsfree_capture = False
        app._capture_generation = 0
        app._closing = False
        app.recording = False
        app.busy = False
        app.stop_event = threading.Event()
        app._record_after_cancel = False
        app._apply_phase = Mock()
        app._refresh_record_buttons = Mock()
        app.set_phase = Mock()
        app.log = Mock()
        app.root = FakeRoot()
        app.speaker = Mock()
        app.speaker.is_active.return_value = False
        app.mic = Mock()
        app.command = Mock()
        app.command.get.return_value = ''
        app.config = {'silence_seconds': 1.4}
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        return app

    def test_explicit_start_resumes_without_duplicate_microphones(self):
        app = self.minimal()
        VoicePilotApp.toggle_handsfree(app)
        self.assertTrue(app.handsfree.enabled)
        app.root.flush(max_iterations=2)
        app.mic.start.assert_called_once_with(continuous=True)
        self.assertTrue(app.recording)
        self.assertTrue(app._handsfree_capture)

    def test_stop_invalidates_scheduled_rearm(self):
        app = self.minimal()
        VoicePilotApp.toggle_handsfree(app)
        VoicePilotApp.stop_handsfree(app)
        app.root.flush()
        app.mic.start.assert_not_called()

    def test_stop_during_capture_discards_and_does_not_restart(self):
        app = self.minimal()
        VoicePilotApp.toggle_handsfree(app)
        # manually execute the two scheduled rearm steps
        _, cb = app.root.callbacks.pop(0); cb()
        _, cb = app.root.callbacks.pop(0); cb()
        self.assertTrue(app.recording)
        VoicePilotApp.stop_handsfree(app)
        self.assertFalse(app.recording)
        app.mic.stop.assert_called_once()
        app.root.flush()
        self.assertEqual(app.mic.start.call_count, 1)

    def test_speech_output_delays_rearm(self):
        app = self.minimal()
        app.speaker.is_active.side_effect = [True, True, False, False]
        VoicePilotApp.toggle_handsfree(app)
        # call rearm three times, then the 450ms quiet margin
        app.root.flush(max_iterations=4)
        app.mic.start.assert_called_once_with(continuous=True)

    def test_busy_does_not_reopen_microphone(self):
        app = self.minimal()
        VoicePilotApp.toggle_handsfree(app)
        app.busy = True
        app.root.flush()
        app.mic.start.assert_not_called()
        # Once the worker finishes, mark_idle can resume.
        VoicePilotApp.mark_idle(app, 'completed')
        app.root.flush(max_iterations=2)
        app.mic.start.assert_called_once()

    def test_voice_stop_is_intercepted_before_agent_and_dictation(self):
        app = self.minimal()
        app.handsfree.start()
        app.recognizer = Mock(model_name='small')
        app.recognizer.transcribe.return_value = 'ハンズフリー終了。'
        app._run_agent = Mock()
        app._run_dictation_command = Mock()
        app.mark_idle = Mock()
        app._speak_result = Mock()
        app.dictation_target = None
        app.runtime_settings = Mock()
        app._last_transcription_finished = 0
        VoicePilotApp.transcribe_then_execute(app, [0.3],
                                             {'whisper_model': 'small'}, True)
        self.assertFalse(app.handsfree.enabled)
        app._run_agent.assert_not_called()
        app._run_dictation_command.assert_not_called()
        app.mark_idle.assert_called_once()

    def test_empty_transcript_does_not_extend_handsfree_idle(self):
        now = [100.0]
        app = self.minimal()
        app.handsfree = HandsFreeSession(clock=lambda: now[0])
        app.handsfree.start()
        app.recognizer = Mock(model_name='small')
        app.recognizer.transcribe.return_value = ''
        app.mark_idle = Mock()
        app._speak_result = Mock()
        app.dictation_target = None
        now[0] = 300.0
        VoicePilotApp.transcribe_then_execute(app, [0.0],
                                             {'whisper_model': 'small'}, True)
        self.assertTrue(app.handsfree.expired())
        app._speak_result.assert_not_called()

    def test_explicit_abort_disables_session(self):
        app = self.minimal()
        VoicePilotApp.toggle_handsfree(app)
        VoicePilotApp.abort(app)
        app.root.flush()
        self.assertFalse(app.handsfree.enabled)
        app.mic.start.assert_not_called()

    def test_mic_start_failure_disables_session(self):
        app = self.minimal()
        app.mic.start.side_effect = OSError('broken mic')
        VoicePilotApp.toggle_handsfree(app)
        app.root.flush()
        self.assertFalse(app.handsfree.enabled)
        app.set_phase.assert_called_with('error')
        self.assertEqual(app.mic.start.call_count, 1)


class SpeechActivityTests(unittest.TestCase):
    def test_pending_speech_is_active_and_stop_clears(self):
        class MockBackend:
            def speak(self, _text, _cancelled): pass
            def close(self): pass
        # A disabled TTS backend avoids starting the worker in this test.
        speaker = SpeechFeedback(backend_factory=MockBackend)
        with speaker._condition:
            speaker._pending = (1, '操作完了')
        self.assertTrue(speaker.is_active())
        speaker.stop()
        self.assertFalse(speaker.is_active())
        speaker.close()


if __name__ == '__main__':
    unittest.main()

class MicrophoneBoundedBufferTests(unittest.TestCase):
    def test_opt_in_idle_keeps_only_short_preroll(self):
        import sys
        import time
        import types
        import numpy as np
        from unittest.mock import patch
        from app.audio import Microphone

        fake = types.ModuleType('sounddevice')
        fake.query_devices = lambda kind: {'default_samplerate': 16000}
        state = {}
        class FakeStream:
            def __init__(self, **kwargs):
                state['callback'] = kwargs['callback']
            def start(self): pass
            def stop(self): pass
            def close(self): pass
        fake.InputStream = FakeStream
        with patch.dict(sys.modules, {'sounddevice': fake}):
            mic = Microphone()
            mic.start(continuous=True)
            chunk = np.zeros((1024, 1), dtype=np.float32)
            for _ in range(400):
                state['callback'](chunk, len(chunk), None, None)
            self.assertLessEqual(len(mic._chunks), 33)
            self.assertFalse(mic.activity()['heard_voice'])
            samples = mic.stop()
            self.assertLessEqual(samples.size, 33 * 1024)