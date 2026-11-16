"""Small UI state tests; no Windows microphone, display, or paid API needed."""
import threading
import unittest
from unittest.mock import Mock

from app.main import VoicePilotApp


class SimpleField:
    def __init__(self, value=""):
        self.value = value
        self.changed = False

    def get(self):
        return self.value

    def delete(self, *_):
        self.value = ""
        self.changed = True

    def insert(self, _index, value):
        self.value = value
        self.changed = True

    def xview_moveto(self, _pos):
        pass


class UXLifecycleTest(unittest.TestCase):
    def _instance(self):
        app = VoicePilotApp.__new__(VoicePilotApp)
        from app.control_permit import DesktopControlPermit
        app.control_permit = DesktopControlPermit()
        app.control_permit.arm()
        app._refresh_control_lock_ui = Mock()
        app.recording = False
        app.busy = True
        app._record_after_cancel = False
        app.stop_event = threading.Event()
        app.speaker = Mock()
        app.log = Mock()
        app.set_phase = Mock()
        app._apply_phase = Mock()
        return app

    def test_press_speak_while_busy_requests_cancel_and_new_record(self):
        app = self._instance()
        VoicePilotApp.toggle_record(app)
        self.assertTrue(app._record_after_cancel)
        self.assertTrue(app.stop_event.is_set())
        app.set_phase.assert_called_with("cancelling")

    def test_press_speak_twice_cancels_queued_recording(self):
        app = self._instance()
        VoicePilotApp.toggle_record(app)
        VoicePilotApp.toggle_record(app)
        self.assertFalse(app._record_after_cancel)
        self.assertTrue(app.stop_event.is_set())

    def test_explicit_abort_clears_pending_record(self):
        app = self._instance()
        app._record_after_cancel = True
        VoicePilotApp.abort(app)
        self.assertFalse(app._record_after_cancel)

    def test_worker_completion_starts_record_only_after_exit(self):
        app = self._instance()
        app._record_after_cancel = True
        app.stop_event.set()
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        app.root = Mock()
        VoicePilotApp.mark_idle(app, "interrupted")
        self.assertFalse(app.busy)
        self.assertFalse(app._record_after_cancel)
        app.root.after_idle.assert_called_once()

    def test_worker_exception_after_cancel_still_allows_next_record(self):
        app = self._instance()
        app._record_after_cancel = True
        app.stop_event.set()
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        app.root = Mock()
        VoicePilotApp.mark_idle(app, "error")
        app.root.after_idle.assert_called_once()

    def test_work_completed_without_cancel_does_not_queue_recording(self):
        app = self._instance()
        app._record_after_cancel = False
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        app.root = Mock()
        VoicePilotApp.mark_idle(app, "completed")
        app.root.after_idle.assert_not_called()

    def test_transcript_does_not_destroy_a_newly_typed_draft(self):
        app = self._instance()
        app.command = SimpleField("今入力している別の指示")
        app._text_at_record_stop = "古い入力欄"
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        VoicePilotApp.show_transcript(app, "認識した言葉")
        self.assertEqual(app.command.get(), "今入力している別の指示")
        self.assertFalse(app.command.changed)

    def test_transcript_is_shown_when_draft_unchanged(self):
        app = self._instance()
        app.command = SimpleField("古い入力欄")
        app._text_at_record_stop = "古い入力欄"
        app.ui = Mock()
        app.ui.post.side_effect = lambda callback: callback()
        VoicePilotApp.show_transcript(app, "認識した言葉")
        self.assertEqual(app.command.get(), "認識した言葉")

    def test_settings_toggle_does_not_force_window_resize(self):
        app = self._instance()
        app.settings_card = Mock()
        app.settings_card.winfo_manager.return_value = False
        app.settings_button = Mock()
        app.pair_card = Mock()
        app.root = Mock()
        VoicePilotApp.toggle_settings(app)
        app.root.geometry.assert_not_called()
        app.settings_card.pack.assert_called_once()


if __name__ == "__main__":
    unittest.main()