"""Small reliability and resource regressions added in the v0.7 polish pass."""
from __future__ import annotations

import asyncio
import json
import tempfile
from pathlib import Path
import sys
import threading
import time
import types
import unittest
from unittest.mock import patch

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from app.agent import VoiceAgent
from app.actions import validate_action
from app.fast_commands import direct_action
from app.audio import Microphone
from app.bridge import BrowserBridge
from app.ui_dispatch import UIDispatcher


def action(name, args=None):
    return {"action": name, "args": args or {}, "message": "操作"}


class _Bridge:
    connected = False


class _Windows:
    def __init__(self):
        self.actions = []

    def is_chrome_foreground(self):
        return False

    def snapshot(self):
        return {"active_title": "メモ帳", "windows": [],
                "controls": [{"id": "0", "name": "次へ", "type": "Button"}]}

    def act(self, name, args):
        self.actions.append((name, args))
        return "accepted"


class _Planner:
    provider = "ollama"

    def __init__(self, actions):
        self.actions = iter(actions)

    def next_action(self, *_):
        return next(self.actions)


class SafetyTests(unittest.TestCase):
    def agent(self, windows, actions):
        return VoiceAgent(_Bridge(), windows, _Planner(actions), lambda _: None,
                          lambda _: True, False, threading.Event())

    def test_identical_unverified_click_is_not_sent_twice(self):
        windows = _Windows()
        outcome = self.agent(windows, [
            action("windows_click", {"id": "0"}),
            action("windows_click", {"id": "0"}),
            action("done"),
        ]).run("次の画面に進んで")
        self.assertEqual(len(windows.actions), 1)
        self.assertEqual(outcome.status, "incomplete")
        self.assertIn("再実行", outcome.summary)

    def test_identical_typing_is_not_doubled(self):
        windows = _Windows()
        outcome = self.agent(windows, [
            action("windows_type", {"text": "こんにちは"}),
            action("windows_type", {"text": "こんにちは"}),
        ]).run("テキストを入力して")
        self.assertEqual(len(windows.actions), 1)
        self.assertEqual(outcome.status, "incomplete")

    def test_repeated_navigation_keys_still_work(self):
        windows = _Windows()
        outcome = self.agent(windows, [
            action("windows_hotkey", {"keys": "tab"}),
            action("windows_hotkey", {"keys": "tab"}),
            action("done"),
        ]).run("タブキーを二回押して")
        self.assertEqual(len(windows.actions), 2)
        self.assertEqual(outcome.status, "unverified")

    def test_repeated_scroll_remains_available(self):
        windows = _Windows()
        outcome = self.agent(windows, [
            action("windows_scroll", {"direction": "down"}),
            action("windows_scroll", {"direction": "down"}),
            action("done"),
        ]).run("二回下へスクロールして")
        self.assertEqual(len(windows.actions), 2)
        self.assertEqual(outcome.status, "unverified")

    def test_page_end_command_does_not_degrade_to_one_page(self):
        self.assertEqual(direct_action("一番下までスクロールして"),
                         ("smart_scroll", {"direction": "down", "amount": "large"}))
        windows = _Windows()
        outcome = self.agent(windows, [action("done")]).run("一番下までスクロールして")
        self.assertEqual(windows.actions, [("windows_scroll", {"direction": "bottom"})])
        self.assertIn(outcome.status, {"unverified", "completed"})

    def test_scroll_direction_rejects_typos(self):
        for direction in ("up", "down", "top", "bottom"):
            validate_action(action("browser_scroll", {"direction": direction}))
        with self.assertRaises(ValueError):
            validate_action(action("browser_scroll", {"direction": "sideways"}))


