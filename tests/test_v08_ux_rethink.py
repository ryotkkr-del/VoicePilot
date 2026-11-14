"""Regression tests for the UX rethink based on the three product requirements."""
from __future__ import annotations

from pathlib import Path
import threading
import tempfile
import unittest
from unittest.mock import patch

from app.fast_commands import installed_app_action
from app.agent import VoiceAgent
from app.win_actions import find_files


class DummyWindows:
    def __init__(self):
        self.calls = []
    def is_chrome_foreground(self):
        return False
    def snapshot(self):
        return {"active_title": "メモ帳", "controls": [], "windows": []}
    def act(self, action, args):
        self.calls.append((action, args))
        return "実行しました"

class DummyBridge:
    connected = False

class PlannerNeverCalled:
    provider = "ollama"
    def next_action(self, *args):
        raise AssertionError("fast path must not call the AI")

class PlannerDone:
    provider = "ollama"
    def __init__(self, message):
        self.message = message
    def next_action(self, *args):
        return {"action": "done", "args": {}, "message": self.message}

class InstalledAppsTests(unittest.TestCase):
    def setUp(self):
        self.apps = {
            "discord": Path('Discord.lnk'),
            "visual studio code": Path('Visual Studio Code.lnk'),
            "uninstall app": Path('Uninstall App.lnk'),
        }

    def test_exact_installed_app_runs_without_llm(self):
        windows = DummyWindows()
        agent = VoiceAgent(DummyBridge(), windows, PlannerNeverCalled(),
                           lambda _: None, lambda _: True, False, threading.Event())
        with patch('app.agent.start_menu_shortcuts', return_value=self.apps):
            result = agent.run('Discordを開いて')
        self.assertEqual(windows.calls, [('windows_launch', {'app': 'discord'})])
        self.assertIn(result.status, ('unverified', 'completed'))

    def test_normalized_whitespace_and_safe_exact_lookup(self):
        self.assertEqual(installed_app_action('Visual Studio Codeを起動して', self.apps),
                         ('windows_launch', {'app': 'visual studio code'}))
        self.assertIsNone(installed_app_action('Visual Studioを開いて', self.apps))
        self.assertIsNone(installed_app_action('昨日のPDFを開いて', self.apps))
        self.assertIsNone(installed_app_action('Uninstall Appを開いて', self.apps))

    def test_ambiguous_installations_are_not_guessed(self):
        apps = {'my app': Path('my app.lnk'), 'myapp': Path('myapp.lnk')}
        self.assertIsNone(installed_app_action('My Appを開いて', apps))

    def test_unknown_app_falls_back_to_planner(self):
        windows = DummyWindows()
        agent = VoiceAgent(DummyBridge(), windows, PlannerDone('どのアプリですか？'),
                           lambda _: None, lambda _: True, False, threading.Event())
        with patch('app.agent.start_menu_shortcuts', return_value={}):
            result = agent.run('何かを開いて')
        self.assertEqual(windows.calls, [])
        self.assertIn('どのアプリですか', result.summary)
        self.assertEqual(result.status, 'incomplete')

class SearchUXTests(unittest.TestCase):
    def test_cancellation_skips_file_tree(self):
        with tempfile.TemporaryDirectory() as td:
            Path(td, 'sample.pdf').write_text('hello', encoding='utf8')
            with patch('app.win_actions.user_roots', return_value=[Path(td)]):
                with self.assertRaises(InterruptedError):
                    find_files('sample', cancelled=lambda: True)
                self.assertEqual(len(find_files('sample', cancelled=lambda: False)), 1)

    def test_recent_file_order_is_stable_and_bounded(self):
        windows = DummyWindows()
        session = {'recent_files': ['old.pdf', 'second.pdf']}
        agent = VoiceAgent(DummyBridge(), windows, PlannerNeverCalled(),
                           lambda _: None, lambda _: True, False, threading.Event(), session=session)
        with patch('app.agent.find_files', return_value=['second.pdf', 'new.pdf']) as search:
            agent._execute('files_find', {'query':'new'})
        search.assert_called_once()
        self.assertEqual(session['recent_files'], ['old.pdf', 'second.pdf', 'new.pdf'])
        self.assertEqual(agent._found_paths, {'old.pdf','second.pdf','new.pdf'})

