"""Opt-in Windows safety smoke test for a user's own PC.

Default mode does not click/type, launch apps, inspect personal files, access
microphone, or transmit data. Only private scratch files are modified.
The optional Notepad path requires explicit confirmation, then targets ONLY
an empty scratch document after process+window+focused-control checks.

The JSON report contains only test names and statuses; it omits window titles,
file paths, screenshots, microphone data and user content.
"""
from __future__ import annotations

import argparse
from dataclasses import dataclass
import re
import json
import os
import shutil
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import win_actions as win
from app.actions import validate_action


def scratch_files() -> None:
    """Real NTFS operations, but with roots redirected to disposable test dirs."""
    with tempfile.TemporaryDirectory(prefix="VoicePilotSafety-") as td:
        base = Path(td)
        docs, downloads = base / "Documents", base / "Downloads"
        docs.mkdir(); downloads.mkdir()
        old = docs / "dummy.txt"
        old.write_text("scratch", encoding="utf-8")
        with patch("app.win_actions.user_roots", return_value=[docs, downloads]):
            win.rename_file(str(old), "renamed.txt")
            renamed = docs / "renamed.txt"
            assert renamed.read_text(encoding="utf-8") == "scratch"
            collide = downloads / "renamed.txt"
            collide.write_text("protected", encoding="utf-8")
            try:
                win.move_file(str(renamed), "downloads")
                raise AssertionError("overwrite was not blocked")
            except FileExistsError:
                assert collide.read_text(encoding="utf-8") == "protected"
            collide.unlink()
            win.move_file(str(renamed), "downloads")
            assert collide.read_text(encoding="utf-8") == "scratch"
            assert not renamed.exists()
            try:
                win.rename_file(str(collide), "malware.exe")
                raise AssertionError("extension change accepted")
            except ValueError:
                pass


def privilege_guard() -> None:
    if os.name == "nt" and win.current_process_elevated():
        raise PermissionError("running as admin is forbidden")


def process_guard() -> None:
    if not win.is_unsafe_foreground("Google Chrome", "cmd.exe"):
        raise AssertionError("window title spoofing passed")
    if not win.is_unsafe_foreground("Normal window", None):
        raise AssertionError("unknown process passed")
    controller = win.WindowsController.__new__(win.WindowsController)
    controller._foreground_process_name = lambda: "cmd.exe"
    controller.active_window_title = lambda: "Google Chrome"
    assert not controller.is_chrome_foreground()
    controller._foreground_process_name = lambda: "chrome.exe"
    assert controller.is_chrome_foreground()


def action_guard() -> None:
    bad = ("shell", "files_delete", "registry_write", "arbitrary_exec")
    for action in bad:
        try:
            validate_action({"action": action, "args": {}})
            raise AssertionError("unsupported action accepted")
        except ValueError:
            pass
    try:
        validate_action({"action": "browser_open", "args": {"url": "http://127.1/"}})
        raise AssertionError("loopback bypass accepted")
    except ValueError:
        pass


def readonly_win32_probe() -> None:
    if os.name != "nt":
        raise RuntimeError("requires Windows")
    controller = win.WindowsController()
    result = controller._foreground_process_name()
    if result is not None and (not isinstance(result, str) or not result.endswith(".exe")):
        raise AssertionError("invalid process identity")
    if not isinstance(controller.is_chrome_foreground(), bool):
        raise AssertionError("invalid Chrome guard")


def _voice_phrase_matches(transcript: str) -> bool:
    """Only compare the spoken scratch phrase; never persist or print text."""
    normalized = re.sub(r"[\s、。!！?？.,]+", "", transcript).casefold()
    return normalized in {"音声のテストです", "音声テストです"}


