"""v0.4 resource, context, and expanded desktop-action regression tests."""
import threading
import time
import unittest
from unittest.mock import patch

from app.actions import validate_action
from app.agent import VoiceAgent
from app.compact import compact_for_model
from app.planner import Planner
from app.vision import ScreenFrame


class _Bridge:
    connected = False


class _Windows:
    def __init__(self):
        self.hwnd = 42
        self.dragged = []

    def foreground_hwnd(self):
        return self.hwnd

    def is_chrome_foreground(self):
        return False

    def snapshot(self):
        return {"windows": [], "controls": [], "active_title": "Test Window"}

    def drag_point(self, *coords):
        self.dragged.append(coords)
        return "dragged"


class _Planner:
    provider = "ollama"

    def __init__(self):
        self.responses = iter([
            {"action": "screen_observe", "args": {}, "message": "observe"},
            {"action": "screen_drag", "args": {
                "x": "100", "y": "200", "to_x": "500", "to_y": "600", "target": "スライダー"
            }, "message": "drag"},
            {"action": "done", "args": {}, "message": "done"},
        ])

    def next_action(self, *_args, **_kwargs):
        return next(self.responses)


class TestLowResourcePlan(unittest.TestCase):
    @patch('app.planner.post_json')
    def test_ollama_auto_unloads_and_caps_context_threads(self, send):
        send.return_value = {"response": '{"action":"done","args":{},"message":"ok"}'}
        Planner("ollama").next_action("テスト", {}, [])
        payload = send.call_args.args[1]
        self.assertEqual(payload["keep_alive"], "30s")
        self.assertLessEqual(payload["options"]["num_thread"], 4)
        self.assertEqual(payload["options"]["num_ctx"], 4096)
        self.assertNotIn("images", payload)

    def test_compact_context_keeps_matching_control_and_original_intact(self):
        elements = [{"id": str(i), "label": f"ボタン番号{i}", "in_view": True} for i in range(110)]
        elements[-1] = {"id": "last", "label": "卒業論文をダウンロード", "in_view": False}
        controls = [{"id": str(i), "name": f"操作欄{i}"} for i in range(65)]
        controls[-1] = {"id": "last-control", "name": "卒業論文をダウンロード"}
        original = {"browser": {"elements": elements}, "windows": {"controls": controls}}
        reduced = compact_for_model(original, "卒業論文をダウンロードして")
        self.assertLessEqual(len(reduced["browser"]["elements"]), 70)
        self.assertLessEqual(len(reduced["windows"]["controls"]), 45)
        self.assertIn("last", [e["id"] for e in reduced["browser"]["elements"]])
        self.assertIn("last-control", [e["id"] for e in reduced["windows"]["controls"]])
        self.assertEqual(len(original["browser"]["elements"]), 110)
        self.assertEqual(len(original["windows"]["controls"]), 65)

    def test_short_context_unmodified(self):
        original = {"browser": {"elements": [{"id": "1", "label": "戻る"}]}}
        self.assertEqual(compact_for_model(original, "戻る"), original)

    def test_drag_rejects_invalid_coordinates_or_missing_target(self):
        for kwargs in [
            {"x": "0", "y": "0", "to_x": "1001", "to_y": "5", "target": "スライダー"},
            {"x": "0", "y": "0", "to_x": "2", "to_y": "-1", "target": "スライダー"},
            {"x": "0", "y": "0", "to_x": "2", "to_y": "3", "target": ""},
        ]:
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                validate_action({"action": "screen_drag", "args": kwargs})

    def test_drag_requires_approval(self):
        frame = ScreenFrame(b"fake", 1001, 1001, 42, time.monotonic())
        windows = _Windows()
        confirm_calls = []
        def reject(msg):
            confirm_calls.append(msg)
            return False
        with patch("app.agent.capture_screen", return_value=frame):
            VoiceAgent(_Bridge(), windows, _Planner(), lambda _: None, reject,
                       False, threading.Event()).run("スライダーを動かして")
        self.assertEqual(windows.dragged, [])
        self.assertEqual(len(confirm_calls), 1)

    def test_drag_moves_with_user_approval(self):
        frame = ScreenFrame(b"fake", 1001, 1001, 42, time.monotonic())
        windows = _Windows()
        with patch("app.agent.capture_screen", return_value=frame):
            VoiceAgent(_Bridge(), windows, _Planner(), lambda _: None, lambda _: True,
                       False, threading.Event()).run("スライダーを動かして")
        self.assertEqual(windows.dragged, [(100, 200, 500, 600)])

    def test_drag_refuses_stale_or_changed_foreground(self):
        frame = ScreenFrame(b"fake", 1001, 1001, 42, time.monotonic() - 180)
        windows = _Windows()
        with patch("app.agent.capture_screen", return_value=frame):
            VoiceAgent(_Bridge(), windows, _Planner(), lambda _: None, lambda _: True,
                       False, threading.Event()).run("スライダーを動かして")
        self.assertEqual(windows.dragged, [])

    def test_idle_speech_unloads_without_model_running(self):
        from app.main import VoicePilotApp, SPEECH_MODEL_IDLE_SECONDS
        class Root:
            def __init__(self): self.scheduled = []
            def after(self, ms, callback): self.scheduled.append((ms, callback))
        class Fake:
            pass
        f = Fake()
        f.busy = False
        f.recording = False
        f.recognizer = Fake()
        f.recognizer.model = object()
        f._last_transcription_finished = time.monotonic() - SPEECH_MODEL_IDLE_SECONDS - 2
        f.root = Root()
        f.release_idle_model = lambda: None
        VoicePilotApp.release_idle_model(f)
        self.assertIsNone(f.recognizer)
        self.assertEqual(f.root.scheduled[0][0], 10_000)

    def test_busy_speech_is_never_unloaded(self):
        from app.main import VoicePilotApp
        class Root:
            def after(self, *_): pass
        class Fake:
            pass
        f = Fake()
        f.busy = True
        f.recording = False
        f.recognizer = Fake()
        f._last_transcription_finished = time.monotonic() - 999
        f.root = Root()
        f.release_idle_model = lambda: None
        VoicePilotApp.release_idle_model(f)
        self.assertIsNotNone(f.recognizer)


if __name__ == "__main__":
    unittest.main()