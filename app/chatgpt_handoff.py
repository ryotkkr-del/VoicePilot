"""Deliberate ChatGPT *web draft* handoff, never autonomous ChatGPT inference.

No ChatGPT credentials, OAuth access, API requests or page-output extraction.
The desktop agent performs local PC operations using its existing planner.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from collections.abc import Callable

CHATGPT_HOME = "https://chatgpt.com/"
MAX_PROMPT = 4000


@dataclass(frozen=True)
class HandoffResult:
    """A truthful, narrowly scoped report; 'prepared' never means 'sent'."""
    stage: str  # handoff / handoff_clipboard / interrupted
    message: str


def extract_chatgpt_request(utterance: str) -> str | None:
    """Only explicitly addressed utterances become ChatGPT requests.

    None: regular PC instruction. Empty string: only open ChatGPT.
    Do not assume a selected window or clipboard content was authorized for upload.
    """
    text = utterance.strip()
    match = re.match(r"^(?:チャッピー|チャットGPT|チャットジーピーティー|Chat\s*GPT)\s*(?:に|で|へ)?[\s、,:：]*", text, flags=re.I)
    if not match:
        return None
    rest = text[match.end():].strip(" \t\n、。,:：")
    if rest in {"開いて", "開く", "を開いて", "起動して", "相談したい", "相談して"}:
        return ""
    # Remove only very simple wrappers; preserve the substance of the request.
    rest = re.sub(r"^(?:これを|次のことを|以下を)?(?:質問して|聞いて|相談して)[：:\s、]*", "", rest)
    rest = re.sub(r"(?:って|と)(?:聞いて|質問して)[。！!\s]*$", "", rest)
    return rest.strip(" \t\n、。『』「」")


def prepare_chatgpt_handoff(
    prompt: str,
    bridge,
    copy_to_clipboard: Callable[[str], None],
    open_browser: Callable[[str], bool],
    *,
    cancelled: Callable[[], bool] = lambda: False,
) -> HandoffResult:
    """Open a ChatGPT tab, optionally fill *draft only*, or copy for manual paste.

    Chrome extension is optional. Browser responses are NEVER read or sent back.
    If Chrome is unavailable, open the user's default browser and copy the prompt.
    """
    if len(prompt) > MAX_PROMPT:
        raise ValueError("ChatGPTに渡す指示が長すぎます（最大4000文字）")
    if cancelled():
        return HandoffResult("interrupted", "中止しました")
    chrome_responded = False
    if getattr(bridge, "connected", False):
        try:
            result = bridge.call("chatgpt_prepare", {"prompt": prompt}, timeout=11)
            chrome_responded = bool(result.get("opened"))
            if cancelled():
                return HandoffResult("interrupted", "中止しました（タブは開いている可能性があります）")
            if result.get("prepared"):
                return HandoffResult("handoff", "ChatGPTの入力欄に指示を用意しました。送信は自分で確認してください")
            if not prompt and chrome_responded:
                return HandoffResult("handoff", "ChatGPTを開きました")
        except (RuntimeError, ValueError, ConnectionError, TimeoutError):
            # Connection failures only enable the manual fallback. Never retry
            # automatic insertion: a lost receipt may mean it already occurred.
            pass
    if cancelled():
        return HandoffResult("interrupted", "中止しました")
    if prompt:
        copy_to_clipboard(prompt)
    if not chrome_responded and not open_browser(CHATGPT_HOME):
        if prompt:
            return HandoffResult("handoff_clipboard", "質問をコピーしました。ブラウザでChatGPTを開いて貼り付けてください")
        raise RuntimeError("ブラウザを開けませんでした")
    if prompt:
        return HandoffResult("handoff_clipboard", "ChatGPTを開き、質問をコピーしました。貼り付けてから送信してください")
    return HandoffResult("handoff", "ChatGPTを開きました")