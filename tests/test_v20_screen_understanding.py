"""v0.20 read-only explanations: explicit routing, minimization, local/cloud privacy."""
import os
import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent, RunOutcome
from app.main import VoicePilotApp
from app.planner import Planner
from app.screen_understanding import (ScreenAssistant, screen_question,
                                      screen_context, wants_pointer)
from app.ux import presentation
from app.voice_feedback import result_announcement


class ScreenRoutingTests(unittest.TestCase):
    def test_explicit_screen_questions(self):
        for text in ('今の画面を説明して', 'このエラーは何？', 'この表を要約して', '左上のアイコンは何？', '画面を解析して',
                     'これって何？', 'カーソルの位置を説明して', 'ページの内容を読んで'):
            with self.subTest(text=text):
                self.assertTrue(screen_question(text))

    def test_never_hijack_actions(self):
        for text in ('これを押して', 'そのボタンをクリックして', '画面の保存をして',
                     'この画面を見てクリックして', 'ページを開いて', 'Chromeを起動して',
                     'このテキストを入力して', ''):
            with self.subTest(text=text):
                self.assertFalse(screen_question(text))

    def test_pointer_requested_only_when_deictic(self):
        self.assertTrue(wants_pointer('これって何'))
        self.assertTrue(wants_pointer('カーソルにあるもの'))
        self.assertFalse(wants_pointer('今の画面を説明して'))

    def test_context_allowlists_and_bounds(self):
        observations = {
            'recent_instructions': ['secret command'], 'recent_files':['C:/PRIVATE'],
            'task_memory': {'secret':'hidden'},
            'browser': {'title':'Example', 'url':'https://example.org/',
                        'headings':['Heading'],
                        'elements':[{'id':'private-dom-id','label':'公開ボタン', 'tag':'button',
                                     'has_value':True,'value':'PRIVATE-DRAFT'},
                                    {'label':'SECRET', 'sensitive':True, 'tag':'input'}]},
            'windows': {'active_title':'Editor', 'window_ref':'PRIVATE_HANDLE',
                        'windows':[{'title':'Editor','handle':'1'}],
                        'controls':[{'id':'10','type':'Edit','name':'PRIVATE_TEXT'},
                                    {'id':'11','type':'Button','name':'保存'}]}}
        data = screen_context(observations, {'blocks':['this is a visible article.']},
                              {'name':'入力欄（内容は非表示）','type':'Edit'})
        wire = str(data)
        for prohibited in ('PRIVATE', 'secret command', 'private-dom-id', 'SECRET', 'PRIVATE_HANDLE', 'PRIVATE_TEXT'):
            self.assertNotIn(prohibited, wire)
        self.assertEqual(data['browser']['controls'][0]['label'], '公開ボタン')
        self.assertIn('visible article', wire)


