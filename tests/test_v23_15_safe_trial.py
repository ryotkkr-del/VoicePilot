"""P0-5: safe, local microphone-only trial; no real microphone or cloud calls."""
from __future__ import annotations

import ast
import os
from pathlib import Path
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

import numpy as np

from app.audio import SpeechRecognizer
from app import first_trial
from voicepilot_bootstrap import _ALLOWED_FILES as BOOTSTRAP_ALLOWED
from app.signed_migration import _ALLOWED_FILES as MIGRATION_ALLOWED


ROOT = Path(__file__).resolve().parents[1]


class TrialModeTests(unittest.TestCase):
    def test_first_trial_explicitly_windows_nonadmin_only(self):
        self.assertTrue(first_trial.trial_platform_ok(
            platform="nt", elevated=False, build=22631))
        for values in [
            {"platform": "posix", "elevated": False, "build": 22631},
            {"platform": "nt", "elevated": True, "build": 22631},
            {"platform": "nt", "elevated": False, "build": 19045},
            {"platform": "nt", "elevated": None, "build": 22631},
        ]:
            with self.subTest(values=values):
                # On a Windows runner, elevated=None resolves the actual
                # account state; test only deterministic explicit cases.
                if values["elevated"] is None:
                    continue
                self.assertFalse(first_trial.trial_platform_ok(**values))

    def test_trial_module_has_no_agent_browser_cloud_update_imports(self):
        source = (ROOT / "app/first_trial.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        imported = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        self.assertFalse(any(any(bad in mod for bad in (
            "agent", "bridge", "planner", "win_actions", "voicepilot_bootstrap",
            "requests", "urllib", "socket", "subprocess", "gemini"
        )) for mod in imported))
        self.assertNotIn("VoiceAgent(", source)
        self.assertNotIn("check_and_stage(", source)
        self.assertNotIn("send_keys(", source)

    def test_default_recognizer_keeps_old_behavior_for_normal_app(self):
        recognizer = SpeechRecognizer("small")
        self.assertFalse(recognizer.local_files_only)

    def test_first_trial_offline_model_load_never_requests_download(self):
        whisper = types.ModuleType("faster_whisper")
        model = Mock()
        model.transcribe.return_value = ([], None)
        ctor = Mock(return_value=model)
        whisper.WhisperModel = ctor
        with patch.dict(sys.modules, {"faster_whisper": whisper}):
            recognizer = SpeechRecognizer("small", local_files_only=True)
            text = recognizer.transcribe(np.ones(1600, dtype=np.float32) * 0.1)
        self.assertEqual(text, "")
        self.assertTrue(ctor.call_args.kwargs["local_files_only"])

    def test_consent_allows_model_fetch_only_during_explicit_recognition(self):
        whisper = types.ModuleType("faster_whisper")
        model = Mock()
        model.transcribe.return_value = ([], None)
        ctor = Mock(return_value=model)
        whisper.WhisperModel = ctor
        with patch.dict(sys.modules, {"faster_whisper": whisper}):
            recognizer = SpeechRecognizer("small", local_files_only=False)
            recognizer.transcribe(np.ones(1600, dtype=np.float32) * 0.1)
        self.assertFalse(ctor.call_args.kwargs["local_files_only"])

    def mock_view(self):
        view = first_trial.LocalTrial.__new__(first_trial.LocalTrial)
        view.root = Mock()
        view.root.after.side_effect = lambda _delay, callback: callback()
        view.mic = None
        view.recording = False
        view.working = False
        view.closed = False
        view.generation = 1
        view.started = 0.0
        view.recognizer = None
        view.allow_download = Mock()
        view.allow_download.get.return_value = False
        view.platform_safe = True
        view.status = Mock()
        view.output = Mock()
        view.record_btn = Mock()
        view.stop_btn = Mock()
        view._set_output = Mock()
        view._status = Mock()
        return view

    def test_transcription_only_displays_text_does_not_automate_desktop(self):
        view = self.mock_view()
        result = "ダミー音声メッセージ"
        model = Mock()
        model.transcribe.return_value = result
        with patch("app.audio.SpeechRecognizer", return_value=model) as constructor:
            view.working = True
            view._transcribe(np.ones(1600, np.float32), view.generation, False)
        constructor.assert_called_once_with("small", local_files_only=True)
        self.assertFalse(view.working)
        self.assertEqual(view._set_output.call_args.args[0], result)
        view.stop_btn.configure.assert_called_with(state="disabled")

    def test_cancel_during_inference_does_not_publish_transcription(self):
        view = self.mock_view()
        view.working = True
        view.stop()
        self.assertTrue(view.working, "must wait for old inference to exit")
        self.assertEqual(view.generation, 2)
        result = "PRIVATE_STUB_TEXT"
        model = Mock()
        model.transcribe.return_value = result
        with patch("app.audio.SpeechRecognizer", return_value=model):
            view._transcribe(np.ones(1600, np.float32), 1, False)
        self.assertFalse(view.working)
        view._set_output.assert_not_called()
        self.assertNotIn(result, str(view._status.call_args_list))

    def test_explicit_whisper_network_permission_can_be_changed(self):
        view = self.mock_view()
        view.recognizer = Mock(local_files_only=True)
        view.recognizer.transcribe.return_value = ""
        view._transcribe(np.ones(1600, np.float32), view.generation, True)
        self.assertFalse(view.recognizer.local_files_only)
        view._transcribe(np.ones(1600, np.float32), view.generation, False)
        self.assertTrue(view.recognizer.local_files_only)

    def test_no_microphone_opens_until_start_button_pressed(self):
        view = self.mock_view()
        view.generation = 0
        mock_mic = Mock()
        with patch("app.audio.Microphone", return_value=mock_mic) as make_mic:
            self.assertFalse(view.recording)
            make_mic.assert_not_called()
            # No real sound device is touched; root.after is only scheduled.
            view.root.after.side_effect = None
            with patch("app.first_trial.time.monotonic", return_value=10.0):
                view.start()
        make_mic.assert_called_once()
        self.assertTrue(view.recording)
        mock_mic.start.assert_called_once()
        self.assertFalse(view.working)

    def test_trial_launcher_requires_explicit_package_install_consent(self):
        bat = (ROOT / "try_voicepilot.bat").read_text(encoding="utf-8")
        self.assertIn('INSTALL', bat)
        self.assertIn('app.first_trial', bat)
        self.assertNotIn('setup_and_run.bat', bat)
        self.assertNotIn('voicepilot_bootstrap.py apply', bat)
        self.assertNotIn('app.main', bat)
        self.assertNotIn('requirements.txt', bat)
        self.assertIn('pip install', bat)
        self.assertIn('ASK_INSTALL', bat)
        self.assertIn('numpy', bat)
        self.assertIn('faster-whisper', bat)

    def test_trial_launcher_in_release_and_signed_migration_allowlist(self):
        self.assertIn("try_voicepilot.bat", BOOTSTRAP_ALLOWED)
        self.assertIn("try_voicepilot.bat", MIGRATION_ALLOWED)


if __name__ == "__main__":
    unittest.main()
