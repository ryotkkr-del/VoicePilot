"""Fresh-only references for spoken demonstratives and safe context compression."""
from __future__ import annotations

import threading
import unittest
from unittest.mock import patch

from app.agent import VoiceAgent
from app.compact import compact_for_model
from app.session_context import remember_step, task_context


def browser(*, doc="7:abc", items=None, title="Research"):
    return {"document_ref": doc, "title": title, "url": "https://example.test/article",
            "elements": items if items is not None else [
                {"id": "new-id", "label": "詳細", "tag": "button", "role": "",
                 "type": "button", "in_view": True, "in_form": False,
                 "in_dialog": False, "link_key": "", "external_origin": ""}]}


def windows(*, handle="42", items=None, title="Chrome"):
    return {"window_ref": handle, "active_title": title, "controls": items or []}


def remember_browser(session):
    observed = {"browser": browser(items=[{**browser()["elements"][0], "id": "old-id"}]),
                "windows": windows()}
    remember_step(session, "browser_click", {"id": "old-id"}, observed, "verified")


class DeicticReferenceTests(unittest.TestCase):
    def test_old_id_is_never_reused(self):
        session = {}
        remember_browser(session)
        state = task_context(session, "Chrome", browser=browser(), windows=windows())
        self.assertEqual(state["resolved_reference"]["id"], "new-id")
        self.assertNotIn("old-id", str(state))
        self.assertEqual(state["reference_status"], "matched")
        self.assertFalse(state["last_target"]["possibly_stale"])

    def test_cross_tab_and_spa_navigation_never_resolve(self):
        session = {}
        remember_browser(session)
        for new_doc in ("9:abc", "7:def", ""):
            with self.subTest(new_doc=new_doc):
                state = task_context(session, "Chrome", browser=browser(doc=new_doc), windows=windows())
                self.assertNotIn("resolved_reference", state)
                self.assertTrue(state["last_target"]["possibly_stale"])

    def test_two_identical_labels_do_not_choose_first(self):
        session = {}
        remember_browser(session)
        twins = browser()["elements"] + [{**browser()["elements"][0], "id":"second-id"}]
        state = task_context(session, "Chrome", browser=browser(items=twins), windows=windows())
        self.assertEqual(state["reference_status"], "ambiguous")
        self.assertNotIn("resolved_reference", state)

    def test_changed_semantics_or_hidden_target_is_rejected(self):
        session = {}
        remember_browser(session)
        for change in ({"in_form": True}, {"label": "削除"}, {"in_dialog": True},
                       {"link_key": "new"}, {"in_view": False}):
            with self.subTest(change=change):
                item = {**browser()["elements"][0], **change}
                state = task_context(session, "Chrome", browser=browser(items=[item]), windows=windows())
                self.assertNotIn("resolved_reference", state)
                self.assertEqual(state["reference_status"], "not_found")

    def test_unverified_operation_preserves_target_but_not_success_claim(self):
        session = {}
        remember_step(session, "browser_click", {"id":"new-id"},
                      {"browser":browser(), "windows":windows()}, "unverified")
        context = task_context(session, "Chrome", browser=browser(), windows=windows())
        self.assertEqual(context["resolved_reference"]["id"], "new-id")
        self.assertEqual(context["last_target"]["confirmation"], "unverified")

    def test_target_reference_expires(self):
        session = {}
        remember_browser(session)
        with patch("app.session_context.time.monotonic", return_value=session["last_reference"]["at"]+601):
            state = task_context(session,"Chrome",browser=browser(),windows=windows())
        self.assertEqual(state["reference_status"], "expired")
        self.assertNotIn("resolved_reference", state)

    def test_windows_reference_uses_current_window_and_fresh_control_id(self):
        session = {}
        old = {"id":"0", "name":"詳細", "type":"Button"}
        seen = {"windows": windows(items=[old],title="メモ帳")}
        remember_step(session, "windows_click", {"id":"0"}, seen, "verified")
        new = {"id":"7", "name":"詳細", "type":"Button"}
        matched = task_context(session,"メモ帳",windows=windows(items=[new],title="メモ帳"))
        self.assertEqual(matched["resolved_reference"]["id"],"7")
        changed = task_context(session,"メモ帳",windows=windows(handle="43",items=[new],title="メモ帳"))
        self.assertNotIn("resolved_reference", changed)

    def test_no_old_ui_id_or_user_field_value_in_memory(self):
        session = {}
        seen = {"browser": browser(items=[{**browser()["elements"][0],"id":"secret-handle"}]),
                "windows": windows()}
        remember_step(session,"browser_fill", {"id":"secret-handle","text":"PRIVATE USER STRING"},seen,"verified")
        self.assertNotIn("secret-handle",repr(session))
        self.assertNotIn("PRIVATE USER STRING",repr(session))

    def test_reference_and_near_end_control_survive_compaction(self):
        many = [dict(browser()["elements"][0], id=f"el{i}", label=f"option{i}") for i in range(140)]
        many[135] = {**browser()["elements"][0], "id":"fresh-reference"}
        payload = {"browser":browser(items=many), "windows": windows(),
                   "task_memory":{"resolved_reference":{"source":"browser","id":"fresh-reference","label":"詳細"}}}
        reduced = compact_for_model(payload,"それを押して")
        self.assertIn("fresh-reference", [x["id"] for x in reduced["browser"]["elements"]])
        self.assertLessEqual(len(reduced["browser"]["elements"]),70)
        self.assertNotIn("document_ref",reduced["browser"])
        self.assertIn("document_ref",payload["browser"])

    def test_integration_context_resolves_fresh_target(self):
        session = {}
        remember_browser(session)
        class Bridge:
            connected=True
            def call(self, operation):
                self.operation=operation
                return browser()
        class Win:
            allow_chrome_uia_fallback=True
            def is_chrome_foreground(self): return True
            def snapshot(self): return windows()
        agent=VoiceAgent(Bridge(),Win(),object(),lambda s: None,lambda s:True,False,threading.Event(),session=session)
        state=agent._context()
        self.assertEqual(state["task_memory"]["resolved_reference"]["id"],"new-id")

