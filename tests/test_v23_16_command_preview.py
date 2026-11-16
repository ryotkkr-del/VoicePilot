"""P0-1/P0-5: strictly non-executing voice command preview regression."""
from __future__ import annotations

import ast
from pathlib import Path
import queue
import sys
import threading
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.command_preview import preview_command
from app import first_trial


ROOT = Path(__file__).resolve().parents[1]


class CommandPreviewTests(unittest.TestCase):
    def test_known_commands_are_only_static_candidates(self):
        for phrase, expected in (
            ("メモ帳を開いて", "メモ帳を開く"),
            ("電卓を起動して", "電卓を開く"),
            ("下にスクロールして", "下にスクロール"),
            ("一番下までスクロールして", "下端までスクロール"),
            ("前のページに戻って", "ブラウザの前のページ"),
            ("ダウンロードを開いて", "ダウンロードを表示"),
            ("コピーして", "コピー"),
            ("アプリ内の編集を元に戻して", "編集の取り消し"),
        ):
            with self.subTest(phrase=phrase):
                kind, detail = preview_command(phrase)
                self.assertEqual(kind, "recognized")
                self.assertIn(expected, detail)
                self.assertIn("実行はしません", detail)
                self.assertNotIn("実行しました", detail)

    def test_ambiguous_and_unsupported_are_not_guessed(self):
        for phrase in (
            "元に戻して", "ファイルを全部消して", "支払いを完了して",
            "管理者でPowerShellを起動して", "なにかやって",
            "Chromeに表示されているあのボタンをクリックして",
        ):
            with self.subTest(phrase=phrase):
                kind, description = preview_command(phrase)
                self.assertEqual(kind, "unknown")
                self.assertNotIn("候補：", description)

    def test_sensitive_search_terms_never_repeated_in_preview(self):
        secret = "FAKE_PASSWORD_ONLY_FOR_TEST_03953"
        phrase = f"Googleで{secret}を検索して"
        kind, description = preview_command(phrase)
        self.assertEqual(kind, "recognized")
        self.assertIn("Web検索", description)
        self.assertNotIn(secret, description)
        self.assertNotIn(phrase, description)

    def test_no_transcript_echoed_when_unknown(self):
        fake_secret = "SECRET_DO_NOT_ECHO_EXAMPLE_448"
        kind, detail = preview_command(fake_secret)
        self.assertEqual(kind, "unknown")
        self.assertNotIn(fake_secret, detail)
        self.assertEqual(preview_command("")[0], "empty")
        self.assertEqual(preview_command("A" * 2001)[0], "unknown")
        self.assertEqual(preview_command(None)[0], "empty")

    def test_preview_module_never_imports_execution_or_cloud_code(self):
        code = (ROOT / "app/command_preview.py").read_text("utf-8")
        tree = ast.parse(code)
        imports = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports.append(node.module or "")
            if isinstance(node, ast.Import):
                imports.extend(x.name for x in node.names)
        for restricted in (
            "agent", "planner", "win_actions", "bridge", "requests",
            "socket", "subprocess", "ctypes", "urllib", "voicepilot_bootstrap",
        ):
            self.assertFalse(any(restricted in name for name in imports), restricted)
        self.assertNotIn("exec(", code)
        self.assertNotIn("eval(", code)


class GUIWorkerBoundaryTests(unittest.TestCase):
    def view(self):
        view = first_trial.LocalTrial.__new__(first_trial.LocalTrial)
        view.root = Mock()
        view.root.after.side_effect = None
        view.mic = None
        view.recording = False
        view.working = True
        view.closed = False
        view.generation = 3
        view.recognizer = Mock()
        view.worker_results = queue.Queue(maxsize=1)
        view.allow_download = Mock()
        view.status = Mock()
        view.preview_state = Mock()
        view.record_btn = Mock()
        view.stop_btn = Mock()
        view._set_output = Mock()
        view._set_preview = Mock()
        view._status = Mock()
        return view

    def test_background_worker_cannot_touch_tk_before_main_thread_polls(self):
        view = self.view()
        view.recognizer.transcribe.return_value = "メモ帳を開いて"
        worker = threading.Thread(
            target=view._transcribe,
            args=(np.ones(1600, np.float32), view.generation, False)
        )
        worker.start()
        worker.join(timeout=5)
        self.assertFalse(worker.is_alive())
        view.root.after.assert_not_called()
        view._set_output.assert_not_called()
        view._set_preview.assert_not_called()
        self.assertTrue(view.working)
        view._poll_worker()
        self.assertFalse(view.working)
        self.assertEqual(view._set_output.call_args.args[0], "メモ帳を開いて")
        self.assertEqual(view._set_preview.call_args.args[0][0], "recognized")
        self.assertIn("メモ帳を開く", view._set_preview.call_args.args[0][1])

    def test_canceled_recognition_hides_text_and_action_preview(self):
        view = self.view()
        view.stop()
        self.assertTrue(view.working)
        self.assertEqual(view.generation, 4)
        view.recognizer.transcribe.return_value = "SECRET_NOT_SHOWN_1357"
        view._transcribe(np.ones(1600, np.float32), 3, False)
        view._poll_worker()
        self.assertFalse(view.working)
        view._set_output.assert_not_called()
        view._set_preview.assert_not_called()
        view.record_btn.configure.assert_called_with(state="normal")

    def test_ui_polls_again_if_worker_not_ready(self):
        view = self.view()
        view._poll_worker()
        view.root.after.assert_called_once_with(120, view._poll_worker)
        self.assertTrue(view.working)

    def test_closed_ui_discards_queued_result_without_tk(self):
        view = self.view()
        view.worker_results.put((view.generation, "secret dummy", "completed"))
        view.closed = True
        view._poll_worker()
        view.root.after.assert_not_called()
        view._set_output.assert_not_called()
        view._set_preview.assert_not_called()
        self.assertTrue(view.working)

    def test_ui_preview_is_canned_not_raw_command_data(self):
        view = self.view()
        fake_key = "EXAMPLE_PRIVATE_TOKEN_5387"
        view.worker_results.put((3, f"Googleで{fake_key}を検索して", "認識完了"))
        view._poll_worker()
        self.assertNotIn(fake_key, str(view._set_preview.call_args))


if __name__ == "__main__":
    unittest.main()
