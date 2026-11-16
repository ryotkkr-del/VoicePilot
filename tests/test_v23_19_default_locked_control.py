"""P0: real VoicePilot desktop effect authorization; mocked OS/UI only.

The permit is NEVER stored in user config, browser state or disk. All Windows
effects are blocked on a fresh process until the user clicks the local GUI
button and confirms a time-bounded authorization.
"""
from __future__ import annotations

from pathlib import Path
import threading
import unittest
from unittest.mock import Mock, patch

from app.agent import VoiceAgent
from app.control_permit import DesktopControlLocked, DesktopControlPermit
from app.main import VoicePilotApp


class VolatilePermissionTests(unittest.TestCase):
    def setUp(self):
        self.time = [100.0]
        self.permit = DesktopControlPermit(clock=lambda: self.time[0])

    def test_lock_is_initially_closed_and_cannot_be_persisted(self):
        self.assertFalse(self.permit.allowed())
        with self.assertRaises(DesktopControlLocked):
            self.permit.require()
        self.assertFalse(hasattr(self.permit, "config_file"))
        self.assertFalse(hasattr(self.permit, "save"))

    def test_explicit_arm_only_lasts_five_minutes(self):
        self.permit.arm()
        self.assertTrue(self.permit.allowed())
        self.time[0] += 299.99
        self.permit.require()
        self.time[0] += 0.02
        self.assertFalse(self.permit.allowed())
        with self.assertRaises(DesktopControlLocked):
            self.permit.require()

    def test_stop_revokes_without_rearming(self):
        self.permit.arm()
        self.permit.revoke()
        self.assertFalse(self.permit.allowed())

    def test_time_limit_must_be_bounded(self):
        for val in (0, -10, 29, 3601):
            with self.subTest(value=val), self.assertRaises(ValueError):
                DesktopControlPermit(ttl_seconds=val)

    def agent(self):
        bridge, windows, planner = Mock(), Mock(), Mock()
        agent = VoiceAgent(
            bridge, windows, planner, log=Mock(), confirm=Mock(return_value=True),
            allow_cloud_ui=True, stop_event=threading.Event(),
            control_permit=self.permit
        )
        return agent, bridge, windows, planner

    def test_while_locked_run_does_not_inspect_pc_or_contact_planner(self):
        agent, bridge, windows, planner = self.agent()
        outcome = agent.run("メモ帳を開いて")
        self.assertEqual(outcome.status, "incomplete")
        self.assertIn("ロック", outcome.summary)
        windows.snapshot.assert_not_called()
        windows.act.assert_not_called()
        bridge.call.assert_not_called()
        planner.next_action.assert_not_called()
        self.assertEqual(agent.session.get("recent_instructions", []), [])

    def test_every_fast_and_planned_effect_still_checked_at_dispatch(self):
        agent, bridge, windows, planner = self.agent()
        for action, args in (
            ("windows_launch", {"app": "notepad"}),
            ("windows_type", {"text": "harmless"}),
            ("windows_hotkey", {"keys": "ctrl+v"}),
            ("browser_click", {"id": "1"}),
            ("files_move", {"path": "dummy", "folder": "downloads"}),
        ):
            with self.subTest(action=action), self.assertRaises(DesktopControlLocked):
                agent._execute(action, args)
        windows.act.assert_not_called()
        bridge.call.assert_not_called()

    def test_authorized_windows_dispatch_then_revoke_blocks_next(self):
        agent, _, windows, _ = self.agent()
        self.permit.arm()
        windows.act.return_value = "mock success"
        self.assertEqual(agent._execute("windows_launch", {"app": "notepad"}), "mock success")
        windows.act.assert_called_once()
        self.permit.revoke()
        with self.assertRaises(DesktopControlLocked):
            agent._execute("windows_launch", {"app": "notepad"})
        windows.act.assert_called_once()

    def test_stop_event_cannot_be_bypassed_with_valid_permission(self):
        agent, _, windows, _ = self.agent()
        self.permit.arm()
        agent.stop_event.set()
        with self.assertRaises(InterruptedError):
            agent._execute("windows_launch", {"app": "notepad"})
        windows.act.assert_not_called()

    def test_paused_action_gate_rechecked_at_dispatch(self):
        agent, _, windows, _ = self.agent()
        self.permit.arm()
        agent.action_gate = Mock()
        agent.action_gate.checkpoint.return_value = False
        with self.assertRaises(InterruptedError):
            agent._execute("windows_launch", {"app": "notepad"})
        windows.act.assert_not_called()


