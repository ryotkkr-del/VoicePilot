"""Bounded perceive -> plan -> act -> verify loop for desktop/browser control."""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from typing import Callable

from .actions import validate_action
from .compact import compact_for_model
from .planner import Planner, parse_json_text  # backwards-compatible import
from .fast_commands import direct_action, installed_app_action
from .vision import ScreenFrame, capture_screen
from .win_actions import (WindowsController, find_files, open_file, show_folder,
                          move_file, rename_file, start_menu_shortcuts, BLOCKED_APP)
from .verification import Evidence, assess
from .session_context import remember_instruction, remember_step, task_context
from .safety import RISK_WORDS, is_sensitive_field, unfamiliar_destination, approval_summary
from .task_policy import LongTaskLimits, TaskProgress, EFFECTFUL_ACTIONS, requires_stop_for_ambiguity

# Sending these operations twice can submit forms, duplicate typing, or move
# files twice. Each distinct proposed effect is attempted at most once per run.
NON_REPEATABLE = frozenset({
    "browser_click", "browser_fill", "windows_click", "windows_fill",
    "windows_type", "screen_click", "screen_double_click",
    "screen_drag", "files_move", "files_rename", "browser_new_tab",
    "browser_open", "windows_arrange", "windows_launch",
})


@dataclass(frozen=True)
class RunOutcome:
    """Real execution outcome, separate from AI-generated natural-language text."""
    status: str  # completed / unverified / interrupted / incomplete / error
    summary: str
    verified_action: str | None = None  # safe fixed vocabulary for optional TTS


