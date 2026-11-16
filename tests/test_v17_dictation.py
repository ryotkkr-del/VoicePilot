"""Local dictation routing and safety. Requires no Windows or microphone."""
import threading
import unittest
from unittest.mock import Mock

from app.dictation import (DictationController, DictationError, DictationTarget,
                           dictation_intent)
from app.main import VoicePilotApp


class DictationPhraseTests(unittest.TestCase):
    def test_mode_commands_are_exact(self):
        self.assertEqual(dictation_intent('文章入力を始めて。', False), ('start', ''))
        self.assertEqual(dictation_intent('入力終了', True), ('stop', ''))
        self.assertIsNone(dictation_intent('今日は音声入力を始めてみたい', False))
        self.assertEqual(dictation_intent('文章入力開始', True), ('already_on', ''))

    def test_punctuation_and_literal_commands(self):
        self.assertEqual(dictation_intent('改行', True), ('insert', '\n'))
        self.assertEqual(dictation_intent('句点。', True), ('insert', '。'))
        self.assertEqual(dictation_intent('スペース', True), ('insert', ' '))
        self.assertEqual(dictation_intent('今日は改行という言葉を使った', True),
                         ('insert', '今日は改行という言葉を使った'))
        self.assertEqual(dictation_intent('文章を保存して', True), ('insert', '文章を保存して'))
        self.assertIsNone(dictation_intent('メールを送って', False))

    def test_reject_bad_content(self):
        for value in ('x' * 2001, 'hello\tworld', 'hello\x00world'):
            with self.subTest(value=value[:12]), self.assertRaises(DictationError):
                dictation_intent(value, True)


class DictationControllerTests(unittest.TestCase):
    def make(self, *, chrome=False, bridge_connected=True):
        windows = Mock()
        windows.foreground_hwnd.return_value = 777
        windows.is_chrome_foreground.return_value = chrome
        windows.dictation_probe.return_value = 'Edit:1,2,3'
        bridge = Mock()
        bridge.connected = bridge_connected
        bridge.call.return_value = {'token':'abc','tab_id':25}
        return DictationController(windows, bridge), windows, bridge

    def test_browser_uses_pinned_tab_and_not_agent(self):
        d, w, b = self.make(chrome=True)
        target = d.start()
        self.assertEqual(target, DictationTarget('browser', 777, 'abc', 25))
        b.call.return_value = {'inserted':True, 'verified':True}
        self.assertTrue(d.insert(target, 'こんにちは^s{ENTER}'))
        b.call.assert_called_with('dictation_insert',
                                  {'token':'abc','tab_id':25,'text':'こんにちは^s{ENTER}'},
                                  timeout=12)

    def test_windows_is_limited_to_pinned_focus(self):
        d, w, b = self.make()
        t = d.start()
        self.assertFalse(d.insert(t, 'Hello + {ENTER}'))
        w.dictation_insert.assert_called_once_with('Hello + {ENTER}', 'Edit:1,2,3')
        b.call.assert_not_called()
        w.foreground_hwnd.return_value = 123
        with self.assertRaisesRegex(DictationError, 'ウィンドウが変わりました'):
            d.insert(t, 'secret')
        self.assertEqual(w.dictation_insert.call_count, 1)

    def test_chrome_without_extension_fails_closed(self):
        d, w, b = self.make(chrome=True, bridge_connected=False)
        with self.assertRaisesRegex(DictationError, '拡張機能'):
            d.start()

    def test_insert_rejects_controls_and_long_payload(self):
        d, w, b = self.make()
        target = d.start()
        for text in ('x'*2001, '', 'a\tb'):
            with self.assertRaises(DictationError):
                d.insert(target, text)
        w.dictation_insert.assert_not_called()


class AppDictationTests(unittest.TestCase):
    def minimal(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()
        app.dictation_target = None
        app.dictation = Mock()
        app.dictation.start.return_value = DictationTarget('windows', 777, 'Edit:123')
        app.dictation.insert.return_value = False
        app.restore_target = Mock()
        app.log = Mock()
        app.stop_event = threading.Event()
        return app

    def test_starts_and_dictates_without_llm(self):
        app = self.minimal()
        result = VoicePilotApp._run_dictation_command(app, '文章入力を始めて')
        self.assertEqual(result.status, 'completed')
        self.assertIsNotNone(app.dictation_target)
        result = VoicePilotApp._run_dictation_command(app, 'これはテスト文章です')
        self.assertEqual(result.status, 'unverified')
        app.dictation.insert.assert_called_once()
        called = app.dictation.insert.call_args
        self.assertEqual(called.args, (app.dictation_target, 'これはテスト文章です'))
        self.assertTrue(callable(called.kwargs["checkpoint"]))
        self.assertFalse(any('これはテスト' in c.args[0] for c in app.log.call_args_list))
        result = VoicePilotApp._run_dictation_command(app, '入力終了')
        self.assertEqual(result.status, 'completed')
        self.assertIsNone(app.dictation_target)

    def test_failed_focus_stops_mode(self):
        app = self.minimal()
        VoicePilotApp._run_dictation_command(app, '文章入力開始')
        app.dictation.insert.side_effect = DictationError('入力先が変わりました')
        result = VoicePilotApp._run_dictation_command(app, 'こんにちは')
        self.assertEqual(result.status, 'incomplete')
        self.assertIsNone(app.dictation_target)

    def test_cancellation_before_typing(self):
        app = self.minimal()
        VoicePilotApp._run_dictation_command(app, '文章入力開始')
        app.stop_event.set()
        result = VoicePilotApp._run_dictation_command(app, '保存します')
        self.assertEqual(result.status, 'interrupted')
        app.dictation.insert.assert_not_called()


if __name__ == '__main__':
    unittest.main()