class DispatcherTests(unittest.TestCase):
    def test_all_tk_callbacks_run_in_the_pump_not_worker(self):
        class Root:
            scheduled = []
            def after(self, ms, fn): self.scheduled.append((ms, fn))
        root = Root()
        ui = UIDispatcher(root, batch_size=2)
        main_thread = threading.get_ident()
        called = []
        thread = threading.Thread(target=lambda: [ui.post(lambda: called.append(threading.get_ident())) for _ in range(3)])
        thread.start(); thread.join()
        self.assertEqual(called, [])
        self.assertEqual(len(root.scheduled), 1)
        root.scheduled.pop(0)[1]()
        self.assertEqual(called, [main_thread, main_thread])
        root.scheduled.pop(0)[1]()
        self.assertEqual(called, [main_thread] * 3)
        ui.shutdown()
        ui.post(lambda: called.append("after close"))
        self.assertNotIn("after close", called)


class MicrophoneTests(unittest.TestCase):
    def test_failed_input_start_always_releases_device(self):
        events = []
        class Stream:
            def start(self):
                events.append("start")
                raise OSError("microphone is busy")
            def close(self): events.append("close")
        fake_sd = types.SimpleNamespace(query_devices=lambda kind: {"default_samplerate": 16000},
                                        InputStream=lambda **kwargs: Stream())
        mic = Microphone()
        with patch.dict(sys.modules, {"sounddevice": fake_sd}):
            with self.assertRaises(OSError):
                mic.start()
        self.assertEqual(events, ["start", "close"])
        self.assertIsNone(mic._stream)


class ConfigTests(unittest.TestCase):
    def test_settings_use_atomic_replacement_and_preserve_old_data_on_error(self):
        from app import main
        with tempfile.TemporaryDirectory() as folder:
            directory = Path(folder)
            settings = directory / "config.json"
            with patch.object(main, "CONFIG_DIR", directory), patch.object(main, "CONFIG_FILE", settings):
                first = dict(main.DEFAULTS, token="paired-code")
                main.save_config(first)
                self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["token"], "paired-code")
                with patch.object(main.os, "replace", side_effect=OSError("write interrupted")):
                    with self.assertRaises(OSError):
                        main.save_config(dict(first, token="new-code"))
                self.assertEqual(json.loads(settings.read_text(encoding="utf-8"))["token"], "paired-code")
                self.assertEqual(list(directory.glob("*.tmp")), [])


class BridgeTests(unittest.TestCase):
    def test_reconnect_rejects_stale_inflight_request_and_close_releases_port(self):
        import json
        port = 18773
        bridge = BrowserBridge("valid-secret", port=port)
        bridge.start()
        self.assertEqual(bridge.error, "")
        client_error = []
        origin = "chrome-extension://aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa"

        async def scenario():
            async with connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as first:
                await first.send(json.dumps({"type": "hello", "token": "valid-secret"}))
                self.assertIn("ready", await first.recv())
                caller = threading.Thread(target=lambda: self._call_with_result(bridge, client_error), daemon=True)
                caller.start()
                pending_request = json.loads(await asyncio.wait_for(first.recv(), 2))
                self.assertEqual(pending_request["action"], "snapshot")
                async with connect(f"ws://127.0.0.1:{port}/ws", origin=origin) as second:
                    await second.send(json.dumps({"type": "hello", "token": "valid-secret"}))
                    self.assertIn("ready", await second.recv())
                    # The first command was sent to the OLD extension. The
                    # reconnect must resolve it as unknown, not success.
                    caller.join(timeout=2)
                    self.assertFalse(caller.is_alive())
                    self.assertTrue(client_error)
                    self.assertIsInstance(client_error[0], ConnectionError)
                    self.assertTrue(bridge.connected)
        try:
            asyncio.run(scenario())
        finally:
            bridge.close()
        self.assertFalse(bridge.connected)
        second_instance = BrowserBridge("another-secret", port=port)
        second_instance.start()
        try:
            self.assertEqual(second_instance.error, "")
        finally:
            second_instance.close()

    @staticmethod
    def _call_with_result(bridge, errors):
        try:
            bridge.call("snapshot", timeout=5)
        except Exception as exc:
            errors.append(exc)


if __name__ == "__main__":
    unittest.main()