class FastReferenceTests(unittest.TestCase):
    """Common deictic clicks should not need a second model inference."""

    def make_agent(self, *, items=None, current_doc="7:abc", approve=True, stop=False):
        class Bridge:
            connected = True
            def __init__(self): self.calls = []
            def call(self, action, args=None):
                self.calls.append((action, args))
                if action == "snapshot":
                    return browser(doc=current_doc,items=items)
                if action == "browser_click":
                    return {"clicked": "詳細", "verified": False}
                raise AssertionError(action)
        class Win:
            allow_chrome_uia_fallback=True
            def is_chrome_foreground(self): return True
            def snapshot(self): return windows()
        class PlannerNever:
            provider = "ollama"
            def next_action(self, *_): raise AssertionError("LLM called")
        session={}
        remember_browser(session)
        confirmation=[]
        bridge=Bridge()
        abort=threading.Event()
        if stop: abort.set()
        agent=VoiceAgent(bridge,Win(),PlannerNever(),lambda s:None,
                         lambda msg: confirmation.append(msg) or approve,
                         False,abort,session=session)
        return agent,bridge,confirmation

    def test_unique_recent_target_click_without_llm(self):
        agent,bridge,confirmation=self.make_agent()
        outcome=agent.run("それを押して")
        self.assertEqual(outcome.status,"unverified")  # click acknowledgement != success proof
        self.assertEqual([call[0] for call in bridge.calls],["snapshot","browser_click"])
        self.assertEqual(bridge.calls[-1][1],{"id":"new-id"})
        self.assertFalse(confirmation)

    def test_ambiguous_target_refuses_click(self):
        item=browser()["elements"][0]
        agent,bridge,_=self.make_agent(items=[item,{**item,"id":"second"}])
        outcome=agent.run("それをクリックして")
        self.assertEqual(outcome.status,"incomplete")
        self.assertEqual([call[0] for call in bridge.calls],["snapshot"])

    def test_page_change_refuses_click(self):
        agent,bridge,_=self.make_agent(current_doc="7:changed")
        outcome=agent.run("そのボタンを押して")
        self.assertEqual(outcome.status,"incomplete")
        self.assertEqual([call[0] for call in bridge.calls],["snapshot"])

    def test_no_memory_avoids_expensive_screen_scan(self):
        agent,bridge,_=self.make_agent()
        agent.session.clear()
        outcome=agent.run("それを押して")
        self.assertEqual(outcome.status,"incomplete")
        self.assertEqual(bridge.calls,[])

    def test_risky_button_requires_approval(self):
        item={**browser()["elements"][0],"label":"送信", "in_form":True}
        agent,bridge,confirmation=self.make_agent(items=[item],approve=False)
        # Store the same observed button as the last one, but no previous ID reuse.
        agent.session.clear()
        remember_step(agent.session, "browser_click", {"id":"new-id"},
                      {"browser":browser(items=[item]),"windows":windows()},"unverified")
        outcome=agent.run("さっきのボタンを押して")
        self.assertEqual(outcome.status,"interrupted")
        self.assertEqual([call[0] for call in bridge.calls],["snapshot"])
        self.assertEqual(len(confirmation),1)
        self.assertIn("送信",confirmation[0])

class WindowsReferenceFastTests(unittest.TestCase):
    def test_windows_recent_control_click_is_still_supported(self):
        class Bridge:
            connected=False
        class Win:
            def __init__(self): self.executed=[]
            def is_chrome_foreground(self): return False
            def snapshot(self):
                return windows(handle="42", title="メモ帳",
                               items=[{"id":"fresh","name":"詳細", "type":"Button"}])
            def act(self, action, args):
                self.executed.append((action,args))
                return "clicked"
        class PlannerNever:
            provider="ollama"
            def next_action(self,*_): raise AssertionError('called LLM')
        session={}
        remember_step(session,"windows_click",{"id":"old"},
                      {"windows":windows(handle="42",title="メモ帳",
                                         items=[{"id":"old","name":"詳細","type":"Button"}])},
                      "unverified")
        win=Win()
        agent=VoiceAgent(Bridge(),win,PlannerNever(),lambda s:None,lambda s:True,
                         False,threading.Event(),session=session)
        result=agent.run("そのボタンを押して")
        self.assertEqual(win.executed,[("windows_click",{"id":"fresh"})])
        self.assertEqual(result.status,"unverified")


if __name__ == "__main__":
    unittest.main()