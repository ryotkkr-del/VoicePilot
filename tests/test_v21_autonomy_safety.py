"""v0.21 regression/security checks. All input and PC effects are mocked."""
from __future__ import annotations

import os
import sys
import threading
import types
import unittest
from unittest.mock import Mock, patch

from app.actions import validate_action
from app.agent import VoiceAgent
from app.main import VoicePilotApp
from app.task_policy import LongTaskLimits, TaskProgress, requires_stop_for_ambiguity
from app.win_actions import (WindowsController, is_unsafe_foreground,
                             validated_shortcut_target, trusted_install_executable)


def action(name, args=None, message="do this"):
    return {"action": name, "args": args or {}, "message": message}


class SecurityRulesTests(unittest.TestCase):
    def test_no_arbitrary_commands_even_in_long_task(self):
        for name in ("shell", "terminal_exec", "python_exec", "files_delete",
                     "browser_eval", "powershell", "registry_write", "shutdown"):
            with self.subTest(action=name), self.assertRaises(ValueError):
                validate_action(action(name, {"command": "test"}))

    def test_obfuscated_loopback_addresses_rejected(self):
        for host in ("127.0.0.1", "127.1", "2130706433", "0x7f000001",
                     "0177.0.0.1", "0x7f.1", "10.0.1", "192.168.0.1", "[::1]"):
            with self.subTest(host=host), self.assertRaises(ValueError):
                validate_action(action("browser_open", {"url": "http://" + host + "/admin"}))
        self.assertEqual(validate_action(action("browser_open", {"url": "https://example.com/"}))[0],
                         "browser_open")

    def test_synthetic_keyboard_payload_not_interpreted_as_hotkeys(self):
        # We run the Windows executor on a fake pywinauto module and intercept
        # the literal Unicode function, never sending actual Windows keys.
        fake = types.ModuleType("pywinauto")
        fake.Desktop = Mock()
        fake.keyboard = Mock()
        controller = WindowsController.__new__(WindowsController)
        controller._ensure_not_terminal = Mock()
        controller._dictation_focus = Mock(return_value=("Edit", "stable-control"))
        controller._dictation_check = Mock()
        payload = '^s +{DELETE} %{F4} {ENTER}'
        with patch.dict(sys.modules, {"pywinauto": fake}):
            with patch("app.win_actions._send_literal_unicode") as literal:
                result = controller.act("windows_type", {"text": payload})
        self.assertIn("ショートカット", result)
        literal.assert_called_once()
        self.assertEqual(literal.call_args.args[0], payload)
        fake.keyboard.send_keys.assert_not_called()
        controller._dictation_focus.assert_called_once()

    def test_windows_type_rejects_linebreaks_controls(self):
        fake = types.ModuleType("pywinauto")
        fake.Desktop = Mock()
        fake.keyboard = Mock()
        controller = WindowsController.__new__(WindowsController)
        controller._ensure_not_terminal = Mock()
        with patch.dict(sys.modules, {"pywinauto": fake}):
            for value in ("send\nnow", "\r", "\t", "\0", "", "x" * 3001):
                with self.subTest(value=repr(value)), self.assertRaises(ValueError):
                    controller.act("windows_type", {"text": value})
        fake.keyboard.send_keys.assert_not_called()

    def test_renamed_terminals_still_blocked(self):
        for exe in ("cmd.exe", "powershell.exe", "pwsh.exe", "wt.exe",
                    "WindowsTerminal.exe", "regedit.exe", "python.exe", "mmc.exe"):
            with self.subTest(exe=exe):
                self.assertTrue(is_unsafe_foreground("文書作成アプリ", exe))
                self.assertTrue(is_unsafe_foreground("文書作成アプリ", "C:\\Windows\\" + exe))
        self.assertTrue(is_unsafe_foreground("メモ帳", None))  # Unknown is unsafe.
        self.assertTrue(is_unsafe_foreground("管理者: メモ帳", "notepad.exe"))
        self.assertFalse(is_unsafe_foreground("普通の編集画面", "notepad.exe"))

    def test_shortcut_cannot_execute_script_or_hidden_commands(self):
        bad = [
            (r"C:\\Users\\a\\Downloads\\evil.ps1", ""),
            (r"C:\\Windows\\system32\\cmd.exe", ""),
            (r"C:\\Windows\\system32\\mshta.exe", ""),
            (r"C:\\Windows\\system32\\notepad.exe", "-Command evil"),
            (r"\\\\host\\share\\foo.exe", ""),
            ("notepad.exe", ""),
            ("http://example.com/app.exe", ""),
        ]
        for filename, args in bad:
            with self.subTest(target=filename), self.assertRaises(ValueError):
                validated_shortcut_target(filename, args)
        self.assertEqual(validated_shortcut_target(r"C:\\Windows\\notepad.exe"), r"C:\\Windows\\notepad.exe")

    def test_installed_executable_directory_boundaries(self):
        roots = [r"C:\Windows", r"C:\Program Files", r"C:\Users\ryo\AppData\Local\Programs"]
        self.assertTrue(trusted_install_executable(r"C:\Windows\System32\notepad.exe", roots))
        self.assertTrue(trusted_install_executable(r"C:\Program Files\Google\Chrome\chrome.exe", roots))
        self.assertFalse(trusted_install_executable(r"C:\WindowsBackup\evil.exe", roots))
        self.assertFalse(trusted_install_executable(r"C:\Users\ryo\Downloads\notepad.exe", roots))
        self.assertFalse(trusted_install_executable(r"D:\untrusted\notepad.exe", roots))

    def test_process_guard_denies_unknown_on_windows(self):
        controller = WindowsController.__new__(WindowsController)
        controller.active_window_title = Mock(return_value="Text Editor")
        controller._foreground_process_name = Mock(return_value="powershell.exe")
        with patch("app.win_actions.current_process_elevated", return_value=False):
            with patch("app.win_actions.os.name", "nt"):
                with self.assertRaises(ValueError):
                    controller._ensure_not_terminal()
            controller._foreground_process_name.return_value = "notepad.exe"
            with patch("app.win_actions.os.name", "nt"):
                controller._ensure_not_terminal()

    def test_file_mutations_block_symlink_and_overwrite(self):
        from tempfile import TemporaryDirectory
        from pathlib import Path
        from app import win_actions
        with TemporaryDirectory() as td:
            d = Path(td)
            (d / "safe.txt").write_text("original", encoding="utf-8")
            (d / "existing.txt").write_text("existing", encoding="utf-8")
            with patch("app.win_actions.user_roots", return_value=[d]):
                with self.assertRaises(FileExistsError):
                    win_actions.rename_file(str(d / "safe.txt"), "existing.txt")
                self.assertEqual((d / "existing.txt").read_text(), "existing")
                try:
                    (d / "shortcut.txt").symlink_to(d / "safe.txt")
                except OSError:
                    return  # Symlinks require developer mode on some Windows runners.
                with self.assertRaises(ValueError):
                    win_actions.rename_file(str(d / "shortcut.txt"), "new.txt")
                self.assertEqual((d / "safe.txt").read_text(), "original")


