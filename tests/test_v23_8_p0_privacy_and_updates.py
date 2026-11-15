"""Roadmap P0-3/P0-4 regressions; no cloud calls or real file updates."""
from __future__ import annotations

import io
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import zipfile

from app.agent import VoiceAgent
from app.screen_understanding import ScreenAssistant, screen_context
from voicepilot_bootstrap import UpdateError, _safe_name, extract_checked


SECRET = "FAKE_SECRET_ACCOUNT_DATA_DO_NOT_SEND"


class CloudScreenDisclosureTests(unittest.TestCase):
    def context(self):
        return {
            "windows": {
                "active_title": "Notepad",
                "controls": [{"type": "Button", "name": "OK"}],
                "windows": [{"title": "Private background window " + SECRET}],
            },
            "browser": {
                "title": "Current page",
                "url": "https://example.org/",
                "headings": ["Public heading"],
                "elements": [],
            },
        }

    def test_screen_context_excludes_background_titles_only_for_cloud(self):
        data = self.context()
        remote = screen_context(data, None, None, cloud=True)
        self.assertNotIn("open_windows", remote["windows"])
        self.assertNotIn(SECRET, str(remote))
        local = screen_context(data, None, None, cloud=False)
        self.assertIn(SECRET, str(local))
        self.assertIn("windows", data["windows"])

    def test_gemini_screen_question_never_forwards_background_windows_or_image(self):
        windows = Mock()
        windows.foreground_hwnd.return_value = 101
        bridge = Mock(connected=False)
        planner = Mock(provider="gemini")
        planner.explain_screen.return_value = "現在のメモ帳です"
        agent = ScreenAssistant(
            windows=windows, bridge=bridge, planner=planner,
            context_provider=lambda: self.context(),
            stop_event=threading.Event(), allow_cloud_ui=True,
            allow_cloud_image=False,
        )
        with patch("app.screen_understanding.capture_screen") as screenshot:
            self.assertIn("メモ帳", agent.answer("画面を説明して"))
        screenshot.assert_not_called()
        payload = planner.explain_screen.call_args.args
        self.assertNotIn(SECRET, str(payload))
        self.assertIsNone(payload[2])

    def test_cloud_history_does_not_repeat_previous_form_contents_and_paths(self):
        planner = Mock(provider="gemini")
        planner.next_action.return_value = {"action": "done", "args": {}, "message": "done"}
        agent = VoiceAgent(
            Mock(connected=False), Mock(), planner, Mock(), Mock(),
            True, threading.Event()
        )
        history = [{
            "action": "browser_fill",
            "args": {"id": "1", "text": SECRET},
            "verification": "verified",
        }, {
            "action": "files_open",
            "args": {"path": "C:/Users/private/" + SECRET},
            "verification": "unverified",
        }]
        agent._plan_checked("次に進んで", {}, history, None)
        context_sent = planner.next_action.call_args.args
        self.assertNotIn(SECRET, str(context_sent))
        self.assertEqual(context_sent[2][0]["action"], "browser_fill")
        self.assertEqual(context_sent[2][0]["verification"], "verified")
        self.assertIn(SECRET, str(history), "Local evidence must stay untouched")

    def test_local_history_still_available_to_local_planner(self):
        planner = Mock(provider="ollama")
        planner.next_action.return_value = {"action": "done", "args": {}, "message": "done"}
        agent = VoiceAgent(
            Mock(connected=False), Mock(), planner, Mock(), Mock(),
            False, threading.Event()
        )
        history = [{"action": "browser_fill", "args": {"text": SECRET}}]
        agent._plan_checked("もう一度確認して", {}, history, None)
        self.assertIn(SECRET, str(planner.next_action.call_args))


class Win32ArchivePathTests(unittest.TestCase):
    def test_windows_reserved_devices_and_alternate_data_stream_names_rejected(self):
        for name in [
            "app/CON.py", "app/nul.txt", "app/PRN.md", "app/aux.js",
            "app/COM1.js", "app/LPT9.json", "app/secret.py:alternate.py",
            "app/x?.py", "app/x*.js", "app/x|name.py",
            "app/folder /nested.py", "app/folder./nested.py",
        ]:
            with self.subTest(name=name), self.assertRaises(UpdateError):
                _safe_name(name)
        self.assertEqual(_safe_name("app/valid_source.py"), "app/valid_source.py")

    def test_duplicate_filenames_differing_by_case_rejected_before_extraction(self):
        names = {
            "VERSION": "0.23.8",
            "voicepilot_bootstrap.py": "updater",
            "setup_and_run.bat": "launcher",
            "requirements.txt": "requirements",
            "app/main.py": "original",
            "app/Main.py": "malicious duplicate",
            "app/__init__.py": "",
            "chrome-extension/manifest.json": '{"manifest_version":3}',
        }
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            zip_path = root / "release.zip"
            with zipfile.ZipFile(zip_path, "w") as zf:
                for name, data in names.items():
                    zf.writestr(name, data)
            output = root / "staging"
            output.mkdir()
            with self.assertRaisesRegex(UpdateError, "重複"):
                extract_checked(zip_path, output, "0.23.8")
            self.assertEqual(list(output.rglob("*")), [],
                             "No files should be extracted when metadata is unsafe")

    def test_missing_or_invalid_update_does_not_touch_installed_data(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            live_file = root / "installed" / "app" / "main.py"
            live_file.parent.mkdir(parents=True)
            live_file.write_text("existing user installation", encoding="utf-8")
            archive = root / "danger.zip"
            with zipfile.ZipFile(archive, "w") as zf:
                zf.writestr("app/NUL.py", "unsafe")
                zf.writestr("VERSION", "0.23.8")
            staged = root / "staged"
            staged.mkdir()
            with self.assertRaises(UpdateError):
                extract_checked(archive, staged, "0.23.8")
            self.assertEqual(live_file.read_text(encoding="utf-8"),
                             "existing user installation")


if __name__ == "__main__":
    unittest.main()
