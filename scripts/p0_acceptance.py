"""P0 go-live evidence: read-only preflight plus opt-in MANUAL acceptance.

Default: no GUI clicks, keyboard sends, audio capture, user-file traversal,
cloud requests, network, updates, subprocesses, or persisted reports.
Interactive mode only asks the operator to mark named tests PASS/FAIL/SKIP;
it does not perform the tests or accept unstructured notes.
No local report can itself certify production safety or key ownership.
"""
from __future__ import annotations

import argparse
import ast
import json
import os
from pathlib import Path
import re
import sys

ROOT = Path(__file__).resolve().parents[1]
_VERSION = re.compile(r"^(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)$")

# Exactly one of these three keywords is accepted; never collect notes,
# window titles, file names, speech, account data, or screenshots.
MANUAL_CASES = (
    ("P0-1_focus_guard", "切替先の別アプリに誤入力しない（メモ帳のダミー入力のみ）"),
    ("P0-2_gui_stop", "GUI停止後に新しい操作が開始されない"),
    ("P0-2_hotkey_stop", "Ctrl+Alt+Esc 停止後に新しい操作が開始されない"),
    ("P0-2_voice_stop", "ハンズフリーを明示的に有効化した場合だけ音声停止を確認"),
    ("P0-3_dummy_secret", "架空の秘密値だけを用いクラウド送信・表示を確認"),
    ("P0-4_scratch_rollback", "別の隔離した検証コピーで更新中断と復旧を検証"),
    ("P0-5_notepad", "ユーザーが同意したダミーのメモ帳だけを入力"),
    ("P0-5_mic", "ローカル固定フレーズ3回のマイクテスト"),
    ("P0-5_chrome", "ダミーのChromeページで対象タブ切替と拒否を確認"),
    ("P0-5_sleep", "スリープと復帰後に未確認操作を続行しない"),
    ("P0-5_restart", "通常権限で再起動し設定と停止操作を再確認"),
)


def _constant_assignment(tree: ast.Module, name: str):
    for node in tree.body:
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Name) and t.id == name for t in node.targets):
                return node.value.value if isinstance(node.value, ast.Constant) else None
    return None


def bootstrap_metadata(path: Path) -> tuple[int, bool]:
    """Read only two literal constants; never import/execute update code."""
    if not path.is_file() or path.stat().st_size > 128 * 1024:
        return 0, False
    tree = ast.parse(path.read_text(encoding="utf-8"))
    revision = _constant_assignment(tree, "BOOTSTRAP_SECURITY_REVISION")
    pin = _constant_assignment(tree, "PINNED_ED25519_PUBLIC_KEY_HEX")
    configured = isinstance(pin, str) and re.fullmatch(r"[0-9a-f]{64}", pin) is not None
    return (revision if type(revision) is int and revision >= 0 else 0), configured


def _platform_evidence() -> tuple[str, bool | None, int | None]:
    if os.name != "nt":
        return "non-windows", None, None
    try:
        import ctypes
        admin = bool(ctypes.windll.shell32.IsUserAnAdmin())
    except (AttributeError, OSError):
        admin = None
    try:
        build = int(sys.getwindowsversion().build)
    except (AttributeError, ValueError):
        build = None
    return "windows", admin, build


