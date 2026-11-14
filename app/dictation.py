"""Explicit, local-only dictation mode independent of the LLM planner.

Never overwrites an editor's existing value. A target is pinned at mode start;
changes of window, tab, document or focused control stop text insertion.
"""
from __future__ import annotations

from dataclasses import dataclass


START_WORDS = frozenset({"文章入力を始めて", "文章入力開始", "音声タイピング開始", "音声入力モードにして", "音声入力を始めて"})
STOP_WORDS = frozenset({"入力終了", "文章入力終了", "文章入力を終えて", "音声タイピング終了", "音声入力を終了"})
SPECIAL_WORDS = {
    "改行": "\n", "改行して": "\n", "新しい行": "\n",
    "空白": " ", "スペース": " ",
    "句点": "。", "読点": "、", "てん": "、",
}
MAX_CHARS = 2000


class DictationError(ValueError):
    """Dictation-specific refusal that is safe to display verbatim."""


@dataclass(frozen=True)
class DictationTarget:
    kind: str  # 'browser' or 'windows'
    window: int
    token: str
    tab_id: int | None = None


def normalized_phrase(text: str) -> str:
    return str(text).strip().rstrip("。！! ").replace("　", " ")


def dictation_intent(text: str, active: bool) -> tuple[str, str] | None:
    """Strict mode commands; only exact, standalone phrases trigger controls."""
    phrase = normalized_phrase(text)
    if phrase in STOP_WORDS and active:
        return "stop", ""
    if phrase in START_WORDS:
        return ("already_on", "") if active else ("start", "")
    if not active:
        return None
    if not phrase:
        return "empty", ""
    payload = SPECIAL_WORDS.get(phrase, str(text).strip())
    if not payload or len(payload) > MAX_CHARS or any(ord(c) < 32 and c != "\n" for c in payload):
        raise DictationError("入力する文章が長すぎるか、制御文字を含んでいます")
    return "insert", payload


class DictationController:
    def __init__(self, windows, bridge):
        self.windows = windows
        self.bridge = bridge

    def start(self) -> DictationTarget:
        hwnd = self.windows.foreground_hwnd()
        if not hwnd:
            raise DictationError("入力先のウィンドウを選択してください")
        if self.windows.is_chrome_foreground():
            if not self.bridge.connected:
                raise DictationError("Chromeの音声入力にはVoicePilot拡張機能の接続が必要です")
            result = self.bridge.call("dictation_probe", {}, timeout=8)
            if not isinstance(result, dict) or not isinstance(result.get("token"), str) or not isinstance(result.get("tab_id"), int):
                raise DictationError("Chromeの文字入力欄を確認できませんでした")
            return DictationTarget("browser", hwnd, result["token"], result["tab_id"])
        # The Windows adapter checks focused UIA control and password status.
        result = self.windows.dictation_probe()
        if not isinstance(result, str) or not result:
            raise DictationError("選択された入力欄を確認できませんでした")
        return DictationTarget("windows", hwnd, result)

    def insert(self, target: DictationTarget, text: str) -> bool:
        if (not isinstance(text, str) or not text or len(text) > MAX_CHARS or
                any(ord(c) < 32 and c != "\n" for c in text)):
            raise DictationError("文章が長すぎるか、入力できない文字を含んでいます")
        # GUI focus restoration, when necessary, is the caller's responsibility.
        if self.windows.foreground_hwnd() != target.window:
            raise DictationError("入力先のウィンドウが変わりました。入力モードを終了して開始し直してください")
        if target.kind == "browser":
            if not self.windows.is_chrome_foreground() or not self.bridge.connected:
                raise DictationError("Chromeの接続または入力先が変わりました")
            result = self.bridge.call("dictation_insert", {
                "token": target.token, "tab_id": target.tab_id, "text": text,
            }, timeout=12)
            if not isinstance(result, dict) or result.get("inserted") is not True:
                raise DictationError("Chromeへの文字入力を確認できませんでした")
            return result.get("verified") is True
        if target.kind == "windows":
            self.windows.dictation_insert(text, target.token)
            return False  # Win32 sends characters; arbitrary editors may transform them.
        raise DictationError("不明な入力先です")