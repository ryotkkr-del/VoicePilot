"""v0.6: outcome integrity, lightweight verification, context, safe cancellation."""
from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from app.agent import VoiceAgent, RunOutcome
from app.session_context import remember_instruction, remember_step, task_context
from app.verification import assess
from app.ux import presentation


class Bridge:
    connected = False


class Windows:
    def snapshot(self):
        return {"active_title": "メモ帳", "controls": [], "windows": []}

    def is_chrome_foreground(self):
        return False

    def quick_state(self):
        return {"active_title": "メモ帳", "windows": [{"title": "メモ帳"}]}


class Planner:
    provider = "ollama"

    def __init__(self, *actions, stop_on_call=None):
        self.actions = iter(actions)
        self.calls = 0
        self.stop_on_call = stop_on_call

    def next_action(self, *args):
        self.calls += 1
        if self.stop_on_call:
            self.stop_on_call()
        return next(self.actions)


def action(name, args=None):
    return {"action": name, "args": args or {}, "message": "AIが完了と言っています"}


def make_agent(windows=None, planner=None, stop=None, session=None):
    return VoiceAgent(Bridge(), windows or Windows(), planner or Planner(action("done")),
                      lambda _: None, lambda _: True, False,
                      stop or threading.Event(), session=session)


class ReliabilityTests(unittest.TestCase):
    def test_cancel_after_model_result_always_returns_outcome(self):
        stop = threading.Event()
        planner = Planner(action("windows_launch", {"app": "notepad"}), stop_on_call=stop.set)
        outcome = make_agent(planner=planner, stop=stop).run("あのアプリを開いて")
        self.assertIsInstance(outcome, RunOutcome)
        self.assertEqual(outcome.status, "interrupted")
        self.assertEqual(planner.calls, 1)

    def test_cancel_between_observations_does_not_return_none(self):
        stop = threading.Event()

        class StopWindows(Windows):
            def snapshot(self):
                stop.set()
                return super().snapshot()

        outcome = make_agent(windows=StopWindows(), stop=stop).run("文書を開いて")
        self.assertIsInstance(outcome, RunOutcome)
        self.assertEqual(outcome.status, "interrupted")

    def test_cancel_after_side_effect_does_not_start_followup(self):
        stop = threading.Event()

        class SideEffectWindows(Windows):
            def __init__(self):
                self.executions = 0

            def act(self, *a):
                self.executions += 1
                stop.set()
                return "accepted"

        windows = SideEffectWindows()
        planner = Planner(action("windows_launch", {"app": "notepad"}), action("done"))
        outcome = make_agent(windows=windows, planner=planner, stop=stop).run("メモ帳を出して")
        self.assertEqual(outcome.status, "interrupted")
        self.assertEqual(windows.executions, 1)
        self.assertEqual(planner.calls, 1)
        self.assertIn("可能性", outcome.summary)

    def test_failed_verification_never_retries_side_effect(self):
        class ProbeFails(Windows):
            def __init__(self): self.calls = 0
            def act(self, *a):
                self.calls += 1
                return "accepted"
            def quick_state(self):
                raise RuntimeError("UIA could not observe")

        windows = ProbeFails()
        outcome = make_agent(windows, Planner(action("windows_launch", {"app":"notepad"}),
                                              action("done"))).run("メモ帳を出して")
        self.assertEqual(windows.calls, 1)
        self.assertEqual(outcome.status, "unverified")

    def test_fast_command_is_verified_from_lightweight_windows_probe(self):
        class WithLaunch(Windows):
            def act(self, *a): return "process started"

        with patch("app.agent.Planner", Planner):
            outcome = make_agent(WithLaunch(), Planner()).run("メモ帳を開いて")
        self.assertEqual(outcome.status, "completed")

    def test_click_without_visible_change_is_not_claimed_complete(self):
        class ClickWindows(Windows):
            def act(self, *a): return "clicked"

        # windows_click needs a recognized visible control from the snapshot.
        class Controlled(ClickWindows):
            def snapshot(self):
                return {"active_title":"メモ帳", "windows":[],
                        "controls":[{"id":"0","name":"確認","type":"Button"}]}

        outcome = make_agent(Controlled(), Planner(
            action("windows_click", {"id":"0"}), action("done"))).run("表示中のボタンを押して")
        self.assertEqual(outcome.status, "unverified")

    def test_window_change_confirms_prior_click_without_extra_probe(self):
        class Controlled(Windows):
            count=0
            def snapshot(self):
                self.count += 1
                return {"active_title":"メモ帳" if self.count == 1 else "別ウィンドウ",
                        "windows":[], "controls":[{"id":"0","name":"次へ","type":"Button"}]}
            def act(self, *a): return "clicked"

        windows=Controlled()
        outcome=make_agent(windows, Planner(
            action("windows_click", {"id":"0"}), action("done"))).run("次へ")
        self.assertEqual(outcome.status, "completed")
        self.assertEqual(windows.count, 2)

    def test_verified_browser_receipt_without_extra_snapshot(self):
        evidence = assess("browser_fill", {"id":"1","text":"hello"}, {"verified":True})
        self.assertEqual(evidence.status, "verified")
        self.assertEqual(assess("browser_fill", {}, {"verified":False}).status, "unverified")

    def test_files_move_postcondition_is_checked(self):
        with tempfile.TemporaryDirectory() as home:
            root = Path(home)
            src = root / "a.pdf"
            src.write_bytes(b"sample")
            downloads = root / "Downloads"
            downloads.mkdir()
            new = downloads / src.name
            src.rename(new)
            with patch("app.win_actions.user_roots", return_value=[downloads]):
                evidence = assess("files_move", {"path": str(src), "folder":"downloads"}, "moved")
            self.assertEqual(evidence.status, "verified")

    def test_bounded_context_does_not_keep_field_values(self):
        session={}
        for n in range(20):
            remember_instruction(session, f"command {n}")
            remember_step(session,"browser_fill", {"id":"id1","text":"SECRET123"},
                          {"browser":{"elements":[{"id":"id1", "label":"検索欄"}]},
                           "windows":{"active_title":"Chrome"}},"verified")
        ctx = task_context(session, "Chrome")
        self.assertEqual(len(ctx["recent_steps"]), 6)
        self.assertEqual(len(session["recent_instructions"]), 4)
        self.assertNotIn("SECRET123", repr(ctx))
        self.assertEqual(ctx["last_target"]["label"], "検索欄")
        self.assertFalse(ctx["last_target"]["possibly_stale"])
        self.assertTrue(task_context(session, "Word")["last_target"]["possibly_stale"])

    def test_ui_distinguishes_confirmed_and_unverified(self):
        self.assertNotEqual(presentation("completed").title, presentation("unverified").title)
        self.assertIn("未確認", presentation("unverified").hint)

    def test_agent_context_exposes_bounded_task_memory(self):
        session={}
        remember_step(session, "windows_launch", {"app":"notepad"},
                      {"windows":{"active_title":"メモ帳"}}, "verified")
        agent=make_agent(session=session)
        ctx=agent._context()
        self.assertEqual(ctx["task_memory"]["last_target"]["label"], "notepad")
        self.assertNotIn("id", ctx["task_memory"]["last_target"])


if __name__ == '__main__':
    unittest.main()