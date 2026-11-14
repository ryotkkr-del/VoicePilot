"""Cheap, explicit evidence about whether a requested action took effect.

Never equate a successful API call with a verified UI outcome. Only actions
with a meaningful, inexpensive postcondition are considered verified.
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import time


@dataclass(frozen=True)
class Evidence:
    status: str  # verified / unverified / failed
    reason: str


def assess(action: str, args: dict, result, windows=None) -> Evidence:
    if isinstance(result, dict):
        if result.get("verified") is True:
            return Evidence("verified", "操作結果を確認しました")
        if result.get("verified") is False:
            return Evidence("unverified", "操作後の変化を確認できませんでした")
    if action == "files_find" and isinstance(result, dict) and isinstance(result.get("matches"), list):
        if not result["matches"]:
            return Evidence("unverified", "一致するファイルは見つかりませんでした")
        return Evidence("verified", "ファイル検索結果を確認しました")
    if action in {"files_move", "files_rename"}:
        # Filesystem state is a stronger signal than the function's message.
        source = Path(args["path"])
        if action == "files_rename":
            dest = source.with_name(args["new_name"])
        else:
            from .win_actions import user_roots
            names = {"desktop": {"desktop", "デスクトップ"},
                     "documents": {"documents", "ドキュメント"},
                     "downloads": {"downloads", "ダウンロード"}}[args["folder"]]
            target = next((p for p in user_roots() if p.name.casefold() in names), None)
            dest = target / source.name if target else None
        if dest and dest.is_file() and not source.exists():
            return Evidence("verified", "ファイルの移動・改名を確認しました")
        return Evidence("unverified", "変更後のファイルを確認できませんでした")
    if action in {"windows_focus", "windows_launch"} and windows is not None:
        probe = getattr(windows, "quick_state", None)
        if callable(probe):
            tokens = {
                "notepad": ("メモ帳", "notepad"),
                "calculator": ("電卓", "calculator"),
                "explorer": ("エクスプローラー", "file explorer"),
                "chrome": ("chrome",),
                "vscode": ("visual studio code", "vs code"),
                "paint": ("ペイント", "paint"),
                "settings": ("設定", "settings"),
            }.get(args.get("app", "").lower(), (args.get("app", ""),))
            # Launch can create its window slightly after process creation.
            # At most 600 ms of cheap Win32 polling, with no UIA traversal.
            attempts = 5 if action == "windows_launch" else 1
            for attempt in range(attempts):
                state = probe()
                titles = [str(x.get("title", "")) for x in state.get("windows", [])]
                foreground = str(state.get("active_title", ""))
                if action == "windows_focus":
                    if args["title"].casefold() in foreground.casefold():
                        return Evidence("verified", "対象ウィンドウへの切替を確認しました")
                elif any(token and token.casefold() in title.casefold()
                         for token in tokens for title in titles):
                    return Evidence("verified", "起動したウィンドウを確認しました")
                if attempt + 1 < attempts:
                    time.sleep(0.15)
    return Evidence("unverified", "操作を送信しましたが、結果は未確認です")