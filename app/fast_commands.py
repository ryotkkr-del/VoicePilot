"""Deterministic, privacy-first single-step Japanese commands.

Do not use fuzzy match for commands that could alter data. Unknown phrasing falls
through to the planner. No network request is made for these commands.
"""
from __future__ import annotations

import re


def direct_action(utterance: str) -> tuple[str, dict] | None:
    text = re.sub(r"[\s、。！!？?]+", "", utterance).lower().strip()
    if not text or len(text) > 120:
        return None
    # Commands are intentionally narrowly anchored to prevent accidental actions.
    launches = {
        "メモ帳": "notepad", "ノートパッド": "notepad", "電卓": "calculator",
        "計算機": "calculator", "エクスプローラー": "explorer",
        "ファイルエクスプローラー": "explorer", "クローム": "chrome",
        "chrome": "chrome", "ペイント": "paint", "paint": "paint",
        "vscode": "vscode", "vsコード": "vscode", "設定": "settings",
    }
    for spoken, app in launches.items():
        if text in {f"{spoken}を開いて", f"{spoken}開いて", f"{spoken}を起動して", f"{spoken}起動して"}:
            return "windows_launch", {"app": app}
    if text in {"戻って", "前のページに戻って", "ブラウザで戻って", "ページを戻して"}:
        return "browser_back", {}
    if text in {"進んで", "次のページへ進んで", "ブラウザで進んで"}:
        return "browser_forward", {}
    if text in {"下にスクロール", "下にスクロールして", "ページを下へ", "下に動かして"}:
        return "smart_scroll", {"direction": "down"}
    if text in {"上にスクロール", "上にスクロールして", "ページを上へ", "上に動かして"}:
        return "smart_scroll", {"direction": "up"}
    if text in {"一番下にスクロール", "一番下までスクロールして"}:
        return "smart_scroll", {"direction": "down", "amount": "large"}
    if text in {"一番上にスクロール", "一番上までスクロールして"}:
        return "smart_scroll", {"direction": "up", "amount": "large"}
    if text in {"ダウンロードフォルダを開いて", "ダウンロードを開いて"}:
        return "files_show_folder", {"folder": "downloads"}
    if text in {"ドキュメントフォルダを開いて", "ドキュメントを開いて"}:
        return "files_show_folder", {"folder": "documents"}
    if text in {"デスクトップフォルダを開いて"}:
        return "files_show_folder", {"folder": "desktop"}
    if text in {"コピーして", "選択したものをコピーして"}:
        return "windows_hotkey", {"keys": "ctrl+c"}
    if text in {"貼り付けて", "ペーストして"}:
        return "windows_hotkey", {"keys": "ctrl+v"}
    # "元に戻して" is ambiguous: Ctrl+Z would undo the user's own edits,
    # not necessarily the previous VoicePilot action. Keep it out of fast path.
    if text in {"アプリ内の編集を元に戻して", "このアプリの編集を取り消して"}:
        return "windows_hotkey", {"keys": "ctrl+z"}
    if text in {"ウィンドウを切り替えて", "アプリを切り替えて"}:
        return "windows_hotkey", {"keys": "alt+tab"}
    # Explicit navigation only; do not interpret arbitrary utterances as searches.
    match = re.fullmatch(r"(?:web|ウェブ|google|グーグル|ブラウザ)で(.{2,80})を検索して", text)
    if match:
        return "browser_search", {"query": match.group(1)}
    return None


def installed_app_action(utterance: str, installed: dict) -> tuple[str, dict] | None:
    """Launch an *exactly named* Start Menu shortcut without contacting an AI.

    This deliberately does not guess fuzzy names: opening a different program
    is worse UX than letting the planner disambiguate. This never executes
    arbitrary user text as a shell command; the executor resolves a .lnk in
    the Windows Start Menu once more before launching.
    """
    normalized = re.sub(r"[\s、。！!？?]+", "", utterance).casefold().strip()
    matched = re.fullmatch(r"(.{2,90}?)(?:を)?(?:開いて|起動して)", normalized)
    if not matched:
        return None
    candidate = matched.group(1)
    # Do not silently run utilities that may change or remove software.
    if re.search(r"uninstall|アンインストール|削除|remove|repair|修復|reset|リセット|"
                 r"ターミナル|コマンドプロンプト|管理者|powershell|cmd(?:\.exe)?|regedit", candidate, re.I):
        return None
    matches = [title for title in installed
               if re.sub(r"\s+", "", title).casefold() == candidate]
    if len(matches) != 1:
        return None
    return "windows_launch", {"app": matches[0]}