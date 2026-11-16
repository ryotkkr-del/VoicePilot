"""ChatGPT web handoff: tests run offline and consume zero cloud allowances."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import RunOutcome
from app.chatgpt_handoff import extract_chatgpt_request, prepare_chatgpt_handoff
from app.main import VoicePilotApp
from app.ux import presentation
from app.voice_feedback import result_announcement


class ExplicitRoutingTests(unittest.TestCase):
    def test_only_explicit_chatgpt_requests_route(self):
        self.assertIsNone(extract_chatgpt_request("Chromeを開いて"))
        self.assertIsNone(extract_chatgpt_request("PDFを開いて"))
        self.assertEqual(extract_chatgpt_request("チャッピーにPythonを説明して"), "Pythonを説明して")
        self.assertEqual(extract_chatgpt_request("ChatGPTでバグの原因を考えて"), "バグの原因を考えて")
        self.assertEqual(extract_chatgpt_request("チャッピーを開いて"), "")
        self.assertEqual(extract_chatgpt_request("ChatGPTを開いて"), "")

    def test_no_cloud_planner_for_explicit_chatgpt(self):
        app = object.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()  # simulate explicitly authorized test
        app.set_phase = Mock()
        app.restore_target = Mock()
        app._run_chatgpt = Mock(return_value=RunOutcome("handoff", "準備済み"))
        with patch("app.main.Planner", side_effect=AssertionError("should not use plan budget")):
            result = app._run_agent("チャッピーにこんにちはと言って", {})
        self.assertEqual(result.status, "handoff")
        app._run_chatgpt.assert_called_once_with("こんにちはと言って")
        app.restore_target.assert_not_called()


class HandoffTests(unittest.TestCase):
    def test_browser_extension_prepares_but_never_submits(self):
        bridge = Mock(connected=True)
        bridge.call.return_value = {"opened": True, "prepared": True}
        copy = Mock()
        open_browser = Mock()
        result = prepare_chatgpt_handoff("テスト質問", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff")
        bridge.call.assert_called_once_with("chatgpt_prepare", {"prompt": "テスト質問"}, timeout=11)
        copy.assert_not_called()
        open_browser.assert_not_called()
        self.assertIn("送信は自分で", result.message)

    def test_existing_draft_falls_back_to_clipboard_without_overwriting(self):
        bridge = Mock(connected=True)
        bridge.call.return_value = {"opened": True, "prepared": False, "reason": "existing_draft"}
        copy = Mock()
        open_browser = Mock()
        result = prepare_chatgpt_handoff("質問", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff_clipboard")
        copy.assert_called_once_with("質問")
        open_browser.assert_not_called()  # existing ChatGPT tab already open

    def test_no_extension_opens_browser_and_copies(self):
        bridge = Mock(connected=False)
        copy, open_browser = Mock(), Mock(return_value=True)
        result = prepare_chatgpt_handoff("Hello", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff_clipboard")
        open_browser.assert_called_once_with("https://chatgpt.com/")
        copy.assert_called_once_with("Hello")
        bridge.call.assert_not_called()

    def test_no_extension_empty_request_only_opens_chatgpt(self):
        bridge = Mock(connected=False)
        copy, open_browser = Mock(), Mock(return_value=True)
        result = prepare_chatgpt_handoff("", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff")
        copy.assert_not_called()

    def test_cancellation_does_not_open_or_copy(self):
        bridge = Mock(connected=False)
        copy, open_browser = Mock(), Mock()
        result = prepare_chatgpt_handoff("質問", bridge, copy, open_browser, cancelled=lambda: True)
        self.assertEqual(result.stage, "interrupted")
        copy.assert_not_called()
        open_browser.assert_not_called()

    def test_open_failed_does_not_claim_success(self):
        bridge = Mock(connected=False)
        copy, open_browser = Mock(), Mock(return_value=False)
        result = prepare_chatgpt_handoff("質問", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff_clipboard")
        self.assertIn("ブラウザで", result.message)
        self.assertNotIn("開き、", result.message)

    def test_too_long_request_is_rejected_before_network(self):
        bridge = Mock(connected=True)
        with self.assertRaises(ValueError):
            prepare_chatgpt_handoff("A"*4001, bridge, Mock(), Mock())
        bridge.call.assert_not_called()

    def test_connection_failure_falls_back_without_chatgpt_api_call(self):
        bridge = Mock(connected=True)
        bridge.call.side_effect = TimeoutError("Chrome response lost")
        copy, open_browser = Mock(), Mock(return_value=True)
        result = prepare_chatgpt_handoff("質問", bridge, copy, open_browser)
        self.assertEqual(result.stage, "handoff_clipboard")
        self.assertEqual(copy.call_count, 1)
        self.assertEqual(open_browser.call_count, 1)

    def test_report_does_not_claim_sent(self):
        self.assertIn("送信", result_announcement("handoff"))
        self.assertNotIn("回答", result_announcement("handoff"))
        self.assertIn("送信", presentation("handoff").hint)
        self.assertEqual(presentation("handoff_clipboard").tone, "neutral")


if __name__ == "__main__":
    unittest.main()