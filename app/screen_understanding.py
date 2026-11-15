"""Read-only, opt-in screen explanations. Never sends an action to the executor.

Browser and UIA metadata are untrusted *observations*, not commands. No page
text, pointer label, or screenshot is persisted. Screenshot transfer to Gemini
needs its separate explicit image-consent switch; Ollama stays local.
"""
from __future__ import annotations

import re
from collections.abc import Callable

from .vision import capture_screen

# Require an explicit question about the CURRENT screen, not an action request.
_SCREEN = re.compile(r"(?:画面|ウィンドウ|ページ|スクリーン|表示中|このエラー|今のエラー|この表|この文章|ここ|これって|これは|これ何|カーソル|ポインタ|画像|写真|グラフ|アイコン|(?:この|その)(?:ボタン|項目|図))")
_ASK = re.compile(r"(?:何|なに|どう|説明|教えて|解説|読んで|読み取|要約|解析|まとめて|書いてある|意味|見て|確認して)")
_ACTION = re.compile(r"(?:クリック|押して|開いて|起動|入力して|書き込|貼り付|送信|削除|移動|閉じて|変更して|保存して|実行して|選択して)")
_POINT = re.compile(r"(?:ここ|これ|それ|カーソル|ポインタ)")


def screen_question(text: str) -> bool:
    """Precise routing: never hijack normal UI commands."""
    value = text.strip()
    return bool(2 <= len(value) <= 200 and _SCREEN.search(value) and
                _ASK.search(value) and not _ACTION.search(value))


def wants_pointer(text: str) -> bool:
    return bool(_POINT.search(text))


def _limited_string(value: object, limit: int = 130) -> str:
    return str(value or "").replace("\x00", "").strip()[:limit]


def screen_context(context: dict, excerpt: dict | None, pointer: dict | None,
                   *, cloud: bool = False) -> dict:
    """Allowlist on-screen observations; never forward unrelated windows to cloud."""
    output: dict = {}
    browser = context.get("browser")
    if isinstance(browser, dict):
        output["browser"] = {
            "title": _limited_string(browser.get("title")),
            "url": _limited_string(browser.get("url"), 170),
            "headings": [_limited_string(s, 110) for s in (browser.get("headings") or [])[:12]],
            "controls": [{"kind": _limited_string(e.get("role") or e.get("tag"), 24),
                          "label": _limited_string(e.get("label"), 90)}
                         for e in (browser.get("elements") or [])[:35]
                         if isinstance(e, dict) and not e.get("sensitive")],
        }
    windows = context.get("windows")
    if isinstance(windows, dict):
        output["windows"] = {
            "active_title": _limited_string(windows.get("active_title")),
            "controls": [{"type": _limited_string(e.get("type"), 32),
                          "name": _limited_string(e.get("name"), 80)}
                         for e in (windows.get("controls") or [])[:35]
                         if isinstance(e, dict) and e.get("type") != "Edit"],
        }
        if not cloud:
            output["windows"]["open_windows"] = [
                _limited_string(w.get("title"), 100)
                for w in (windows.get("windows") or [])[:12]
                if isinstance(w, dict)
            ]
    if isinstance(excerpt, dict):
        output["visible_web_text"] = [_limited_string(s, 340)
                                      for s in (excerpt.get("blocks") or [])[:16]]
    if isinstance(pointer, dict) and pointer:
        output["cursor_element"] = {
            "name": _limited_string(pointer.get("name"), 85),
            "type": _limited_string(pointer.get("type"), 40),
        }
    return output


class ScreenAssistant:
    def __init__(self, *, bridge, windows, planner, context_provider: Callable,
                 stop_event, allow_cloud_ui: bool, allow_cloud_image: bool):
        self.bridge = bridge
        self.windows = windows
        self.planner = planner
        self.context_provider = context_provider
        self.stop_event = stop_event
        self.allow_cloud_ui = allow_cloud_ui
        self.allow_cloud_image = allow_cloud_image

    def answer(self, question: str) -> str:
        if self.stop_event.is_set():
            raise InterruptedError("説明を中止しました")
        if self.planner.provider == "gemini" and not self.allow_cloud_ui:
            raise PermissionError("Geminiへの画面情報送信が許可されていません。Ollamaを使うかクラウド送信を明示的に許可してください")
        # Avoid capturing or forwarding terminal/admin-tool contents, even
        # when their user-editable title looks like an ordinary application.
        guard = getattr(self.windows, "assert_safe_observation", None)
        if guard is not None:
            guard()
        start_hwnd = self.windows.foreground_hwnd()
        context = self.context_provider()
        excerpt = None
        if self.bridge.connected and self.windows.is_chrome_foreground():
            try:
                excerpt = self.bridge.call("read_visible", timeout=8)
            except (OSError, RuntimeError, TimeoutError, ValueError):
                pass  # DOM text not available on some special pages.
        pointer = None
        if wants_pointer(question):
            try:
                pointer = self.windows.pointer_control()
            except (OSError, RuntimeError, ValueError):
                pass
        observation = screen_context(
            context, excerpt, pointer, cloud=self.planner.provider == "gemini"
        )
        if self.stop_event.is_set():
            raise InterruptedError("説明を中止しました")
        screenshot = None
        # Never upload an image unless its separate cloud permission is ON.
        if self.planner.provider != "gemini" or self.allow_cloud_image:
            try:
                before_hwnd = self.windows.foreground_hwnd()
                frame = capture_screen(before_hwnd)
                # Do not forward a screenshot if focus moved mid-capture.
                if self.windows.foreground_hwnd() != before_hwnd:
                    raise PermissionError("画像取得中に画面が切り替わりました。情報を破棄しました")
                if guard is not None:
                    guard()
                screenshot = frame.jpeg
            except PermissionError:
                # A focus change or protected process must not fall back to
                # sending stale textual observations to a cloud model.
                raise
            except (OSError, RuntimeError, ValueError, ImportError):
                pass  # Textual observation is still useful if capture fails.
        if not observation and screenshot is None:
            raise RuntimeError("画面の情報を取得できませんでした")
        if self.stop_event.is_set():
            raise InterruptedError("説明を中止しました")
        # Cloud text-only mode must be protected against a focus change too.
        # This check happens immediately before any provider call.
        if self.windows.foreground_hwnd() != start_hwnd:
            raise PermissionError("画面の確認中に操作対象が変わりました。情報を破棄しました")
        if guard is not None:
            guard()
        answer = self.planner.explain_screen(question, observation, screenshot)
        if self.stop_event.is_set():
            raise InterruptedError("説明を中止しました")
        if not isinstance(answer, str) or not answer.strip():
            raise RuntimeError("画面についての回答を取得できませんでした")
        return answer.strip()[:1200]