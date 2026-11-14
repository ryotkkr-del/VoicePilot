"""Test high-risk invariants without needing a physical Windows desktop."""
import threading
import time
import unittest
from unittest.mock import patch

from app.actions import validate_action
from app.vision import ScreenFrame
from app.agent import VoiceAgent
from app.win_actions import app_shortcut, start_menu_shortcuts


class FakeBridge:
    connected = False


class FakeWindows:
    def __init__(self):
        self.calls = []
        self.hwnd = 42

    def is_chrome_foreground(self):
        return False

    def foreground_hwnd(self):
        return self.hwnd

    def inspect_point(self, x, y):
        return {'name': '次へ', 'type': 'Button'}

    def click_point(self, x, y, double=False):
        self.calls.append((x, y, double))
        return 'clicked'

    def snapshot(self):
        return {'windows': [{'title': 'メモ帳'}], 'active_title': 'メモ帳', 'controls': []}


class ScriptedPlanner:
    def __init__(self, actions, provider='ollama'):
        self.actions = iter(actions)
        self.provider = provider
        self.images = []

    def next_action(self, instruction, context, history, screenshot=None):
        self.images.append(screenshot)
        return next(self.actions)


def action(name, **kwargs):
    return {'action': name, 'args': kwargs, 'message': name}


class TestVision(unittest.TestCase):
    def test_screen_relative_coordinates(self):
        frame = ScreenFrame(b'image', 1920, 1080, 42, time.monotonic())
        self.assertEqual(frame.pixel('0', '0'), (0, 0))
        self.assertEqual(frame.pixel('1000', '1000'), (1919, 1079))
        self.assertEqual(frame.pixel('500', '500'), (960, 540))
        second = ScreenFrame(b'image', 1920, 1080, 42, time.monotonic(), -1920, 0)
        self.assertEqual(second.pixel('0', '0'), (-1920, 0))
        self.assertEqual(second.pixel('1000', '1000'), (-1, 1079))
        for v in ('-1', '1001', '1.5', 'abc'):
            with self.subTest(v=v), self.assertRaises(ValueError):
                frame.pixel(v, '200')

    def test_visual_click_rejects_missing_image(self):
        planner = ScriptedPlanner([action('screen_click', x='500', y='500', target='次へ')])
        windows = FakeWindows(); logs = []
        agent = VoiceAgent(FakeBridge(), windows, planner, logs.append, lambda _: True,
                           True, threading.Event())
        agent.run('次へを押して')
        self.assertEqual(windows.calls, [])
        self.assertIn('画像観測', '\n'.join(logs))

    def test_visual_fallback_observe_then_click(self):
        frame = ScreenFrame(b'fake-jpeg', 1920, 1080, 42, time.monotonic())
        planner = ScriptedPlanner([
            action('screen_observe'),
            action('screen_click', x='500', y='500', target='次へ'),
            action('done'),
        ])
        windows = FakeWindows()
        with patch('app.agent.capture_screen', return_value=frame) as screenshot:
            agent = VoiceAgent(FakeBridge(), windows, planner, lambda _: None,
                               lambda _: True, False, threading.Event())
            agent.run('画面の次へを押して')
        screenshot.assert_called_once_with(42)
        self.assertEqual(planner.images, [None, b'fake-jpeg', None])
        self.assertEqual(windows.calls, [(960, 540, False)])

    def test_foreground_change_prevents_visual_click(self):
        frame = ScreenFrame(b'fake-jpeg', 1000, 1000, 42, time.monotonic())
        windows = FakeWindows(); logs = []
        class ChangingPlanner(ScriptedPlanner):
            def next_action(self, *args, **kwargs):
                response = super().next_action(*args, **kwargs)
                if response['action'] == 'screen_click':
                    windows.hwnd = 99
                return response
        planner = ChangingPlanner([action('screen_observe'),
                                   action('screen_click', x='300', y='200', target='次へ'),
                                   action('done')])
        with patch('app.agent.capture_screen', return_value=frame):
            agent = VoiceAgent(FakeBridge(), windows, planner, logs.append,
                               lambda _: True, False, threading.Event())
            agent.run('次へ')
        self.assertEqual(windows.calls, [])
        self.assertTrue(any('切り替わった' in l for l in logs))

    def test_cloud_image_needs_separate_consent(self):
        planner = ScriptedPlanner([action('screen_observe')], provider='gemini')
        logs = []
        with patch('app.agent.capture_screen') as screenshot:
            agent = VoiceAgent(FakeBridge(), FakeWindows(), planner, logs.append,
                               lambda _: True, True, threading.Event(), allow_cloud_image=False)
            agent.run('画面を見て')
        screenshot.assert_not_called()
        self.assertTrue(any('許可' in l for l in logs))

    def test_visual_click_requires_0_to_1000_and_label(self):
        for x, y, target in [('1001', '5', '次へ'), ('0', '-1', '戻る'), ('1', '1', '')]:
            with self.subTest(x=x, y=y, target=target), self.assertRaises(ValueError):
                validate_action(action('screen_click', x=x, y=y, target=target))


class TestStartMenuLaunch(unittest.TestCase):
    def test_finds_exact_and_unique_partial_shortcut(self):
        from pathlib import Path
        with patch('app.win_actions.start_menu_shortcuts', return_value={
            'visual studio code': Path('C:/example/Visual Studio Code.lnk'),
            'メモアプリ': Path('C:/example/メモアプリ.lnk'),
        }):
            self.assertEqual(app_shortcut('visual studio code').stem, 'Visual Studio Code')
            self.assertEqual(app_shortcut('メモ').stem, 'メモアプリ')
            with self.assertRaises(FileNotFoundError):
                app_shortcut('unknown')
            with self.assertRaises(ValueError):
                app_shortcut('PowerShell')
            with self.assertRaises(ValueError):
                app_shortcut('cmd.exe')


if __name__ == '__main__':
    unittest.main()