class GUIControlTests(unittest.TestCase):
    def view(self):
        # Does not initialize Tk, microphone, browser, or any real PC backend.
        view = VoicePilotApp.__new__(VoicePilotApp)
        view.control_permit = DesktopControlPermit(clock=lambda: 100.0)
        view.control_lock_label = Mock()
        view.control_lock_button = Mock()
        view.log = Mock()
        view.root = Mock()
        view.busy = False
        view.recording = False
        view._closing = False
        return view

    def test_declined_gui_confirmation_does_not_arm(self):
        view = self.view()
        with patch("app.main.messagebox.askyesno", return_value=False):
            view.toggle_control_permission()
        self.assertFalse(view.control_permit.allowed())

    def test_gui_confirm_click_arms_and_revoke_calls_stop(self):
        view = self.view()
        with patch("app.main.messagebox.askyesno", return_value=True) as confirm:
            view.toggle_control_permission()
        confirm.assert_called_once()
        self.assertTrue(view.control_permit.allowed())
        view.abort = Mock()
        view.toggle_control_permission()
        self.assertFalse(view.control_permit.allowed())
        view.abort.assert_called_once()

    def test_no_gui_arming_during_active_work(self):
        view = self.view()
        view.busy = True
        with patch("app.main.messagebox.askyesno") as confirm:
            view.toggle_control_permission()
        confirm.assert_not_called()
        self.assertFalse(view.control_permit.allowed())

    def test_dictation_start_is_blocked_before_editor_inspection(self):
        view = self.view()
        view.dictation_target = None
        view.dictation = Mock()
        view.restore_target = Mock()
        view.stop_event = threading.Event()
        result = view._run_dictation_command("文章入力開始")
        self.assertEqual(result.status, "incomplete")
        view.dictation.start.assert_not_called()
        view.restore_target.assert_not_called()

    def test_audio_and_text_entry_cannot_start_without_explicit_unlock(self):
        view = self.view()
        view._stop_record = Mock()
        view._control_locked = Mock(return_value=True)
        view.speaker = Mock()
        view.mic = Mock()
        view.toggle_record()
        view.mic.start.assert_not_called()
        view.send_text()
        self.assertFalse(view.busy)
        self.assertEqual(view._control_locked.call_count, 2)

    def test_chatgpt_handoff_rejected_when_locked(self):
        view = self.view()
        with patch("app.main.prepare_chatgpt_handoff") as handoff:
            result = view._run_chatgpt("test text")
        self.assertEqual(result.status, "incomplete")
        handoff.assert_not_called()

    def test_abort_relocks_even_if_not_busy(self):
        view = self.view()
        view.control_permit.arm()
        view.speaker = Mock()
        view.handsfree = Mock(enabled=False)
        view.dictation_target = None
        view._record_after_cancel = False
        view._apply_phase = Mock()
        view.abort()
        self.assertFalse(view.control_permit.allowed())

    def test_production_agent_receives_same_permission(self):
        view = self.view()
        view.handsfree = Mock(enabled=False)
        view.recognizer = None
        view.set_phase = Mock()
        view.restore_target = Mock()
        view.bridge = Mock()
        view.windows = Mock()
        view.confirm = Mock()
        view.stop_event = threading.Event()
        view.session = {}
        with patch("app.main.Planner"), patch("app.main.VoiceAgent") as agent:
            agent.return_value.run.return_value = Mock(status="incomplete")
            view._run_agent("ダミー", {"provider": "ollama",
                                    "key": "", "gemini_model": "gemini",
                                    "ollama_model": "local",
                                    "allow_cloud_ui": False, "allow_cloud_image": False})
        self.assertIs(agent.call_args.kwargs["control_permit"], view.control_permit)


if __name__ == "__main__":
    unittest.main()