class VoiceAgent:
    def __init__(self, bridge, windows: WindowsController, planner: Planner,
                 log: Callable[[str], None], confirm: Callable[[str], bool],
                 allow_cloud_ui: bool, stop_event, max_steps: int = 10,
                 session: dict | None = None, allow_cloud_image: bool = False, action_gate=None,
                 autonomous: bool = False, limits: LongTaskLimits | None = None):
        self.bridge, self.windows, self.planner = bridge, windows, planner
        self.log, self.confirm = log, confirm
        self.allow_cloud_ui, self.stop_event = allow_cloud_ui, stop_event
        self.autonomous = bool(autonomous)
        self.limits = limits or LongTaskLimits()
        self.max_steps = max(1, min(32 if autonomous else 16, max_steps))
        self.progress = TaskProgress() if autonomous else None
        self.allow_cloud_image = allow_cloud_image
        self.action_gate = action_gate  # optional pause/resume checkpoint
        self._frame: ScreenFrame | None = None
        self.session = session if session is not None else {}
        self._found_paths = set(self.session.get("recent_files", []))
        self._fast_outcome: RunOutcome | None = None

    def _may_act(self) -> bool:
        if self.stop_event.is_set():
            return False
        return self.action_gate is None or self.action_gate.checkpoint(self.stop_event)

    def _finish(self, status: str, summary: str,
                verified_action: str | None = None) -> RunOutcome:
        self.log(summary)
        return RunOutcome(status, summary, verified_action if status == "completed" else None)

    def _context(self):
        context = {"chrome_connected": self.bridge.connected,
                   "recent_files": self.session.get("recent_files", [])[-12:],
                   "recent_instructions": self.session.get("recent_instructions", [])[-3:]}
        if self.bridge.connected and self.windows.is_chrome_foreground():
            try:
                context["browser"] = self.bridge.call("snapshot")
            except Exception as e:
                context["browser_error"] = str(e)[:150]
        try:
            if hasattr(self.windows, "allow_chrome_uia_fallback"):
                self.windows.allow_chrome_uia_fallback = not isinstance(context.get("browser"), dict)
            context["windows"] = self.windows.snapshot()
        except Exception as e:
            context["windows_error"] = str(e)[:150]
        context["task_memory"] = task_context(
            self.session, (context.get("windows") or {}).get("active_title", ""),
            browser=context.get("browser"), windows=context.get("windows"))
        return context

    @staticmethod
    def _get_element(items, key: str, identifier: str):
        return next((x for x in items if x.get(key) == identifier), None)

    @staticmethod
    def _ui_transition_verified(action: str, before: dict, after: dict) -> bool:
        """Conservative: a navigation/window change, not an AI assertion, is evidence.

        The subsequent planning cycle already observed the UI. This method does
        not trigger another screenshot, network call, or UIA tree traversal.
        """
        if action == "browser_click":
            previous, current = before.get("browser") or {}, after.get("browser") or {}
            return bool(previous.get("url") and current.get("url") and
                        previous["url"] != current["url"])
        if action == "windows_click":
            previous, current = before.get("windows") or {}, after.get("windows") or {}
            return bool(previous.get("active_title") and current.get("active_title") and
                        previous["active_title"] != current["active_title"])
        return False

    def _requires_confirmation(self, action: str, args: dict, context: dict, instruction: str = "") -> bool:
        if action in {"browser_click", "browser_fill"}:
            browser = context.get("browser", {})
            elements = browser.get("elements", []) if isinstance(browser, dict) else []
            item = self._get_element(elements, "id", args.get("id"))
            if item is None:
                raise ValueError("画面が変わりました。操作対象が確認できません")
            if action == "browser_click":
                # Labels alone are not sufficient: a harmless-looking 'Next'
                # can submit a form or act inside a permission dialog.
                return bool(item.get("in_form") or item.get("in_dialog") or
                            item.get("type") == "submit" or item.get("external_origin") or
                            RISK_WORDS.search(str(item.get("label", "")) + " " + str(item.get("role", ""))) or
                            not str(item.get("label", "")).strip())
            if (item.get("type") in {"password", "file", "hidden"}
                    or item.get("sensitive") or is_sensitive_field(item.get("label"))):
                raise ValueError("機密情報の入力欄は操作できません")
            # Do not silently overwrite the user's existing draft.
            return bool(item.get("has_value"))
        if action in {"windows_click", "windows_fill"}:
            win = context.get("windows", {})
            controls = win.get("controls", []) if isinstance(win, dict) else []
            item = self._get_element(controls, "id", args.get("id"))
            if item is None:
                raise ValueError("画面が変わりました。Windows操作対象が確認できません")
            if action == "windows_fill":
                if (item.get("type") != "Edit" or item.get("sensitive") or
                        is_sensitive_field(item.get("name"))):
                    raise ValueError("この入力欄は安全に編集できません")
                # Windows UIA does not reliably reveal whether existing text
                # will be replaced, so ask before a full-field replacement.
                return True
            return not str(item.get("name", "")).strip() or bool(RISK_WORDS.search(item.get("name", "")))
        if action in {"screen_click", "screen_double_click", "screen_drag"}:
            if self._frame is None:
                raise ValueError("画像を観測してからクリックしてください")
            if action == "screen_drag":
                return True  # Drag can move files or change app state.
            # Image-derived coordinates are not semantically bound to a DOM/UIA
            # element. Require approval even when the label appears harmless.
            # Don't pay the cost of another UIA traversal simply to ask.
            return True
        if action in {"files_move", "files_rename", "windows_type"}:
            return True
        if action == "windows_hotkey":
            return args.get("keys") in {"enter", "ctrl+s", "ctrl+v", "ctrl+z"}
        if action in {"browser_open", "browser_new_tab"}:
            return unfamiliar_destination(args["url"], instruction, context)
        # The search phrase can include page text supplied by an attacker;
        # require explicit approval if it was not spoken by the user.
        if action == "browser_search":
            return args["query"].strip().casefold() not in instruction.casefold()
        return False

    def _execute(self, action: str, args: dict):
        # A cancellation can arrive between the planner checkpoint and the
        # actual dispatch. Reject it at the final shared execution boundary.
        # This cannot roll back actions already in progress.
        if self.stop_event.is_set():
            raise InterruptedError("中止済みの操作は実行しません")
        if action in {"screen_click", "screen_double_click", "screen_drag"}:
            if self._frame is None:
                raise ValueError("画像がありません")
            if time.monotonic() - self._frame.captured_at > 12:
                raise RuntimeError("画像が古くなりました。もう一度観測してください")
            if self.windows.foreground_hwnd() != self._frame.active_hwnd:
                raise RuntimeError("画面が切り替わったためクリックを中止しました")
            x, y = self._frame.pixel(args["x"], args["y"])
            if action == "screen_drag":
                to_x, to_y = self._frame.pixel(args["to_x"], args["to_y"])
                self._frame = None
                return self.windows.drag_point(x, y, to_x, to_y)
            self._frame = None  # one visual action per fresh observation
            return self.windows.click_point(x, y, double=action == "screen_double_click")
        if action.startswith("browser_"):
            # Active-page edits must not silently run in a background Chrome tab
            # if the user switched to another app while the LLM was reasoning.
            # Explicit search/open operations may still activate Chrome.
            if action in {"browser_click", "browser_fill", "browser_scroll",
                          "browser_back", "browser_forward", "browser_switch_tab"}:
                if not self.windows.is_chrome_foreground():
                    raise RuntimeError("Chromeが前面でなくなったため、ブラウザ操作を中止しました")
            return self.bridge.call(action, args)
        if action.startswith("windows_"):
            return self.windows.act(action, args)
        if action == "files_find":
            matches = find_files(args["query"], cancelled=self.stop_event.is_set)
            # Keep order stable so "the file from a moment ago" is predictable.
            previous = self.session.get("recent_files", [])
            ordered = [p for p in previous if p not in matches] + matches
            self.session["recent_files"] = ordered[-30:]
            self._found_paths = set(self.session["recent_files"])
            return {"matches": matches}
        if action == "files_open":
            if args["path"] not in self._found_paths:
                raise ValueError("検索結果にあるファイルだけを開けます。先に検索してください")
            return open_file(args["path"])
        if action == "files_show_folder":
            return show_folder(args["folder"])
        if action in {"files_move", "files_rename"}:
            if args["path"] not in self._found_paths:
                raise ValueError("先にファイル検索して、結果にあるファイルだけを指定してください")
            if action == "files_move":
                result = move_file(args["path"], args["folder"])
            else:
                result = rename_file(args["path"], args["new_name"])
            self._found_paths.discard(args["path"])
            self.session["recent_files"] = [p for p in self.session.get("recent_files", []) if p != args["path"]]
            return result
        raise ValueError("未対応の操作です")

    def _fast(self, instruction: str) -> bool:
        quick = direct_action(instruction)
        if quick is None:
            # A clearly named installed application needs no LLM reasoning.
            # Reading the Start Menu is lazy and cached; unknown utterances
            # still go through the full planner without launching anything.
            if instruction.strip().endswith(("開いて", "起動して")):
                quick = installed_app_action(instruction, start_menu_shortcuts())
                if quick and BLOCKED_APP.search(quick[1]["app"]):
                    quick = None
            if quick is None:
                return False
        action, args = quick
        if action == "smart_scroll":
            action = "browser_scroll" if self.bridge.connected and self.windows.is_chrome_foreground() else "windows_scroll"
            direction = args["direction"]
            if args.get("amount") == "large":
                direction = "bottom" if direction == "down" else "top"
            args = {"direction": direction}
        if action.startswith("browser_") and not self.bridge.connected:
            self._fast_outcome = self._finish("incomplete", "Chrome拡張機能が未接続です。接続を確認してください。")
            return True
        if action in {"browser_back", "browser_forward"} and not self.windows.is_chrome_foreground():
            return False  # Ambiguous: route through the planner instead.
        validate_action({"action": action, "args": args})
        if action == "windows_hotkey" and args["keys"] in {"ctrl+v", "ctrl+z"}:
            # Keep the deterministic fast path aligned with planner approvals.
            # Undo can discard the user's own edits, not just VoicePilot's last
            # action. Never treat it as an automatically safe navigation key.
            undo = args["keys"] == "ctrl+z"
            prompt = ("現在のアプリの編集を元に戻しますか？"
                      if undo else "現在のウィンドウにクリップボードの内容を貼り付けますか？")
            if not self.confirm(prompt):
                self._fast_outcome = self._finish(
                    "interrupted", "取り消し操作をキャンセルしました" if undo else "貼り付けをキャンセルしました")
                return True
        if not self._may_act():
            self._fast_outcome = self._finish("interrupted", "操作を中止しました")
            return True
        self.log("高速操作（AI通信なし）")
        result = self._execute(action, args)
        if self.stop_event.is_set():
            self._fast_outcome = self._finish("interrupted", "中止しました。直前の操作は実行された可能性があります")
            return True
        try:
            evidence = assess(action, args, result, self.windows)
        except Exception:
            # The operation may already have taken effect. Never repeat it
            # merely because a postcondition probe failed.
            evidence = Evidence("unverified", "操作を送りましたが確認処理に失敗しました")
        self.log(evidence.reason)
        remember_step(self.session, action, args, {}, evidence.status)
        status = "completed" if evidence.status == "verified" else "unverified"
        self._fast_outcome = self._finish(status, evidence.reason,
                                         verified_action=action if status == "completed" else None)
        return True

    def _fast_reference(self, instruction: str) -> bool:
        """Resolve an *explicit* follow-up click from a fresh live snapshot.

        Do not infer that "これ" denotes the last clicked object: it might refer
        to the mouse cursor or a different region. Only narrow anaphoric forms
        are eligible. A page change or duplicate candidate never auto-clicks.
        """
        normalized = re.sub(r"[\s、。！!？?]+", "", instruction)
        if not re.fullmatch(r"(?:それ|そのボタン|さっきのボタン)を?(?:押して|クリックして)", normalized):
            return False
        if not self.session.get("last_reference"):
            self._fast_outcome = self._finish("incomplete", "直前のボタンが特定できません。もう少し詳しく教えてください")
            return True
        context = self._context()
        memory = context.get("task_memory") or {}
        referent = memory.get("resolved_reference")
        if not isinstance(referent, dict):
            self._fast_outcome = self._finish(
                "incomplete", "前の操作対象を現在の画面で一意に確認できません。対象をもう少し詳しく教えてください。")
            return True
        source = referent.get("source")
        if source not in {"browser", "windows"}:
            self._fast_outcome = self._finish("incomplete", "前の操作対象を特定できませんでした")
            return True
        action = source + "_click"
        args = {"id": str(referent["id"])}
        try:
            validate_action({"action": action, "args": args})
            if self._requires_confirmation(action, args, context, instruction):
                if not self.confirm("次の操作を許可しますか？\n\n" + approval_summary(action, args, context)):
                    self._fast_outcome = self._finish("interrupted", "承認されなかったため中止しました")
                    return True
            if not self._may_act():
                self._fast_outcome = self._finish("interrupted", "操作を中止しました")
                return True
            result = self._execute(action, args)
            if self.stop_event.is_set():
                self._fast_outcome = self._finish("interrupted", "中止しました。直前の操作は実行された可能性があります")
                return True
            try:
                evidence = assess(action, args, result, self.windows)
            except Exception:
                evidence = Evidence("unverified", "操作を送りましたが結果を確認できませんでした")
            remember_step(self.session, action, args, context, evidence.status)
            status = "completed" if evidence.status == "verified" else "unverified"
            self._fast_outcome = self._finish(status, evidence.reason,
                                             verified_action=action if status == "completed" else None)
        except Exception as exc:
            self._fast_outcome = self._finish("incomplete", "操作対象が変わったため中止しました: " + str(exc)[:100])
        return True

    def _plan_checked(self, instruction: str, context: dict,
                      history: list[dict], image: bytes | None):
        """Retry a malformed *proposal* once, never an executed action.

        A slow local model occasionally emits invalid JSON or an unknown
        action. A bounded retry can recover without any duplicate clicks.
        Network/timeouts are not retried, to avoid hidden cost and latency.
        """
        proposed_history = history
        for attempt in range(2):
            raw = self.planner.next_action(instruction, context, proposed_history, image)
            try:
                return validate_action(raw)
            except (ValueError, TypeError) as exc:
                if attempt == 1 or self.stop_event.is_set():
                    raise
                self.log("操作候補の形式が不正なため、一度だけ再確認しています")
                proposed_history = history[-5:] + [{
                    "action": "invalid_proposal",
                    "error": str(exc)[:100],
                    "requirement": "許可された操作名と必要な文字列引数を含むJSONだけ返す",
                }]
        raise RuntimeError("有効な操作候補を取得できませんでした")

    def run(self, instruction: str):
        if self.stop_event.is_set():
            return self._finish("interrupted", "操作を中止しました")
        remember_instruction(self.session, instruction)
        if self._fast_reference(instruction):
            return self._fast_outcome
        if self._fast(instruction):
            return self._fast_outcome
        if self.planner.provider == "gemini" and not self.allow_cloud_ui:
            return self._finish("incomplete", "Geminiへの画面・指示送信が許可されていません。設定で明示的に許可するかOllamaを選んでください。")
        history: list[dict] = []
        completed_steps = 0
        vision_count = 0
        pending_image: bytes | None = None
        signatures: dict[str, int] = {}
        attempted_effects: set[str] = set()
        start = time.monotonic()
        pending_verification: tuple[int, dict] | None = None
        effect_count = 0
        confirmation_count = 0
        last_checkpoint = 0
        for index in range(self.max_steps):
            if not self._may_act():
                return self._finish("interrupted", "操作を中止しました")
            pause_seconds = self.action_gate.paused_seconds() if self.action_gate else 0.0
            max_seconds = self.limits.max_seconds if self.autonomous else 100
            if time.monotonic() - start - pause_seconds > max_seconds:
                return self._finish("incomplete", "処理時間の上限に達したため停止しました")
            pause_generation = (self.action_gate.pause_generation if self.action_gate else 0)
            context = self._context()
            if pending_verification is not None:
                step_index, before = pending_verification
                prior_action = history[step_index]["action"]
                if self._ui_transition_verified(prior_action, before, context):
                    history[step_index]["verification"] = "verified"
                    self.log("次の画面状態から操作結果を確認しました")
                pending_verification = None
            image_for_call, pending_image = pending_image, None
            try:
                action, args, message = self._plan_checked(
                    instruction, compact_for_model(context, instruction), history, image_for_call
                )
            except (ValueError, TypeError):
                if self.stop_event.is_set():
                    return self._finish("interrupted", "操作を中止しました")
                return self._finish("incomplete", "AIの操作指示を安全に解釈できなかったため停止しました")
            if not self._may_act():
                return self._finish("interrupted", "操作を中止しました")
            if self.action_gate and self.action_gate.pause_generation != pause_generation:
                self.log("再開後に画面を再確認して操作を選び直します")
                self._frame = None
                continue
            if action == "done":
                if completed_steps == 0:
                    # Preserve a useful clarification from the planner.  It is only
                    # displayed as text, never used as an executable instruction.
                    reason = message.strip()[:180]
                    if not reason or any(word in reason for word in
                                         ("完了しました", "実行しました", "成功しました")):
                        reason = "指示を確認してください"
                    return self._finish("incomplete", "操作は実行されませんでした。" + reason)
                if any("error" in step for step in history):
                    return self._finish("incomplete", "一部の操作でエラーがありました。操作ログを確認してください")
                if any(step.get("verification") != "verified" for step in history):
                    return self._finish("unverified", "操作を実行しましたが、一部の結果は確認できていません")
                only_action = history[0]["action"] if len(history) == 1 else None
                return self._finish("completed", "操作結果を確認しました",
                                    verified_action=only_action)
            if action == "screen_observe":
                if vision_count >= 2:
                    return self._finish("incomplete", "画像観測の上限に達しました。操作対象を特定できませんでした")
                if self.planner.provider == "gemini" and not self.allow_cloud_image:
                    return self._finish("incomplete", "Geminiにスクリーンショットを送る許可がありません。画像共有を有効にするかOllamaを使ってください")
                # A terminal may disguise itself as an ordinary window.
                # Check the actual foreground process BEFORE screen capture.
                guard = getattr(self.windows, "assert_safe_observation", None)
                try:
                    if guard is not None:
                        guard()
                    before_hwnd = self.windows.foreground_hwnd()
                    frame = capture_screen(before_hwnd)
                    if self.windows.foreground_hwnd() != before_hwnd:
                        return self._finish("incomplete", "画面が切り替わったため画像を破棄しました")
                    if guard is not None:
                        guard()  # A new protected app may have gained focus.
                except PermissionError:
                    return self._finish("incomplete", "保護対象の画面は取得できません")
                self._frame = frame
                pending_image = self._frame.jpeg
                vision_count += 1
                self.log("画面の画像を観測して操作対象を確認しています")
                continue
            if action in {"screen_click", "screen_double_click", "screen_drag"} and image_for_call is None:
                return self._finish("incomplete", "画面座標の操作には直前の画像観測が必要です")
            key = json.dumps({"action": action, "args": args}, ensure_ascii=False, sort_keys=True)
            non_repeatable = action in NON_REPEATABLE or (
                action == "windows_hotkey" and args.get("keys") in {"enter", "ctrl+v", "ctrl+s", "ctrl+z"})
            if non_repeatable and key in attempted_effects:
                return self._finish("incomplete", "同じ操作の再実行を防ぐため停止しました。画面を確認してから新しい指示を出してください")
            signatures[key] = signatures.get(key, 0) + 1
            if signatures[key] >= 3:
                return self._finish("incomplete", "同じ操作の繰り返しを検出したため停止しました")
            self.log(f"{index + 1}/{self.max_steps}  {action}" if self.autonomous
                     else f"{index + 1}/{self.max_steps}  {message or action}")
            try:
                if self.autonomous and action in EFFECTFUL_ACTIONS:
                    if effect_count >= self.limits.max_effects:
                        return self._finish("incomplete", "自律作業の操作数上限です。状態を確認して再指示してください")
                    if effect_count - last_checkpoint >= self.limits.checkpoint_every:
                        confirmation_count += 1
                        if confirmation_count > self.limits.max_confirmations or not self.confirm(
                                "自律作業は途中まで進みました。現在の画面を確認して、続けてもよいですか？"):
                            return self._finish("interrupted", "自律作業を確認地点で停止しました")
                        last_checkpoint = effect_count
                        # Approval may steal focus or change the UI. Re-observe.
                        self._frame = None
                        continue
                requires_confirmation = self._requires_confirmation(action, args, context, instruction)
                if self.autonomous and action in {"windows_launch", "files_open"}:
                    # Long tasks must not silently open an app or a downloaded file,
                    # even if the planner inferred its name from untrusted UI text.
                    requires_confirmation = True
                if requires_confirmation:
                    if self.autonomous:
                        confirmation_count += 1
                        if confirmation_count > self.limits.max_confirmations:
                            return self._finish("incomplete", "承認確認の回数上限で停止しました")
                    # Model-generated 'message' is untrusted; the dialog shows
                    # only action parameters and the observed target.
                    detail = approval_summary(action, args, context)
                    if not self.confirm(f"次の操作を許可しますか？\n\n{detail}"):
                        return self._finish("interrupted", "承認されなかったため中止しました")
                if not self._may_act():
                    return self._finish("interrupted", "操作を中止しました")
                if self.action_gate and self.action_gate.pause_generation != pause_generation:
                    self.log("停止前の操作候補は使わず、現在の画面を調べ直します")
                    self._frame = None
                    continue
                if non_repeatable:
                    attempted_effects.add(key)  # count ambiguous failures as attempts
                if self.autonomous and action in EFFECTFUL_ACTIONS:
                    effect_count += 1
                result = self._execute(action, args)
                try:
                    evidence = assess(action, args, result, self.windows)
                except Exception:
                    # Verification errors do not justify redoing a side effect.
                    evidence = Evidence("unverified", "操作結果の確認に失敗しました")
                if (self.autonomous and evidence.status != "verified" and
                        action in {"browser_click", "windows_click"} and not self.stop_event.is_set()):
                    # A click can finish between an immediate acknowledgement
                    # and the next fresh UI snapshot. Observe once more, NEVER
                    # replay the click because its effect was ambiguous.
                    try:
                        after = self._context()
                        if self._ui_transition_verified(action, context, after):
                            evidence = Evidence("verified", "画面遷移を改めて確認しました")
                    except Exception:
                        pass
                history.append({"action": action, "args": args, "result": str(result)[:800],
                                "verification": evidence.status})
                remember_step(self.session, action, args, context, evidence.status)
                completed_steps += 1
                if self.progress is not None:
                    self.progress.record(action, evidence.status == "verified")
                    # Do not keep private text, paths, DOM ids, or credentials.
                    self.session["long_task_progress"] = {
                        "attempted": self.progress.total_attempts,
                        "verified": self.progress.verified,
                        "last_action": action,
                        "status": self.progress.last_status,
                    }
                if self.autonomous and requires_stop_for_ambiguity(action, evidence.status):
                    return self._finish("unverified", "操作結果が不明なため自律作業を停止しました。再実行せず画面を確認してください")
                if evidence.status != "verified" and action in {"browser_click", "windows_click"}:
                    pending_verification = (len(history) - 1, context)
                if self.stop_event.is_set():
                    return self._finish("interrupted", "中止しました。直前の操作は実行された可能性があります")
            except Exception as e:
                if self.stop_event.is_set():
                    return self._finish("interrupted", "操作を中止しました。直前の操作は実行された可能性があります")
                self.log(f"操作エラー: {type(e).__name__}" if self.autonomous else f"操作エラー: {e}")
                history.append({"action": action, "args": args, "error": str(e)[:200]})
                if self.autonomous:
                    return self._finish("incomplete", "操作の失敗が確認されたため、自律作業を安全停止しました")
                if sum("error" in h for h in history) >= 3:
                    return self._finish("error", "連続エラーのため停止しました")
        return self._finish("incomplete", "操作数の上限に達したため停止しました。続きを指示してください。")