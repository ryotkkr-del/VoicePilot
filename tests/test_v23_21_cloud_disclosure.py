"""P0-3: no implicit upload of page text or captured monitor images.

All fixtures are dummy observations. No actual microphone, Chrome, Windows UI,
PIL capture, network request or credential is used.
"""
from __future__ import annotations

import threading
import time
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from app.control_permit import DesktopControlPermit
from app.screen_understanding import ScreenAssistant

DUMMY_IMAGE = b"NOT_A_REAL_JPEG_DO_NOT_UPLOAD"
DUMMY_TEXT = "FAKE_PRIVATE_VISIBLE_PAGE_TEXT_NEVER_TRANSMIT"


class ScreenExplanationConsentTests(unittest.TestCase):
    def make(self, *, confirm=None, image=True, chrome=True, permit=None):
        windows = Mock()
        windows.foreground_hwnd.return_value = 123
        windows.is_chrome_foreground.return_value = chrome
        bridge = Mock(connected=chrome)
        bridge.call.return_value = {"blocks": [DUMMY_TEXT]}
        planner = Mock(provider="gemini")
        planner.explain_screen.return_value = "説明"
        context_provider = Mock(return_value={
            "windows": {"active_title": "公開テスト画面", "controls": []}
        })
        helper = ScreenAssistant(
            bridge=bridge, windows=windows, planner=planner,
            context_provider=context_provider, stop_event=threading.Event(),
            allow_cloud_ui=True, allow_cloud_image=image,
            cloud_disclosure_confirm=confirm, control_permit=permit,
        )
        return helper, bridge, planner, context_provider, windows

    def test_denial_occurs_before_visible_text_read_or_capture(self):
        consent = Mock(return_value=False)
        helper, bridge, planner, provider, windows = self.make(confirm=consent)
        with patch("app.screen_understanding.capture_screen") as capture:
            with self.assertRaises(PermissionError):
                helper.answer("このページを説明して")
        consent.assert_called_once()
        provider.assert_not_called()
        bridge.call.assert_not_called()
        capture.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_no_consent_handler_fails_closed_for_gemini_content(self):
        helper, bridge, planner, _, _ = self.make(confirm=None)
        with patch("app.screen_understanding.capture_screen") as capture:
            with self.assertRaises(PermissionError):
                helper.answer("今の画面を説明して")
        bridge.call.assert_not_called()
        capture.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_confirmation_focus_switch_blocks_any_screen_read(self):
        helper, bridge, planner, provider, windows = self.make(
            confirm=lambda message: (setattr(windows.foreground_hwnd, "return_value", 999) or True))
        with patch("app.screen_understanding.capture_screen") as capture:
            with self.assertRaises(PermissionError):
                helper.answer("このページの意味を教えて")
        provider.assert_not_called()
        bridge.call.assert_not_called()
        capture.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_successful_one_time_confirm_and_screen_guard(self):
        consent = Mock(return_value=True)
        helper, bridge, planner, provider, windows = self.make(confirm=consent)
        with patch("app.screen_understanding.capture_screen") as capture:
            capture.return_value.jpeg = DUMMY_IMAGE
            self.assertEqual(helper.answer("現在の画面を教えて"), "説明")
        consent.assert_called_once()
        capture.assert_called_once_with(123)
        self.assertIn(DUMMY_TEXT, str(planner.explain_screen.call_args.args[1]))
        self.assertEqual(planner.explain_screen.call_args.args[2], DUMMY_IMAGE)

    def test_permission_revoked_during_confirmation_blocks_observation(self):
        permit = DesktopControlPermit(clock=lambda: 100)
        permit.arm()
        def confirmation(message):
            permit.revoke()
            return True
        helper, bridge, planner, provider, windows = self.make(
            confirm=confirmation, permit=permit)
        with patch("app.screen_understanding.capture_screen") as capture:
            with self.assertRaises(PermissionError):
                helper.answer("この画面を教えて")
        provider.assert_not_called()
        bridge.call.assert_not_called()
        capture.assert_not_called()
        planner.explain_screen.assert_not_called()

    def test_local_only_does_not_need_cloud_confirmation(self):
        helper, bridge, planner, provider, windows = self.make(confirm=None)
        planner.provider = "ollama"
        with patch("app.screen_understanding.capture_screen") as capture:
            capture.return_value.jpeg = DUMMY_IMAGE
            self.assertEqual(helper.answer("現在の画面を教えて"), "説明")
        planner.explain_screen.assert_called_once()


