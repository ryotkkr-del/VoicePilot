import threading
import unittest

from app.fast_commands import direct_action
from app.agent import VoiceAgent, Planner, validate_action


class TestDirectCommands(unittest.TestCase):
    def test_local_fast_actions(self):
        cases = {
            'メモ帳を開いて': ('windows_launch', {'app':'notepad'}),
            'Chromeを開いて': ('windows_launch', {'app':'chrome'}),
            '下にスクロールして': ('smart_scroll', {'direction':'down'}),
            'グーグルで深層学習を検索して': ('browser_search', {'query':'深層学習'}),
            'ダウンロードフォルダを開いて': ('files_show_folder', {'folder':'downloads'}),
            'アプリ内の編集を元に戻して': ('windows_hotkey', {'keys':'ctrl+z'}),
        }
        for spoken, expected in cases.items():
            with self.subTest(spoken=spoken):
                self.assertEqual(direct_action(spoken), expected)

    def test_ambiguous_goes_to_planner(self):
        for spoken in ['メモ帳を開いてから何か書いて', 'このサイトの投稿を消して', 'キャンセルしないで', '操作を元に戻して', '']:
            self.assertIsNone(direct_action(spoken))


class FakeBridge:
    connected = False
    def call(self, action, args=None):
        raise AssertionError('Browser should not be called')


class FakeWindows:
    def __init__(self): self.calls = []
    def is_chrome_foreground(self): return False
    def snapshot(self): return {'active_title':'メモ帳', 'windows':[], 'controls': []}
    def act(self, action, args):
        self.calls.append((action,args))
        return {'ok':True}


class CountingPlanner:
    provider='gemini'
    def __init__(self): self.called = 0
    def next_action(self, *args, **kwargs):
        self.called+=1
        return {'action':'done','args':{},'message':'終わり'}


class TestAgentPrivacy(unittest.TestCase):
    def test_cloud_no_consent_must_not_plan(self):
        planner=CountingPlanner()
        logs=[]
        agent=VoiceAgent(FakeBridge(),FakeWindows(),planner,logs.append,lambda _:True,False,threading.Event())
        agent.run('この文章を書き写して')
        self.assertEqual(planner.called,0)
        self.assertTrue(any('許可' in s for s in logs))

    def test_cancel_prevents_fast_execution(self):
        planner=CountingPlanner(); windows=FakeWindows()
        stop=threading.Event(); stop.set()
        agent=VoiceAgent(FakeBridge(), windows, planner, lambda _:None,
                         lambda _:True, False, stop)
        agent.run('メモ帳を開いて')
        self.assertEqual(windows.calls, [])
        self.assertEqual(planner.called, 0)

    def test_simple_operation_skips_llm(self):
        planner=CountingPlanner(); windows=FakeWindows()
        agent=VoiceAgent(FakeBridge(),windows,planner,lambda _:None,lambda _:True,False,threading.Event())
        agent.run('メモ帳を開いて')
        self.assertEqual(windows.calls,[('windows_launch',{'app':'notepad'})])
        self.assertEqual(planner.called,0)

    def test_stop_event_skips_planning(self):
        planner=CountingPlanner(); stop=threading.Event();stop.set()
        agent=VoiceAgent(FakeBridge(),FakeWindows(),planner,lambda _:None,lambda _:True,True,stop)
        agent.run('この文章を書き写して')
        self.assertEqual(planner.called,0)

    def test_sensitive_button_asks_approval(self):
        planner=CountingPlanner(); agent=VoiceAgent(FakeBridge(),FakeWindows(),planner,lambda _:None,lambda _:True,True,threading.Event())
        ctx={'browser':{'elements':[{'id':'x','label':'削除する','type':'button'}]}}
        self.assertTrue(agent._requires_confirmation('browser_click',{'id':'x'},ctx))
        with self.assertRaises(ValueError):
            agent._requires_confirmation('browser_click',{'id':'y'},ctx)


class TestActionSafety(unittest.TestCase):
    def test_url_local_network_denied(self):
        for url in ('http://127.0.0.1:5000','http://localhost:8501','http://device.local'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_action({'action':'browser_open','args':{'url':url}})

    def test_only_three_known_folders(self):
        with self.assertRaises(ValueError):
            validate_action({'action':'files_show_folder','args':{'folder':'C:\\Windows'}})
        self.assertEqual(validate_action({'action':'files_show_folder','args':{'folder':'downloads'}})[0], 'files_show_folder')

    def test_bad_scroll_denied(self):
        with self.assertRaises(ValueError):
            validate_action({'action':'windows_scroll','args':{'direction':'sideways'}})


if __name__ == '__main__': unittest.main()