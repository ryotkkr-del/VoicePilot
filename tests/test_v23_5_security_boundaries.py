"""v0.23.5 safety regressions; no real microphone, desktop input or network."""
from __future__ import annotations

import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from scripts import build_release


ROOT = Path(__file__).resolve().parents[1]


def make_agent(*, windows=None, confirm=None, planner=None):
    windows = windows or Mock()
    bridge = Mock(connected=False)
    planner = planner or Mock(provider="ollama")
    return VoiceAgent(
        bridge, windows, planner, Mock(),
        confirm or Mock(return_value=True), False, threading.Event()
    ), windows


class ForegroundBoundedInputTests(unittest.TestCase):
    def test_keyboard_scroll_and_text_reject_changed_window(self):
        agent, windows = make_agent()
        windows.foreground_hwnd.return_value = 202
        agent._expected_input_hwnd = 101
        for action, args in (
            ("windows_hotkey", {"keys": "ctrl+v"}),
            ("windows_scroll", {"direction": "down"}),
            ("windows_type", {"text": "safe words"}),
        ):
            with self.subTest(action=action), self.assertRaisesRegex(RuntimeError, "ウィンドウが変わった"):
                agent._execute(action, args)
        windows.act.assert_not_called()

    def test_stable_foreground_remains_executable(self):
        agent, windows = make_agent()
        windows.foreground_hwnd.return_value = 101
        agent._expected_input_hwnd = 101
        windows.act.return_value = "OK"
        self.assertEqual(agent._execute("windows_hotkey", {"keys": "ctrl+c"}), "OK")
        windows.act.assert_called_once_with("windows_hotkey", {"keys": "ctrl+c"})

    def test_fast_paste_fails_closed_after_confirmation_focus_switch(self):
        agent, windows = make_agent()
        windows.foreground_hwnd.side_effect = [101, 202]
        outcome = agent.run("貼り付けて")
        self.assertEqual(outcome.status, "incomplete")
        windows.act.assert_not_called()
        self.assertIsNone(agent._expected_input_hwnd)

    def test_fast_undo_fails_closed_after_confirmation_focus_switch(self):
        agent, windows = make_agent()
        windows.foreground_hwnd.side_effect = [101, 202]
        outcome = agent.run("このアプリの編集を取り消して")
        self.assertEqual(outcome.status, "incomplete")
        windows.act.assert_not_called()

    def test_planner_hotkey_requires_window_observation(self):
        proposal = {"action": "windows_hotkey", "args": {"keys": "ctrl+v"}, "message": "paste"}
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            proposal, {"action": "done", "args": {}, "message": "unable"}
        ]
        windows = Mock()
        windows.snapshot.return_value = {
            "window_ref": "", "active_title": "", "controls": [], "windows": []
        }
        agent, windows = make_agent(windows=windows, planner=planner)
        outcome = agent.run("アプリ内に貼り付けて")
        self.assertEqual(outcome.status, "incomplete")
        windows.act.assert_not_called()

    def test_planner_hotkey_rejects_changed_foreground_after_approval(self):
        proposal = {"action": "windows_hotkey", "args": {"keys": "ctrl+v"}, "message": "paste"}
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = [
            proposal, {"action": "done", "args": {}, "message": "unable"}
        ]
        windows = Mock()
        windows.snapshot.return_value = {
            "window_ref": "101", "active_title": "Notepad", "controls": [], "windows": []
        }
        windows.foreground_hwnd.return_value = 202
        agent, windows = make_agent(windows=windows, planner=planner)
        outcome = agent.run("現在のアプリ内でCtrl+Vを押してください")
        self.assertEqual(outcome.status, "incomplete")
        windows.act.assert_not_called()
        self.assertIsNone(agent._expected_input_hwnd)


class BrowserWriteApprovalTests(unittest.TestCase):
    def test_empty_field_still_needs_consent(self):
        agent, _ = make_agent()
        context = {"browser": {"elements": [
            {"id": "field", "label": "Search", "type": "text", "has_value": False}
        ]}}
        self.assertTrue(agent._requires_confirmation(
            "browser_fill", {"id": "field", "text": "possibly autosaved"}, context
        ))

    def test_sensitive_field_remains_prohibited(self):
        agent, _ = make_agent()
        context = {"browser": {"elements": [
            {"id": "field", "label": "API Key", "type": "text", "has_value": False}
        ]}}
        with self.assertRaises(ValueError):
            agent._requires_confirmation("browser_fill", {"id": "field", "text": "secret"}, context)


class ReleasePermissionTests(unittest.TestCase):
    def test_chrome_manifest_version_matches_app(self):
        version = (ROOT / "VERSION").read_text(encoding="utf-8").strip()
        manifest = json.loads((ROOT / "chrome-extension" / "manifest.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["version"], version)

    def test_packager_rejects_extension_version_mismatch(self):
        with tempfile.TemporaryDirectory() as temp:
            directory = Path(temp)
            (directory / "VERSION").write_text("0.23.5", encoding="utf-8")
            extension = directory / "chrome-extension"
            extension.mkdir()
            (extension / "manifest.json").write_text(
                json.dumps({"manifest_version": 3, "version": "0.23.4"}), encoding="utf-8"
            )
            with patch.object(build_release, "ROOT", directory):
                with self.assertRaisesRegex(RuntimeError, "バージョン"):
                    build_release.build(directory / "dist")

    def test_pr_tests_have_read_only_github_permissions(self):
        workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
        tests, publish = workflow.split("  publish:\n", 1)
        self.assertIn("permissions:\n  contents: read", tests)
        self.assertNotIn("contents: write", tests)
        self.assertIn("    permissions:\n      contents: write", publish)
        self.assertIn("if: github.event_name != 'pull_request'", publish)
        self.assertIn("needs: test-and-build", publish)


if __name__ == "__main__":
    unittest.main()
