"""Barge-in tests: opt-in, exact control phrases, pause boundary, mic lifecycle."""
import threading
import time
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.interrupt import (interrupt_intent, duplex_revision, ActionGate,
                            VoiceInterruptMonitor)
from app.agent import VoiceAgent
from app.main import VoicePilotApp


class PhraseTests(unittest.TestCase):
    def test_pause_commands_are_whole_utterance(self):
        for phrase in ("待って！", "ちょっと待って", "少し待ってください。", "一時停止して"):
            self.assertEqual(interrupt_intent(phrase), "pause")
        for phrase in ("待っての意味を説明して", "やめてほしいと言われた", "待ってと言った"):
            self.assertIsNone(interrupt_intent(phrase))

    def test_cancel_stop_and_end(self):
        for phrase in ("やめて", "ストップ！", "作業をやめて", "中止して。"):
            self.assertEqual(interrupt_intent(phrase), "cancel")
        self.assertEqual(interrupt_intent("ハンズフリー終了"), "end")

    def test_resume_only_when_paused(self):
        self.assertIsNone(interrupt_intent("続けて"))
        self.assertEqual(interrupt_intent("続けて", paused=True), "resume")
        self.assertIsNone(interrupt_intent("待って", paused=True))
        self.assertIsNone(interrupt_intent("続きをまとめて", paused=True))

    def test_duplex_revision_requires_an_explicit_prefix(self):
        self.assertEqual(duplex_revision("やっぱりメモ帳を開いて"), "メモ帳を開いて")
        self.assertEqual(duplex_revision("代わりに下へスクロールして"), "下へスクロールして")
        self.assertIsNone(duplex_revision("メモ帳を開いて"))
        self.assertIsNone(duplex_revision("やっぱり"))
        self.assertIsNone(duplex_revision("やっぱりやめて"))


class GateTests(unittest.TestCase):
    def test_pause_does_not_execute_until_resume(self):
        gate = ActionGate()
        gate.pause()
        done = threading.Event()
        cancelled = threading.Event()
        result = []
        worker = threading.Thread(target=lambda: (result.append(gate.checkpoint(cancelled)), done.set()))
        worker.start()
        self.assertFalse(done.wait(0.05))
        gate.resume()
        self.assertTrue(done.wait(1))
        self.assertEqual(result, [True])
        worker.join(1)

    def test_cancel_releases_paused_worker(self):
        gate = ActionGate()
        gate.pause()
        cancel = threading.Event()
        result = []
        t = threading.Thread(target=lambda: result.append(gate.checkpoint(cancel)))
        t.start()
        cancel.set()
        t.join(0.5)
        self.assertFalse(t.is_alive())
        self.assertEqual(result, [False])

    def test_pause_expires_and_cancels(self):
        now = [100.0]
        gate = ActionGate(timeout=3, clock=lambda: now[0])
        gate.pause()
        now[0] += 4
        event = threading.Event()
        self.assertFalse(gate.checkpoint(event))
        self.assertTrue(event.is_set())

    def test_paused_elapsed_time_is_not_counted_as_work(self):
        now = [10.0]
        gate = ActionGate(clock=lambda: now[0])
        gate.pause()
        now[0] += 60
        self.assertAlmostEqual(gate.paused_seconds(), 60.0)
        gate.resume()
        now[0] += 10
        self.assertAlmostEqual(gate.paused_seconds(), 60.0)

    def test_agent_replans_after_pause_instead_of_using_old_target(self):
        gate = ActionGate()
        cancel = threading.Event()
        planner = Mock(provider="ollama")
        calls = []
        def plan(*_args):
            calls.append(True)
            if len(calls) == 1:
                gate.pause()
                threading.Timer(0.08, gate.resume).start()
            return {"action": "windows_launch", "args": {"app": "notepad"}, "message": "open"}
        planner.next_action.side_effect = plan
        agent = VoiceAgent(Mock(connected=False), Mock(), planner,
                           Mock(), Mock(return_value=True), False, cancel,
                           action_gate=gate, max_steps=1)
        agent._context = Mock(return_value={})
        agent._execute = Mock()
        outcome = agent.run("複雑な画面整理をお願い")
        self.assertEqual(outcome.status, "incomplete")
        agent._execute.assert_not_called()
        self.assertEqual(len(calls), 1)

    def test_agent_halts_between_plan_and_action(self):
        gate = ActionGate()
        cancel = threading.Event()
        planner = Mock(provider="ollama")
        def plan(*_args):
            gate.pause()
            return {"action":"windows_launch","args":{"app":"notepad"},"message":"open"}
        planner.next_action.side_effect = plan
        agent = VoiceAgent(Mock(connected=False), Mock(), planner,
                           Mock(), Mock(return_value=True), False, cancel,
                           action_gate=gate)
        agent._context = Mock(return_value={})
        agent._execute = Mock(return_value={"ok":True})
        result = []
        t = threading.Thread(target=lambda: result.append(agent.run("ブラウザなどを適当に整理して")))
        t.start()
        time.sleep(0.1)
        agent._execute.assert_not_called()
        cancel.set()
        gate.resume()
        t.join(2)
        self.assertFalse(t.is_alive())
        self.assertEqual(result[0].status, "interrupted")
        agent._execute.assert_not_called()


class FakeMic:
    starts = 0
    stops = 0
    def __init__(self):
        self._stream = None
    def start(self, *, continuous=False):
        assert continuous
        FakeMic.starts += 1
        self._stream = object()
    def activity(self):
        return {"heard_voice":True,"speech_elapsed":0.8,"silence":1.0}
    def stop(self):
        FakeMic.stops += 1
        self._stream = None
        return np.ones(16000, dtype=np.float32) * 0.03