def interactive_microphone(*, trials: int = 1) -> str:
    """Opt-in local fixed-phrase diagnostic. No audio, transcript or actions saved.

    The normal one-shot test stays backward compatible. A three-trial run reuses
    the same recognizer, so it measures both initial load and subsequent use.
    Whisper may download model weights on the first run.
    """
    if trials not in (1, 3):
        raise ValueError("trials must be 1 or 3")
    if os.name != "nt":
        return "SKIP:Windows only"
    if win.current_process_elevated():
        return "FAIL:admin execution"
    print("\n[任意] ローカルのマイクとWhisperだけを試します。PC操作は行いません。")
    if trials == 3:
        print("同じ短いフレーズを最大8秒ずつ3回録音し、認識成功数と処理時間を測定します。")
        print("1回目はモデルの初期ロード時間を含む場合があります。")
    else:
        print("同意した場合だけ8秒間録音します。")
    print("音声・認識文章は保存も送信もしません。")
    print("開始したら『音声のテストです』と話してください。")
    if input("録音を開始する場合だけ YES と入力してください: ").strip() != "YES":
        return "SKIP:no consent"

    from app.audio import Microphone, SpeechRecognizer
    recognizer = SpeechRecognizer("small")
    durations: list[float | None] = []
    successes: list[bool] = []
    for trial in range(trials):
        if trials == 3:
            print(f"テスト {trial + 1}/3: 『音声のテストです』と話してください。")
        mic = Microphone()
        recording = False
        samples = None
        try:
            mic.start()
            recording = True
            started = time.monotonic()
            heard = False
            while time.monotonic() - started < 8.0:
                time.sleep(0.15)
                activity = mic.activity()
                heard = heard or bool(activity.get("heard_voice", False))
                if (heard and time.monotonic() - started >= 1.0
                        and float(activity.get("silence", 0.0)) >= 1.25):
                    break
            samples = mic.stop()
            recording = False
            if not heard or samples.size < 1600:
                if trials == 1:
                    return "FAIL:no speech detected"
                successes.append(False)
                durations.append(None)
                print(f"テスト {trial + 1}/3: 音声検出なし（不一致）")
                continue

            recognized_at = time.monotonic()
            recognized = recognizer.transcribe(samples)
            elapsed = max(0.0, time.monotonic() - recognized_at)
            matched = _voice_phrase_matches(recognized)
            successes.append(matched)
            durations.append(elapsed)
            # Only retain a Boolean for accuracy. Never print recognized text.
            if trials == 1:
                print(f"ローカル音声認識時間: {elapsed:.2f}秒（認識本文は非表示）")
            else:
                print(f"テスト {trial + 1}/3: {'一致' if matched else '不一致'}、{elapsed:.2f}秒")
            del recognized
        except KeyboardInterrupt:
            return "SKIP:cancelled"
        except Exception as exc:
            return "FAIL:" + type(exc).__name__
        finally:
            if recording:
                try:
                    mic.stop()
                except Exception:
                    pass
            del samples

    if trials == 1:
        return "PASS" if successes[0] else "FAIL:phrase mismatch"
    warm = sorted(x for x in durations[1:] if x is not None)
    if warm:
        median = (warm[len(warm) // 2] if len(warm) % 2
                  else (warm[len(warm) // 2 - 1] + warm[len(warm) // 2]) / 2)
        print(f"モデル再利用時の認識時間（中央値）: {median:.2f}秒")
    print(f"認識成功: {sum(successes)}/3（固定フレーズ完全一致のみ）")
    print("この3回の結果は日常の操作成功率・遅延を保証するものではありません。")
    return "PASS" if all(successes) else f"FAIL:{sum(successes)}/3 phrase matches"


@dataclass(frozen=True)
class ScratchNotepadTarget:
    """Identity captured from a disposable scratch Notepad editor only."""

    hwnd: int
    token: str
    control: str


def assert_scratch_notepad_target(controller, target: ScratchNotepadTarget) -> None:
    """Fail closed on a changed HWND, process, document or caret focus.

    No private content or full window title is captured in diagnostic output.
    The title is only an extra check: executable identity and the UIA focused
    control are checked independently, once before and once per input unit.
    """
    if (not isinstance(target.hwnd, int) or target.hwnd <= 0 or
            not isinstance(target.token, str) or not target.token.startswith("VoicePilot_Test_") or
            len(target.token) > 48 or not target.control):
        raise PermissionError("テスト対象を安全に識別できません")
    if controller.foreground_hwnd() != target.hwnd:
        raise PermissionError("テスト用メモ帳以外のウィンドウに切り替わりました")
    if controller._foreground_process_name() != "notepad.exe":
        raise PermissionError("入力先のプロセスがテスト用メモ帳ではありません")
    if target.token.casefold() not in controller.active_window_title().casefold():
        raise PermissionError("メモ帳のテスト文書が切り替わりました")
    if controller.dictation_probe() != target.control:
        raise PermissionError("メモ帳の入力欄が変わりました")


def pin_scratch_notepad_target(controller, token: str) -> ScratchNotepadTarget:
    """Capture a target only after manual focus of the new dummy document."""
    hwnd = controller.foreground_hwnd()
    if (type(hwnd) is not int or hwnd <= 0 or
            controller._foreground_process_name() != "notepad.exe" or
            not isinstance(token, str) or not token.startswith("VoicePilot_Test_") or
            token.casefold() not in controller.active_window_title().casefold()):
        raise PermissionError("テスト用メモ帳が前面であることを確認できません")
    # Do not even query the caret of another application before confirming
    # the real process and the scratch document's one-time title token.
    candidate = ScratchNotepadTarget(hwnd, token, controller.dictation_probe())
    assert_scratch_notepad_target(controller, candidate)
    return candidate


def interactive_notepad() -> str:
    """Only if user agrees: launch a scratch Notepad then type one harmless line.

    No automatic save, close, hotkeys, mouse clicks, or personal file access.
    Unknown focus or UIA selection prevents typing (skip is not success).
    """
    if os.name != "nt":
        return "SKIP:Windows only"
    if win.current_process_elevated():
        return "FAIL:admin execution"
    print("\n[任意] テスト用の空のメモ帳だけに文字を入力します。")
    print("ほかのアプリ・個人ファイルには入力しません。確認できなければ入力せず中止します。")
    consent = input("実行する場合だけ YES と入力してください: ").strip()
    if consent != "YES":
        return "SKIP:no consent"
    # Notepad may keep the blank test file open. Best-effort cleanup avoids
    # reporting a false application failure if Windows locks that scratch file.
    folder = tempfile.mkdtemp(prefix="VoicePilotNotepad-")
    try:
        token = "VoicePilot_Test_" + uuid.uuid4().hex[:10]
        scratch = Path(folder) / (token + ".txt")
        scratch.write_text("", encoding="utf-8")
        try:
            binary = win.resolved_known_app("notepad")
            subprocess.Popen([binary, str(scratch)], shell=False)
        except (OSError, ValueError) as exc:
            return "SKIP:Notepad could not be launched"
        print("メモ帳のテスト用ウィンドウが開いたら、空の本文をクリックしてカーソルを置いてください。")
        print("7秒後に入力先のプロセス・文書名・カーソルを検証します。")
        time.sleep(7)
        controller = win.WindowsController()
        try:
            target = pin_scratch_notepad_target(controller, token)
            # No newline, save, undo or hotkey: these are just literal chars.
            # Re-check target HWND, actual process, exact scratch-document
            # token and UIA caret BEFORE EACH UTF-16 unit is synthesized.
            message = "VoicePilot dummy: ^s +{DELETE} %{F4}"
            controller.dictation_insert(
                message, target.control,
                extra_guard=lambda: assert_scratch_notepad_target(controller, target),
            )
            assert_scratch_notepad_target(controller, target)
        except (OSError, RuntimeError, ValueError, PermissionError):
            return "SKIP:editor focus or document could not be safely verified"
        print("テスト用メモ帳に記号がそのまま表示されたか、目視で確認してください。")
        print("入力先は各文字の直前に再確認しましたが、フォーカス競合を完全には防げません。")
        print("保存操作や他ウィンドウへの意図的な操作は行っていません。")
        return "MANUAL:typed into verified scratch Notepad; inspect display"
    finally:
        shutil.rmtree(folder, ignore_errors=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="VoicePilot scratch-only Windows safety diagnostics")
    parser.add_argument("--interactive-notepad", action="store_true",
                        help="Optional Notepad typing; asks explicit consent")
    parser.add_argument("--interactive-microphone", action="store_true",
                        help="Optional local Whisper mic test; asks explicit consent")
    parser.add_argument("--interactive-microphone-benchmark", action="store_true",
                        help="Optional 3-trial mic recognition and timing; asks explicit consent")
    args = parser.parse_args()
    checks = [
        ("privilege", privilege_guard),
        ("window_process_identity", process_guard),
        ("allowed_actions", action_guard),
        ("scratch_file_integrity", scratch_files),
    ]
    if os.name == "nt":
        checks.append(("win32_read_only", readonly_win32_probe))
    results = {}
    for name, fn in checks:
        try:
            fn()
            results[name] = "PASS"
        except Exception as exc:
            results[name] = "FAIL:" + type(exc).__name__
    if args.interactive_notepad and all(v == "PASS" for v in results.values()):
        results["notepad_typing"] = interactive_notepad()
    elif args.interactive_notepad:
        results["notepad_typing"] = "SKIP:preflight failed"
    if args.interactive_microphone and all(not v.startswith("FAIL") for v in results.values()):
        results["local_microphone"] = interactive_microphone()
    elif args.interactive_microphone:
        results["local_microphone"] = "SKIP:preflight failed"
    if args.interactive_microphone_benchmark and all(not v.startswith("FAIL") for v in results.values()):
        results["microphone_benchmark"] = interactive_microphone(trials=3)
    elif args.interactive_microphone_benchmark:
        results["microphone_benchmark"] = "SKIP:preflight failed"
    print("\nVoicePilot Windows safety report (no private text captured)")
    print(json.dumps({"version": (PROJECT_ROOT / "VERSION").read_text(encoding="utf-8").strip(), "os": "Windows" if os.name == "nt" else "Other",
                      "results": results}, ensure_ascii=False, indent=2))
    return 1 if any(v.startswith("FAIL") for v in results.values()) else 0


if __name__ == "__main__":
    # Windows hosts sometimes use a legacy codepage (e.g. cp1252).
    # Print predictable UTF-8 diagnostics instead of crashing on Japanese text.
    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except (OSError, ValueError):
            pass
    raise SystemExit(main())