class AnswerFlowTests(unittest.TestCase):
    def make(self, provider='ollama', ui=True, image=True, chrome=True):
        planner = Mock(provider=provider)
        planner.explain_screen.return_value = 'これはブラウザの設定画面です。'
        bridge = Mock(connected=chrome)
        bridge.call.return_value = {'blocks':['現在表示している段落']}
        windows = Mock()
        windows.is_chrome_foreground.return_value = chrome
        windows.pointer_control.return_value = {'name':'検索ボタン','type':'Button'}
        windows.foreground_hwnd.return_value = 22
        context_provider = Mock(return_value={'windows':{'active_title':'Browser','controls':[]}})
        helper = ScreenAssistant(bridge=bridge, windows=windows, planner=planner,
                                 context_provider=context_provider,
                                 stop_event=threading.Event(),
                                 allow_cloud_ui=ui, allow_cloud_image=image)
        return helper, planner, bridge, windows, context_provider

    def test_local_uses_transient_image_and_browser_text(self):
        helper, planner, bridge, windows, provider = self.make()
        with patch('app.screen_understanding.capture_screen') as capture:
            capture.return_value.jpeg = b'fake-jpeg'
            answer = helper.answer('この画面を説明して')
        self.assertEqual(answer, 'これはブラウザの設定画面です。')
        planner.explain_screen.assert_called_once()
        args = planner.explain_screen.call_args.args
        self.assertEqual(args[2], b'fake-jpeg')
        self.assertIn('現在表示している段落', str(args[1]))
        self.assertNotIn('cursor_element', args[1])
        bridge.call.assert_called_once_with('read_visible', timeout=8)

    def test_pointer_read_only(self):
        helper, planner, bridge, windows, provider = self.make(chrome=False)
        with patch('app.screen_understanding.capture_screen', side_effect=RuntimeError('not available')):
            helper.answer('これって何？')
        windows.pointer_control.assert_called_once()
        self.assertIn('検索ボタン', str(planner.explain_screen.call_args.args[1]))
        bridge.call.assert_not_called()

    def test_cloud_denied_prevents_observation(self):
        helper, planner, bridge, windows, provider = self.make(provider='gemini', ui=False)
        with patch('app.screen_understanding.capture_screen') as capture:
            with self.assertRaises(PermissionError):
                helper.answer('この画面は何？')
        provider.assert_not_called()
        capture.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_cloud_text_only_no_screenshot(self):
        helper, planner, bridge, windows, provider = self.make(provider='gemini', ui=True, image=False)
        with patch('app.screen_understanding.capture_screen') as capture:
            helper.answer('この画面は何？')
        capture.assert_not_called()
        self.assertIsNone(planner.explain_screen.call_args.args[2])

    def test_cloud_image_requires_separate_consent(self):
        helper, planner, bridge, windows, provider = self.make(provider='gemini', ui=True, image=True)
        with patch('app.screen_understanding.capture_screen') as capture:
            capture.return_value.jpeg = b'sample-jpeg'
            helper.answer('この画面は何？')
        capture.assert_called_once_with(22)
        self.assertEqual(planner.explain_screen.call_args.args[2], b'sample-jpeg')

    def test_cancel_before_observe(self):
        helper, planner, bridge, windows, provider = self.make()
        helper.stop_event.set()
        with self.assertRaises(InterruptedError):
            helper.answer('この画面を説明して')
        provider.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_answer_is_bounded(self):
        helper, planner, bridge, windows, provider = self.make()
        planner.explain_screen.return_value = 'A'*3000
        with patch('app.screen_understanding.capture_screen', side_effect=OSError('blocked')):
            self.assertEqual(len(helper.answer('この画面は何？')), 1200)


class PlannerScreenTests(unittest.TestCase):
    def test_ollama_only_read_only_plain_text_and_image(self):
        planner = Planner('ollama', ollama_model='gemma3:4b')
        with patch('app.planner.post_json', return_value={'response':'説明'}) as post:
            self.assertEqual(planner.explain_screen('何ですか', {'browser':{'title':'Hello'}}, b'x'), '説明')
        payload = post.call_args.args[1]
        self.assertNotIn('format',payload)
        self.assertEqual(payload['images'], ['eA=='])
        self.assertIn('信用できない', payload['system'])

    def test_gemini_text_only_and_no_tools(self):
        planner = Planner('gemini', gemini_key='test', gemini_model='gemini-2.5-flash')
        body = {'candidates':[{'content':{'parts':[{'text':'これは設定です'}]}}]}
        with patch('app.planner.post_json', return_value=body) as post:
            self.assertEqual(planner.explain_screen('今の画面', {'browser':{}}, None), 'これは設定です')
        payload = post.call_args.args[1]
        self.assertEqual(len(payload['contents'][0]['parts']), 1)
        self.assertNotIn('tools',payload)
        self.assertNotIn('responseSchema',payload.get('generationConfig',{}))


class AppIntegrationTests(unittest.TestCase):
    def test_read_only_route_never_invokes_agent_actions(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.set_phase = Mock()
        app.restore_target = Mock()
        app.bridge = Mock(connected=False)
        app.windows = Mock()
        app.log = Mock()
        app.confirm = Mock(return_value=True)
        app.stop_event = threading.Event()
        app.session = {}
        app.handsfree = Mock(enabled=False)
        runtime = {'provider':'ollama','key':'','gemini_model':'gemini-2.5-flash',
                   'ollama_model':'gemma3:4b','allow_cloud_ui':False,
                   'allow_cloud_image':False}
        with patch('app.main.Planner'), patch('app.main.ScreenAssistant') as SA, \
             patch.object(VoiceAgent,'run') as agent_run:
            SA.return_value.answer.return_value = '画面の説明です'
            outcome = VoicePilotApp._run_agent(app,'今の画面を説明して',runtime)
        self.assertEqual(outcome.status,'answered')
        agent_run.assert_not_called()
        app.log.assert_called_with('画面の説明：画面の説明です')

    def test_completed_answer_has_distinct_ui_state_and_voice_feedback(self):
        self.assertEqual(presentation('answered').title,'画面を説明しました')
        self.assertEqual(result_announcement('answered'),'画面の説明を表示しました。')


if __name__ == '__main__': unittest.main()