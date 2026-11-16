"""Read-only preview of deterministic voice commands.

This is not VoiceAgent's planner. It neither discovers a foreground window nor
checks approval, and MUST NEVER execute the returned command. Unknown voice
phrases always remain unclassified. Descriptions never include user text.
"""
from __future__ import annotations

from .fast_commands import direct_action


_APP_LABELS = {
    "notepad": "メモ帳", "calculator": "電卓", "explorer": "エクスプローラー",
    "chrome": "Chrome", "paint": "ペイント", "vscode": "VS Code",
    "settings": "Windows設定",
}
_FOLDERS = {"desktop": "デスクトップ", "documents": "ドキュメント",
            "downloads": "ダウンロード"}
_SHORTCUTS = {
    "ctrl+c": "コピー", "ctrl+v": "貼り付け", "ctrl+z": "編集の取り消し",
    "alt+tab": "ウィンドウ切り替え",
}
_ACTIONS = {
    "browser_back": "ブラウザの前のページに戻る",
    "browser_forward": "ブラウザの次のページに進む",
}


def preview_command(transcript: str) -> tuple[str, str]:
    """Return a static (kind, label), no transcription, arguments or secrets.

    kind: recognized / unknown / empty. Recognition is a *candidate* only:
    not evidence of correct audio recognition, target, or safe execution.
    """
    if not isinstance(transcript, str) or not transcript.strip():
        return "empty", "音声が認識されていません"
    if len(transcript) > 2000:
        return "unknown", "長い発話は操作候補として分類しません"
    candidate = direct_action(transcript)
    if candidate is None:
        return "unknown", "既定の安全な定型コマンドには一致しません"
    action, args = candidate
    if action == "windows_launch":
        app = _APP_LABELS.get(args.get("app"))
        name = f"{app}を開く" if app else None
    elif action == "files_show_folder":
        folder = _FOLDERS.get(args.get("folder"))
        name = f"{folder}を表示する" if folder else None
    elif action == "windows_hotkey":
        name = _SHORTCUTS.get(args.get("keys"))
    elif action == "smart_scroll":
        direction = args.get("direction")
        size = args.get("amount")
        if direction not in {"up", "down"} or size not in (None, "large"):
            name = None
        else:
            name = ("上" if direction == "up" else "下") + (
                "端までスクロール" if size == "large" else "にスクロール"
            )
    elif action == "browser_search":
        # Search queries can include passwords and personal data. Never echo
        # the arguments or original text a second time in a preview label.
        name = "Web検索（検索語はプレビューに表示しません）"
    else:
        name = _ACTIONS.get(action)
    if name is None:
        return "unknown", "分類できない操作候補です"
    return "recognized", f"候補：{name}（対象確認・実行はしません）"