def preflight(root: Path, *, platform_name: str | None = None,
              admin: bool | None = None, windows_build: int | None = None,
              python_version: tuple[int, int] | None = None) -> dict:
    """No I/O outside VERSION, manifest and local bootstrap."""
    root = Path(root)
    version_file = root / "VERSION"
    try:
        version = version_file.read_text(encoding="utf-8").strip()
        version_ok = _VERSION.fullmatch(version) is not None
    except OSError:
        version, version_ok = "", False
    try:
        manifest = json.loads((root / "chrome-extension/manifest.json").read_text(encoding="utf-8"))
        same_extension = (version_ok and isinstance(manifest, dict)
                          and manifest.get("manifest_version") == 3
                          and manifest.get("version") == version)
    except (OSError, ValueError):
        same_extension = False
    try:
        revision, pin_present = bootstrap_metadata(root / "voicepilot_bootstrap.py")
    except (OSError, SyntaxError, UnicodeError):
        revision, pin_present = 0, False
    actual_os, actual_admin, actual_build = _platform_evidence()
    os_label = platform_name if platform_name is not None else actual_os
    admin_value = admin if admin is not None else actual_admin
    build_value = windows_build if windows_build is not None else actual_build
    py = python_version if python_version is not None else sys.version_info[:2]
    checks = {
        "windows_11": "PASS" if os_label == "windows" and isinstance(build_value, int) and build_value >= 22000 else "UNVERIFIED",
        "non_admin": "PASS" if os_label == "windows" and admin_value is False else "BLOCKED",
        "python_3_11": "PASS" if tuple(py) == (3, 11) else "BLOCKED",
        "app_version_format": "PASS" if version_ok else "BLOCKED",
        "chrome_manifest_match": "PASS" if same_extension else "BLOCKED",
        "signed_bootstrap_revision": "PASS" if revision >= 3 else "BLOCKED",
        # This checks presence of a 32-byte-looking key, NOT its authenticity.
        "developer_key_pinned": "UNVERIFIED" if pin_present else "BLOCKED",
    }
    return {
        "schema": 1,
        "version": version if version_ok else "UNKNOWN",
        "checks": checks,
        "public_key_status": "present_identity_unverified" if pin_present else "not_configured",
        "manual_results": {case: "NOT_RUN" for case, _ in MANUAL_CASES},
        "production_approved": False,
        "decision": "P0_INCOMPLETE",
    }


def collect_manual(report: dict, *, ask=input, out=print) -> dict:
    """Explicit operator answers, always incomplete until every test is done."""
    updated = dict(report)
    statuses = dict(report["manual_results"])
    for name, prompt in MANUAL_CASES:
        out(f"{name}: {prompt}")
        out("実機で安全に確認した結果のみ PASS / FAIL / SKIP と入力してください。")
        try:
            answer = ask("> ").strip().upper()
        except (KeyboardInterrupt, EOFError):
            out("中断しました。残りの検証は未実施のままです。")
            break
        if answer not in {"PASS", "FAIL", "SKIP"}:
            statuses[name] = "NOT_RUN"
            out("不正な値です。未実施として記録します。")
        else:
            statuses[name] = answer
    updated["manual_results"] = statuses
    updated["production_approved"] = False
    updated["decision"] = "P0_INCOMPLETE"
    return updated


def save_report(report: dict, destination: Path) -> None:
    """Create-only, opt-in local JSON with no private application content."""
    destination = Path(destination)
    destination.parent.mkdir(parents=True, exist_ok=True)
    with destination.open("x", encoding="utf-8") as target:
        json.dump(report, target, indent=2, ensure_ascii=False, sort_keys=True)
        target.write("\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="VoicePilot P0 safe, read-only go-live evidence collection")
    parser.add_argument("--interactive", action="store_true",
                        help="Manual statuses only. Never controls PC or microphone.")
    parser.add_argument("--output", type=Path,
                        help="Opt-in new JSON output path (will not overwrite an existing file)")
    args = parser.parse_args(argv)
    report = preflight(ROOT)
    if args.interactive:
        if report["checks"]["windows_11"] != "PASS" or report["checks"]["non_admin"] != "PASS":
            print("Windows 11の通常権限でのみ実機の手動確認を受け付けます。")
            print(json.dumps(report, ensure_ascii=False, indent=2))
            return 2
        report = collect_manual(report)
    print(json.dumps(report, ensure_ascii=False, indent=2))
    if args.output is not None:
        try:
            save_report(report, args.output)
        except FileExistsError:
            print("既存の検証結果を上書きしません。別のファイル名を選択してください。", file=sys.stderr)
            return 2
        except OSError:
            print("検証記録を保存できませんでした。", file=sys.stderr)
            return 2
    # A report isn't an authorization to run code or a proof of trust
    # in the owner signing key. Never return a production-safe verdict.
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