class MonitorTests(unittest.TestCase):
    def setUp(self):
        FakeMic.starts = FakeMic.stops = 0

    def test_pause_resume_cancel_sequence(self):
        gate = ActionGate()
        found = []
        finished = threading.Event()
        phrases = iter(["待って", "続けて", "やめて"])
        def on_intent(intent):
            found.append(intent)
            if intent == "pause":gate.pause()
            if intent == "resume":gate.resume()
            if intent == "cancel":finished.set()
        monitor = VoiceInterruptMonitor(lambda _: next(phrases), on_intent,
                                        gate, mic_factory=FakeMic)
        monitor.start()
        self.assertTrue(finished.wait(2))
        monitor.close()
        self.assertEqual(found, ["pause", "resume", "cancel"])
        self.assertFalse(gate.paused)
        self.assertEqual(FakeMic.starts, FakeMic.stops)

    def test_noise_or_long_text_cannot_cancel(self):
        gate = ActionGate()
        called = []
        monitor = VoiceInterruptMonitor(lambda _: "ブラウザでやめてを検索して", called.append,
                                        gate, mic_factory=FakeMic)
        monitor.start()
        time.sleep(0.24)
        monitor.close()
        self.assertEqual(called, [])
        self.assertEqual(FakeMic.starts, FakeMic.stops)

    def test_explicit_revision_is_delivered_once_and_stops_monitor(self):
        gate = ActionGate()
        revisions = []
        monitor = VoiceInterruptMonitor(
            lambda _: "やっぱり下へスクロールして", lambda _: None, gate,
            mic_factory=FakeMic, on_revision=revisions.append,
        )
        monitor.start()
        time.sleep(0.18)
        monitor.close()
        self.assertEqual(revisions, ["下へスクロールして"])
        self.assertEqual(FakeMic.starts, FakeMic.stops)

    def test_missing_device_reports_error_then_exits(self):
        def missing_mic():
            raise OSError("no device")
        errors = []
        monitor = VoiceInterruptMonitor(lambda _: "", lambda _:None,
                                        ActionGate(), mic_factory=missing_mic,
                                        on_error=errors.append)
        monitor.start()
        time.sleep(0.1)
        monitor.close()
        self.assertEqual(len(errors), 1)

    def test_no_microphone_until_explicit_handsfree(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()
        app.handsfree = Mock(enabled=False)
        app.recognizer = Mock()
        app.set_phase = Mock()
        app.restore_target = Mock()
        app.bridge = Mock()
        app.windows = Mock()
        app.log = Mock()
        app.confirm = Mock(return_value=True)
        app.stop_event = threading.Event()
        app.session = {}
        app.config = {}
        app._action_gate = app._interrupt_monitor = None
        runtime = dict(provider="ollama",key="",gemini_model="ignored",
                       ollama_model="ignored",allow_cloud_ui=False,allow_cloud_image=False)
        # Agent.run returns immediately, independent of UI/mic internals.
        from unittest.mock import patch
        with patch('app.main.VoiceAgent') as agent:
            agent.return_value.run.return_value = 'ok'
            self.assertEqual(app._run_agent('複雑な作業をお願い',runtime), 'ok')
            self.assertIsNone(app._interrupt_monitor)
            self.assertIsNone(app._action_gate)


class HandsFreeDurationTests(unittest.TestCase):
    def test_activity_exposes_duration_for_handsfree_silence_stop(self):
        from app.audio import Microphone
        mic = Microphone()
        mic._stream = object()
        mic._started = time.monotonic() - 5
        mic._first_voice = time.monotonic() - 1.0
        mic._last_voice = time.monotonic() - 0.7
        mic._heard_voice = True
        a = mic.activity()
        self.assertGreaterEqual(a['speech_elapsed'], 0.9)
        self.assertGreaterEqual(a['silence'], 0.6)
        mic._stream = None


class DuplexReplacementTests(unittest.TestCase):
    def test_revision_stops_old_task_without_revoking_permission(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.stop_event = threading.Event()
        app._closing = False
        app._duplex_revision = None
        app.ui = Mock()
        app.log = Mock()
        app._apply_phase = Mock()
        app.control_permit = Mock()
        gate = Mock()

        app._handle_duplex_revision("Chromeで下へスクロールして", gate)

        self.assertEqual(app._duplex_revision, "Chromeで下へスクロールして")
        self.assertTrue(app.stop_event.is_set())
        gate.resume.assert_called_once()
        app.control_permit.revoke.assert_not_called()

    def test_replacement_starts_only_after_old_worker_is_idle(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.busy = True
        app.phase = "acting"
        app._status_detail = None
        app._record_after_cancel = False
        app._duplex_revision = "下へスクロールして"
        app._closing = False
        app.handsfree = Mock(enabled=True)
        app.stop_event = threading.Event()
        app.ui = type("ImmediateUI", (), {"post": staticmethod(lambda callback: callback())})()
        app._apply_phase = Mock()
        app.runtime_settings = Mock(return_value={"provider": "ollama"})
        app.log = Mock()
        app.set_phase = Mock()
        with patch("app.main.threading.Thread") as worker:
            app.mark_idle("interrupted")
        self.assertFalse(app.stop_event.is_set())
        self.assertFalse(app._duplex_revision)
        self.assertTrue(app.busy)
        self.assertEqual(worker.call_args.kwargs["args"][0], "下へスクロールして")


if __name__ == '__main__':
    unittest.main()