class ClarificationTests(unittest.TestCase):
    def test_question_is_displayed(self):
        agent = VoiceAgent(DummyBridge(), DummyWindows(), PlannerDone('どちらのファイルを開きますか？'),
                           lambda _: None, lambda _: True, False, threading.Event())
        result = agent.run('これを開いて')
        self.assertIn('どちらのファイル', result.summary)
        self.assertEqual(result.status, 'incomplete')

    def test_llm_completion_claim_with_no_action_does_not_mislead(self):
        agent = VoiceAgent(DummyBridge(), DummyWindows(), PlannerDone('完了しました'),
                           lambda _: None, lambda _: True, False, threading.Event())
        result = agent.run('これを開いて')
        self.assertIn('操作は実行されませんでした', result.summary)
        self.assertNotIn('完了しました', result.summary)

if __name__ == '__main__':
    unittest.main()

class BrowserFallbackTests(unittest.TestCase):
    def test_chrome_without_extension_requests_uia_fallback(self):
        class W(DummyWindows):
            allow_chrome_uia_fallback = False
            def snapshot(self):
                self.was_enabled = self.allow_chrome_uia_fallback
                return super().snapshot()
        windows = W()
        agent = VoiceAgent(DummyBridge(), windows, PlannerDone('対象が見つかりません'),
                           lambda _: None, lambda _: True, False, threading.Event())
        agent._context()
        self.assertTrue(windows.was_enabled)

    def test_connected_chrome_dom_keeps_fast_uia_skip(self):
        class W(DummyWindows):
            allow_chrome_uia_fallback = True
            def is_chrome_foreground(self):
                return True
            def snapshot(self):
                self.was_enabled = self.allow_chrome_uia_fallback
                return super().snapshot()
        class B:
            connected = True
            def call(self, name):
                self._called = name
                return {'elements': []}
        windows = W()
        bridge = B()
        agent = VoiceAgent(bridge, windows, PlannerDone('完了しました'),
                           lambda _: None, lambda _: True, False, threading.Event())
        agent._context()
        self.assertEqual(bridge._called, 'snapshot')
        self.assertFalse(windows.was_enabled)

class CancellationTests(unittest.TestCase):
    def test_cancel_during_file_search_reports_interrupted_even_at_step_limit(self):
        stop = threading.Event()
        class SearchPlanner:
            provider = 'ollama'
            def next_action(self, *_):
                return {"action": "files_find", "args": {"query": "report"}, "message": "検索"}
        def cancelled_search(query, cancelled=None):
            stop.set()
            raise InterruptedError('cancel')
        agent = VoiceAgent(DummyBridge(), DummyWindows(), SearchPlanner(),
                           lambda _: None, lambda _: True, False, stop, max_steps=1)
        with patch('app.agent.find_files', side_effect=cancelled_search):
            result = agent.run('reportを探して')
        self.assertEqual(result.status, 'interrupted')

    def test_bridge_timeout_releases_its_future(self):
        from concurrent.futures import Future
        from app.bridge import BrowserBridge
        bridge = BrowserBridge('local', port=18771)
        bridge._loop = object()
        bridge._connected.set()
        future = Future()
        def thread_safe(coroutine, loop):
            coroutine.close()  # no actual event loop in this unit test
            return future
        with patch('app.bridge.asyncio.run_coroutine_threadsafe', side_effect=thread_safe):
            with self.assertRaisesRegex(TimeoutError, '操作済みの可能性'):
                bridge.call('snapshot', timeout=0.001)
        self.assertTrue(future.cancelled())