class AgentScreenshotConsentTests(unittest.TestCase):
    def make(self, *, permit=None, confirm=None):
        windows = Mock()
        windows.foreground_hwnd.return_value = 123
        bridge = Mock(connected=False)
        planner = Mock(provider="gemini")
        planner.next_action.side_effect = [
            {"action": "screen_observe", "args": {}, "message": ""},
            {"action": "done", "args": {}, "message": ""},
        ]
        agent = VoiceAgent(
            bridge, windows, planner,
            log=Mock(), confirm=confirm if confirm is not None else Mock(return_value=True),
            allow_cloud_ui=True, stop_event=threading.Event(), max_steps=3,
            allow_cloud_image=True, control_permit=permit,
        )
        agent._context = Mock(return_value={"windows": {
            "active_title": "公開テスト画面", "controls": [], "window_ref": "123",
        }})
        return agent, planner, windows

    def test_denied_capture_never_called_and_never_uploaded(self):
        consent = Mock(return_value=False)
        agent, planner, windows = self.make(confirm=consent)
        with patch("app.agent.capture_screen") as capture:
            outcome = agent.run("画面を見て")
        self.assertEqual(outcome.status, "interrupted")
        consent.assert_called_once()
        capture.assert_not_called()
        self.assertEqual(planner.next_action.call_count, 1)

    def test_uploaded_image_refused_if_focus_switches_after_capture(self):
        agent, planner, windows = self.make()
        snapshots = [0]
        def context():
            snapshots[0] += 1
            if snapshots[0] == 2:
                windows.foreground_hwnd.return_value = 456
            return {"windows": {"active_title": "公開テスト", "controls": [], "window_ref": "123"}}
        agent._context.side_effect = context
        fake_frame = SimpleNamespace(
            jpeg=DUMMY_IMAGE, active_hwnd=123, captured_at=time.monotonic())
        with patch("app.agent.capture_screen", return_value=fake_frame):
            outcome = agent.run("画面を見て")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(planner.next_action.call_count, 1,
                         "Must not transmit the captured image to the second Gemini call")
        self.assertIsNone(agent._frame)

    def test_permission_expiry_after_capture_prevents_upload(self):
        clock = [100.0]
        permit = DesktopControlPermit(clock=lambda: clock[0])
        permit.arm()
        agent, planner, windows = self.make(permit=permit)
        count = [0]
        def context():
            count[0] += 1
            if count[0] == 2:
                clock[0] += 301
            return {"windows": {"active_title": "公開テスト", "controls": [], "window_ref": "123"}}
        agent._context.side_effect = context
        fake_frame = SimpleNamespace(
            jpeg=DUMMY_IMAGE, active_hwnd=123, captured_at=time.monotonic())
        with patch("app.agent.capture_screen", return_value=fake_frame):
            outcome = agent.run("画面を見て")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(planner.next_action.call_count, 1)

    def test_confirmation_focus_change_prevents_capture(self):
        windows = Mock()
        def consent(message):
            windows.foreground_hwnd.return_value = 456
            return True
        agent, planner, existing = self.make(confirm=consent)
        agent.windows = windows
        windows.foreground_hwnd.return_value = 123
        with patch("app.agent.capture_screen") as capture:
            outcome = agent.run("今の画面")
        self.assertEqual(outcome.status, "incomplete")
        capture.assert_not_called()
        self.assertEqual(planner.next_action.call_count, 1)

    def test_approved_unchanged_image_can_be_sent_once(self):
        agent, planner, windows = self.make()
        fake_frame = SimpleNamespace(
            jpeg=DUMMY_IMAGE, active_hwnd=123, captured_at=time.monotonic())
        with patch("app.agent.capture_screen", return_value=fake_frame):
            agent.run("画面を見て")
        self.assertEqual(planner.next_action.call_count, 2)
        self.assertIsNone(planner.next_action.call_args_list[0].args[3])
        self.assertEqual(planner.next_action.call_args_list[1].args[3], DUMMY_IMAGE)


if __name__ == "__main__":
    unittest.main()
