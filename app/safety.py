"""Small, deterministic safeguards at the untrusted text -> desktop boundary.

The model and webpage provide *proposals*, never permission.  This module only
uses the user's instruction and freshly observed UI metadata to decide which
side effects require an explicit on-screen approval.
"""
from __future__ import annotations

import re
from urllib.parse import urlparse, urlsplit, urlunsplit

# Conservatively flag actions with financial, account or sharing consequences.
RISK_WORDS = re.compile(
    r"削除|消去|退会|送信|投稿|公開|支払|決済|注文|購入|振込|認証|ログイン|"
    r"登録|共有|許可|承認|保存|適用|確定|提出|アップロード|インストール|"
    r"delete|remove|submit|purchase|pay|send|post|publish|checkout|"
    r"sign\s?in|sign\s?out|share|allow|accept|agree|save|upload|install|"
    r"confirm|apply|continue|proceed|\bok\b|はい|続行|次へ|確認", re.I
)
SENSITIVE_FIELD = re.compile(
    r"password|passcode|passwd|パスワード|暗証|ワンタイム|otp|2fa|"
    r"認証コード|security\s?code|cvc|cvv|card\s?number|クレジット|"
    r"銀行口座|bank\s?account|api.?key|private.?key|secret|access.?token|"
    r"秘密鍵|シークレット|アクセストークン", re.I
)


def is_sensitive_field(value: object) -> bool:
    return bool(SENSITIVE_FIELD.search(str(value or "")))


def unfamiliar_destination(url: str, instruction: str, context: dict) -> bool:
    """Ask before a model-inferred cross-site navigation.

    A domain explicitly named by the user, or navigation within the currently
    observed site, doesn't add a confirmation dialog. DNS/IP safety checks
    still belong to validate_action() and browser's own URL validation.
    """
    host = (urlparse(url).hostname or "").casefold().rstrip(".")
    if not host:
        return True
    user_text = instruction.casefold()
    if host in user_text:
        return False
    # The user may speak the brand without the www prefix; prefer explicit
    # permission in ambiguous cases instead of inferring authority.
    previous = ((context.get("browser") or {}).get("url") or "")
    previous_host = (urlparse(previous).hostname or "").casefold().rstrip(".")
    return host != previous_host


def approval_summary(action: str, args: dict, context: dict) -> str:
    """Show facts, not the model's suggested justification or typed contents."""
    if action in {"browser_click", "browser_fill"}:
        browser = context.get("browser") or {}
        item = next((e for e in browser.get("elements", []) if e.get("id") == args.get("id")), {})
        label = str(item.get("label") or "名称不明")[:90]
        origin = str(browser.get("url") or "")[:160]
        kind = "クリック" if action == "browser_click" else "入力欄の書き換え"
        dest = f"\nリンク先: {item['external_origin']}" if item.get("external_origin") else ""
        return f"Webサイト: {origin}\n対象: {label}\n操作: {kind}{dest}"
    if action in {"browser_open", "browser_new_tab"}:
        parsed = urlsplit(args["url"])
        public = urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))
        return f"新しいサイトを開く: {public[:170]}（検索条件などは非表示）"
    if action == "browser_search":
        return f"Web検索する: {args['query'][:80]}"
    if action in {"windows_click", "windows_fill"}:
        windows = context.get("windows") or {}
        item = next((e for e in windows.get("controls", []) if e.get("id") == args.get("id")), {})
        name = str(item.get("name") or "名称不明")[:90]
        kind = "クリック" if action == "windows_click" else "入力欄の書き換え"
        return f"アプリ: {str(windows.get('active_title') or '不明')[:90]}\n対象: {name}\n操作: {kind}"
    if action == "windows_hotkey":
        return f"ショートカット: {args.get('keys','')}（現在のアプリに作用します）"
    if action == "windows_type":
        return f"現在のアプリへ文字を入力します（{len(args.get('text',''))}文字、内容は非表示）"
    if action in {"screen_click", "screen_double_click", "screen_drag"}:
        return f"画像上の位置を操作: {str(args.get('target', ''))[:80]}（座標操作）"
    if action in {"files_move", "files_rename"}:
        from pathlib import Path
        return f"ファイル: {Path(args.get('path','')).name}\n操作: {'移動' if action == 'files_move' else '名前変更'}"
    return f"操作: {action}"