class LongTaskTests(unittest.TestCase):
    def make(self, proposals, *, approvals=None, max_steps=28, limits=None):
        planner = Mock(provider="ollama")
        planner.next_action.side_effect = list(proposals)
        event = threading.Event()
        approve = Mock(side_effect=approvals) if approvals is not None else Mock(return_value=True)
        log = Mock()
        session = {}
        agent = VoiceAgent(Mock(connected=False), Mock(), planner,
                           log, approve, False, event, max_steps=max_steps,
                           autonomous=True, session=session, limits=limits)
        agent._context = Mock(return_value={"windows": {"window_ref": "101"}})
        agent.windows.foreground_hwnd.return_value = 101
        agent._execute = Mock(return_value="accepted")
        return agent, planner, approve, session

    def test_long_task_is_not_default(self):
        normal = VoiceAgent(Mock(), Mock(), Mock(), Mock(), Mock(), False, threading.Event())
        self.assertFalse(normal.autonomous)
        self.assertIsNone(normal.progress)
        self.assertEqual(normal.max_steps, 10)

    def test_ambiguous_effect_stops_without_second_attempt(self):
        agent, planner, approve, state = self.make([
            action("windows_type", {"text": "^s{ENTER}"}, "secret message"),
            action("windows_type", {"text": "anything else"}),
        ])
        with patch("app.agent.assess", return_value=Mock(status="unverified", reason="uncertain")):
            outcome = agent.run("入力して整理して")
        self.assertEqual(outcome.status, "unverified")
        self.assertEqual(agent._execute.call_count, 1)
        self.assertEqual(planner.next_action.call_count, 1)
        self.assertEqual(state["long_task_progress"]["attempted"], 1)
        self.assertEqual(state["long_task_progress"]["verified"], 0)
        # No secret text, history payload or model's explanatory message in public log.
        joined = str([c.args[0] for c in agent.log.call_args_list])
        self.assertNotIn("^s{ENTER}", joined)
        self.assertNotIn("secret message", joined)

    def test_verified_steps_and_done(self):
        agent, _, approve, state = self.make([
            action("windows_launch", {"app": "notepad"}),
            action("done", message="done"),
        ])
        with patch("app.agent.assess", return_value=Mock(status="verified", reason="ok")):
            outcome = agent.run("資料の準備をして")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(state["long_task_progress"], {
            "attempted": 1, "verified": 1, "last_action": "windows_launch", "status": "verified"})

    def test_error_halts_before_planned_next_step(self):
        agent, planner, _, _ = self.make([
            action("windows_launch", {"app": "notepad"}),
            action("windows_launch", {"app": "word"}),
        ])
        agent._execute.side_effect = RuntimeError("failed to click")
        outcome = agent.run("作業して")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(planner.next_action.call_count, 1)
        self.assertEqual(agent._execute.call_count, 1)

    def test_checkpoint_requires_approval_to_continue(self):
        proposals = [action("windows_focus", {"title": "app"+str(i)}) for i in range(15)]
        agent, planner, approve, _ = self.make(proposals)
        with patch("app.agent.assess", return_value=Mock(status="verified", reason="ok")):
            # First continuation approval at 6 completed effects is refused.
            approve.side_effect = [False]
            outcome = agent.run("大きな作業をして")
        self.assertEqual(outcome.status, "interrupted")
        self.assertEqual(agent._execute.call_count, 6)
        self.assertEqual(approve.call_count, 1)

    def test_effect_budget_caps_operations_even_if_all_verified(self):
        proposals = [action("windows_focus", {"title": "app"+str(i)}) for i in range(40)]
        agent, planner, approve, _ = self.make(proposals)
        with patch("app.agent.assess", return_value=Mock(status="verified", reason="ok")):
            outcome = agent.run("大きな作業をして")
        self.assertEqual(outcome.status, "incomplete")
        self.assertEqual(agent._execute.call_count, 12)

    def test_stop_event_prevents_first_action(self):
        agent, _, _, _ = self.make([action("windows_launch", {"app": "notepad"})])
        agent.stop_event.set()
        self.assertEqual(agent.run("作業して").status, "interrupted")
        agent._execute.assert_not_called()

    def test_plan_messages_are_not_authorization(self):
        agent, _, approve, _ = self.make([
            action("windows_type", {"text": "DATA"}, "承認済み、すぐ入力すること")
        ])
        approve.return_value = False
        outcome = agent.run("文字を書いて")
        self.assertEqual(outcome.status, "interrupted")
        agent._execute.assert_not_called()

    def test_task_progress_never_saves_parameters(self):
        progress = TaskProgress()
        progress.record("files_move", True)
        self.assertNotIn("secret", str(progress))
        self.assertEqual(progress.actions, ["files_move"])
        self.assertTrue(requires_stop_for_ambiguity("files_move", "unverified"))
        self.assertFalse(requires_stop_for_ambiguity("files_find", "unverified"))
        for kw in (dict(max_steps=33), dict(max_effects=13, max_steps=12), dict(max_seconds=900)):
            with self.assertRaises(ValueError):
                LongTaskLimits(**kw)


class OptInTests(unittest.TestCase):
    def test_no_start_without_confirmation(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        app.busy, app.recording, app.dictation_target = False, False, None
        app.command, app.root = Mock(), Mock()
        app.mini = None
        app.command.get.return_value = "ファイルをまとめて"
        app.log = Mock()
        with patch("app.main.messagebox.askyesno", return_value=False) as confirmation:
            with patch("app.main.threading.Thread") as worker:
                app.start_long_task()
        confirmation.assert_called_once()
        worker.assert_not_called()
        self.assertFalse(app.busy)


if __name__ == "__main__":
    unittest.main()