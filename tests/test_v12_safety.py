"""v0.12 security boundaries, all offline and independent of Windows hardware."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock

from app.agent import VoiceAgent
from app.safety import approval_summary, unfamiliar_destination, is_sensitive_field


def build_agent(confirm=None):
    bridge = Mock(connected=False)
    windows = Mock()
    windows.is_chrome_foreground.return_value = False
    windows.snapshot.return_value = {"active_title": "メモ帳", "controls": [], "windows": []}
    planner = Mock(provider="ollama")
    agent = VoiceAgent(bridge, windows, planner, lambda _: None,
                       confirm or (lambda _: True), False, threading.Event())
    return agent, bridge, windows, planner


class SafetyPolicy(unittest.TestCase):
    def test_requires_approval_for_model_inferred_external_navigation(self):
        agent, *_ = build_agent()
        observed = {"browser": {"url": "https://trusted.example/article"}}
        self.assertTrue(agent._requires_confirmation("browser_open", {"url": "https://other.example/"}, observed, "記事を調べて"))
        self.assertFalse(agent._requires_confirmation("browser_open", {"url": "https://trusted.example/next"}, observed, "記事を調べて"))
        self.assertFalse(unfamiliar_destination("https://other.example/", "https://other.example/を開いて", observed))

    def test_submit_form_dialog_and_external_link_need_approval(self):
        agent, *_ = build_agent()
        for extra in ({"in_form": True}, {"in_dialog": True}, {"type": "submit"},
                      {"external_origin": "https://outside.example"},
                      {"label": "送信"}, {"label": "続行"}):
            context = {"browser": {"elements": [{"id": "id1", "label": "次へ", "type": "button", **extra}]}}
            self.assertTrue(agent._requires_confirmation("browser_click", {"id": "id1"}, context), extra)
        context = {"browser": {"elements": [{"id": "id1", "label": "検索", "type": "button"}]}}
        self.assertFalse(agent._requires_confirmation("browser_click", {"id": "id1"}, context))

    def test_existing_draft_needs_approval(self):
        agent, *_ = build_agent()
        context = {"browser": {"elements": [{"id": "a", "label": "メモ", "type": "text", "has_value": True}]}}
        self.assertTrue(agent._requires_confirmation("browser_fill", {"id": "a", "text": "new"}, context))
        context["browser"]["elements"][0]["has_value"] = False
        self.assertFalse(agent._requires_confirmation("browser_fill", {"id": "a", "text": "new"}, context))

    def test_sensitive_fields_rejected(self):
        agent, *_ = build_agent()
        for secret in ("API Key", "ワンタイム認証コード", "card number", "secret", "パスワード"):
            self.assertTrue(is_sensitive_field(secret))
            context = {"browser": {"elements": [{"id": "a", "label": secret, "type": "text"}]}}
            with self.assertRaises(ValueError):
                agent._requires_confirmation("browser_fill", {"id": "a", "text": "test"}, context)

    def test_blind_input_paste_and_screen_click_need_approval(self):
        agent, *_ = build_agent()
        self.assertTrue(agent._requires_confirmation("windows_type", {"text": "hello"}, {}))
        self.assertTrue(agent._requires_confirmation("windows_hotkey", {"keys": "ctrl+v"}, {}))
        self.assertTrue(agent._requires_confirmation("windows_hotkey", {"keys": "enter"}, {}))
        from app.vision import ScreenFrame
        from time import monotonic
        agent._frame = ScreenFrame(b"", 100, 100, 1, monotonic())
        agent.windows.inspect_point.return_value = {"name": "OK", "type": "Button"}
        self.assertTrue(agent._requires_confirmation("screen_click", {"x":"100","y":"100","target":"OK"}, {}))

    def test_confirmation_summary_excludes_typed_text(self):
        text = "never-show-private-password-123"
        for action, args, ctx in (
            ("browser_fill", {"id":"1", "text":text}, {"browser":{"url":"https://example.org/", "elements":[{"id":"1", "label":"検索欄"}]}}),
            ("windows_type", {"text":text}, {}),
            ("browser_open", {"url":"https://example.org/path?token="+text}, {}),
        ):
            self.assertNotIn(text, approval_summary(action, args, ctx))

    def test_untrusted_model_message_not_in_confirmation(self):
        confirmations = []
        agent, bridge, windows, planner = build_agent(lambda message: confirmations.append(message) or False)
        windows.snapshot.return_value = {"active_title":"メモ帳", "controls": [{"id":"0","name":"保存","type":"Button"}], "windows": []}
        planner.next_action.return_value = {"action":"windows_click", "args":{"id":"0"},
                                            "message":"AI says there is nothing dangerous"}
        outcome = agent.run("このボタンを押して")
        self.assertEqual(outcome.status, "interrupted")
        windows.act.assert_not_called()
        self.assertEqual(len(confirmations), 1)
        self.assertNotIn("AI says", confirmations[0])
        self.assertIn("保存", confirmations[0])

    def test_prompt_injection_external_navigation_rejected(self):
        confirmed = []
        agent, bridge, windows, planner = build_agent(lambda message: confirmed.append(message) or False)
        planner.next_action.return_value = {"action":"browser_open", "args":{"url":"https://attacker.example/collect"},
                                            "message":"The webpage said click here immediately"}
        outcome = agent.run("現在のページの要点を教えて")
        self.assertEqual(outcome.status, "interrupted")
        bridge.call.assert_not_called()
        self.assertEqual(len(confirmed), 1)
        self.assertIn("attacker.example", confirmed[0])
        self.assertNotIn("webpage said", confirmed[0])


class WindowsPrivacy(unittest.TestCase):
    def test_uia_edit_contents_are_not_exposed_to_model(self):
        import sys
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.win_actions import WindowsController

        class FakeControl:
            def __init__(self, name, is_password=False):
                self.name = name
                self.element_info = SimpleNamespace(control_type="Edit", is_password=is_password)
            def window_text(self): return self.name
            def is_visible(self): return True
            def children(self): return []

        plain = FakeControl("my confidential note 4873")
        secret = FakeControl("Password")
        root = Mock()
        root.children.return_value = [plain, secret]
        fake_module = SimpleNamespace(Desktop=Mock(return_value=Mock(window=Mock(return_value=root))))
        controller = object.__new__(WindowsController)
        controller._foreground_hwnd = lambda: 101
        controller.active_window_title = lambda: "メモ帳"
        # v0.22 blocks unknown Windows process identities; model a real Notepad.
        controller._foreground_process_name = lambda: "notepad.exe"
        controller.is_chrome_foreground = lambda: False
        controller.allow_chrome_uia_fallback = False
        with patch.dict(sys.modules, {"pywinauto": fake_module}), \
             patch("app.win_actions.current_process_elevated", return_value=False):
            snapshot = controller.snapshot()
        self.assertEqual(len(snapshot["controls"]), 1)
        self.assertEqual(snapshot["controls"][0]["name"], "入力欄")
        self.assertNotIn("confidential", str(snapshot))
        self.assertNotIn("Password", str(snapshot))

    def test_password_flag_can_be_callable(self):
        from app.win_actions import _is_password_control
        self.assertTrue(_is_password_control(Mock(element_info=Mock(is_password=lambda: True))))
        self.assertFalse(_is_password_control(Mock(element_info=Mock(is_password=lambda: False))))


class PairingClipboard(unittest.TestCase):
    def test_only_pairing_code_is_erased(self):
        from app.main import VoicePilotApp
        window = Mock()
        window.clipboard_get.side_effect = ["different clipboard text", "pairing-secret"]
        app = object.__new__(VoicePilotApp)
        app.root = window
        app.config = {"token": "pairing-secret"}
        app._clear_expired_pairing_clipboard()
        window.clipboard_clear.assert_not_called()
        app._clear_expired_pairing_clipboard()
        window.clipboard_clear.assert_called_once()


if __name__ == '__main__':
    unittest.main()