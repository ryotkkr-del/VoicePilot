"""UX correctness independent of a Windows desktop or microphone hardware."""
import threading
import unittest

from app.ux import presentation, meter_level, friendly_error
from app.agent import VoiceAgent


class Bridge:
    connected = False


class Windows:
    def is_chrome_foreground(self): return False
    def snapshot(self): return {"controls": [], "windows": []}


class Planner:
    provider = 'ollama'
    def __init__(self, result): self.result = result
    def next_action(self, *_): return self.result


class UXPresentationTest(unittest.TestCase):
    def test_recording_and_processing_are_distinct_and_cancellable(self):
        self.assertEqual(presentation('listening', heard_voice=True).title, '声を検出しました')
        self.assertTrue(presentation('transcribing').can_stop)
        self.assertFalse(presentation('transcribing').can_record)
        self.assertTrue(presentation('completed').can_record)
        self.assertEqual(presentation('listening', elapsed=3).tone, 'recording')
        self.assertIn('マイク', presentation('listening', elapsed=3).hint)
        self.assertEqual(presentation('cancelling').tone, 'working')
        self.assertEqual(presentation('error').tone, 'error')
        with self.assertRaises(ValueError): presentation('typo')

    def test_manual_recording_mode_has_correct_instruction(self):
        p = presentation('listening', heard_voice=True, auto_stop=False)
        self.assertIn('Ctrl + Alt + Space', p.hint)
        self.assertNotIn('自動', p.hint)

    def test_microphone_meter_is_bounded(self):
        self.assertEqual(meter_level(0, .01), 0)
        self.assertEqual(meter_level(.02, .01), 4)
        self.assertEqual(meter_level(.5, 0), 0)
        self.assertEqual(meter_level(1000, .01), 5)

    def test_errors_hide_sensitive_remote_response(self):
        self.assertIn('Ollama', friendly_error(ConnectionRefusedError('connection refused')))
        self.assertIn('無料枠', friendly_error(RuntimeError('API says 429 quota')))
        self.assertNotIn('secret', friendly_error(ValueError('secret token leaked')))

    def test_recognition_result_does_not_claim_completion_without_action(self):
        logs=[]
        agent=VoiceAgent(Bridge(), Windows(), Planner({'action':'done','args':{},'message':'完了しました'}),
                         logs.append, lambda _: True, False, threading.Event())
        outcome=agent.run('ここに表示されている文書を開き、内容を確認して')
        self.assertEqual(outcome.status, 'incomplete')
        self.assertIn('操作は実行されません', outcome.summary)

    def test_disallowed_cloud_has_explicit_incomplete_status(self):
        logs=[]
        planner=Planner({'action':'done','args':{}})
        planner.provider='gemini'
        outcome=VoiceAgent(Bridge(), Windows(), planner, logs.append, lambda _: True, False, threading.Event()).run('画面を開いて')
        self.assertEqual(outcome.status, 'incomplete')

    def test_quick_command_does_not_claim_success_without_evidence(self):
        class QuickWindows(Windows):
            def act(self, action, args): return 'ok'
        class NoPlanner(Planner):
            def next_action(self, *_): raise AssertionError('quick actions must not ask the LLM')
        result = VoiceAgent(Bridge(), QuickWindows(), NoPlanner({}), lambda _: None,
                            lambda _: True, False, threading.Event()).run('メモ帳を開いて')
        self.assertEqual(result.status, 'unverified')

    def test_global_abort_has_explicit_interrupted_status(self):
        flag=threading.Event(); flag.set()
        outcome=VoiceAgent(Bridge(), Windows(), Planner({}), lambda _: None, lambda _: True, False, flag).run('開いて')
        self.assertEqual(outcome.status, 'interrupted')

    def test_screen_coordinates_invalid_after_12_seconds(self):
        import time
        from app.vision import ScreenFrame
        from app.agent import VoiceAgent
        class ScreenWindows(Windows):
            def foreground_hwnd(self): return 42
            def click_point(self, *a, **k): raise AssertionError('stale image must not click')
        a=VoiceAgent(Bridge(), ScreenWindows(), Planner({}), lambda _: None, lambda _: True, False, threading.Event())
        a._frame=ScreenFrame(b'jpeg', 100, 100, 42, time.monotonic()-13)
        with self.assertRaisesRegex(RuntimeError, '古く'):
            a._execute('screen_click', {'x': '500', 'y': '500'})


if __name__ == '__main__': unittest.main()