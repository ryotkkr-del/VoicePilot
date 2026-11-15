"""Constrained Windows UI Automation and user-visible file actions.

No shell, arbitrary code execution, deletion, or programmatic writes.
"""
from __future__ import annotations

import ctypes
import ntpath
from ctypes import wintypes
from collections import deque
import os
from pathlib import Path, PureWindowsPath
import re
import shutil
import subprocess
import time
from functools import lru_cache

APP_COMMANDS = {
    "notepad": ["notepad.exe"], "メモ帳": ["notepad.exe"],
    "calculator": ["calc.exe"], "電卓": ["calc.exe"],
    "explorer": ["explorer.exe"], "エクスプローラー": ["explorer.exe"],
    "paint": ["mspaint.exe"], "ペイント": ["mspaint.exe"],
    "vscode": ["Code.exe"], "vs code": ["Code.exe"],
    "edge": ["msedge.exe"], "firefox": ["firefox.exe"],
    "word": ["WINWORD.EXE"], "excel": ["EXCEL.EXE"],
    "powerpoint": ["POWERPNT.EXE"],
    "chrome": ["chrome.exe"],
    "settings": [],
}
ALLOWED_KEYS = {
    "ctrl+c": "^c", "ctrl+v": "^v", "ctrl+a": "^a", "ctrl+z": "^z",
    "ctrl+f": "^f", "ctrl+s": "^s", "alt+tab": "%{TAB}",
    "esc": "{ESC}", "enter": "{ENTER}", "tab": "{TAB}",
    "ctrl+tab": "^{TAB}", "ctrl+shift+tab": "^+{TAB}",
    "ctrl+l": "^l", "win+e": "{VK_LWIN down}e{VK_LWIN up}",
    "home": "{HOME}", "end": "{END}",
    "up": "{UP}", "down": "{DOWN}", "left": "{LEFT}", "right": "{RIGHT}",
    "pgup": "{PGUP}", "pgdn": "{PGDN}",
}
SAFE_EXTENSIONS = {".pdf", ".txt", ".md", ".png", ".jpg", ".jpeg", ".webp",
                   ".docx", ".xlsx", ".pptx", ".csv", ".mp3", ".wav", ".mp4"}
# Process identity is checked independently of user-editable window titles.
BLOCKED_INPUT_PROCESSES = frozenset({
    "cmd.exe", "powershell.exe", "pwsh.exe", "windowsterminal.exe", "wt.exe",
    "conhost.exe", "bash.exe", "wsl.exe", "wslhost.exe", "regedit.exe",
    "mmc.exe", "taskmgr.exe", "sudo.exe", "mshta.exe", "wscript.exe",
    "cscript.exe", "rundll32.exe", "python.exe", "pythonw.exe",
    "msiexec.exe", "schtasks.exe", "reg.exe", "robocopy.exe",
})
UNSAFE_WINDOW = re.compile(r"powershell|command prompt|コマンド.?プロンプト|terminal|ターミナル|cmd\.exe|"
                           r"windows terminal|管理者:|administrator:", re.I)
SENSITIVE_CONTROL = re.compile(
    r"password|passwd|passcode|パスワード|暗証|認証コード|ワンタイム|otp|2fa|"
    r"クレジット|card\s?number|cvv|cvc|secret|シークレット|token|api.?key|private.?key",
    re.I,
)

BLOCKED_APP = re.compile(
    r"powershell|cmd(?:\.exe)?|terminal|command prompt|regedit|registry editor|"
    r"ターミナル|コマンドプロンプト|レジストリ|管理者|administrator", re.I
)


def is_unsafe_foreground(title: str, executable: str | None) -> bool:
    """Pure safety rule; a changed terminal title is never sufficient to bypass."""
    return (bool(UNSAFE_WINDOW.search(title)) or not executable
            or ntpath.basename(executable).casefold() in BLOCKED_INPUT_PROCESSES)


def validated_shortcut_target(target: str, arguments: str = "") -> str:
    """Never treat an attacker-controlled .lnk as a shell command.

    Unknown app shortcuts are intentionally conservative: only local EXE
    targets, not scripts, files, UNC paths, argument-bearing commands or URIs.
    """
    if (not isinstance(target, str) or not target.strip() or not isinstance(arguments, str)
            or arguments.strip() or target.startswith(("\\\\", "//"))
            or any(ch in target for ch in '\r\n\x00')):
        raise ValueError("ショートカットの起動先を確認できません")
    parsed = Path(target)
    if not PureWindowsPath(target).is_absolute():
        raise ValueError("相対パスのショートカットは起動できません")
    if parsed.suffix.casefold() != ".exe" or ntpath.basename(target).casefold() in BLOCKED_INPUT_PROCESSES:
        raise ValueError("スクリプト・管理ツールを指すショートカットは起動できません")
    if BLOCKED_APP.search(ntpath.splitext(ntpath.basename(target))[0]):
        raise ValueError("危険なツールへのショートカットは起動できません")
    return target


def trusted_install_executable(target: str, roots: list[str] | None = None) -> bool:
    """Allow only installed EXEs, not an arbitrary program in Downloads/cwd.

    Uses Windows path semantics even when the safety tests run on Linux.
    """
    if not isinstance(target, str) or not PureWindowsPath(target).is_absolute():
        return False
    if roots is None:
        roots = [x for x in (
            os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR"),
            os.environ.get("ProgramFiles"),
            os.environ.get("ProgramFiles(x86)"),
            str(Path(os.environ["LOCALAPPDATA"]) / "Programs") if os.environ.get("LOCALAPPDATA") else None,
        ) if x]
    target_norm = ntpath.normcase(ntpath.normpath(target))
    for root in roots:
        if not PureWindowsPath(root).is_absolute():
            continue
        root_norm = ntpath.normcase(ntpath.normpath(root))
        try:
            if ntpath.commonpath([target_norm, root_norm]) == root_norm:
                return True
        except ValueError:
            continue
    return False


def resolved_known_app(app: str) -> str:
    """Resolve an allowlisted app to an absolute trusted EXE before launching."""
    command = APP_COMMANDS[app]
    if not command:
        raise ValueError("このアプリは専用の起動方法が必要です")
    executable = command[0]
    windows_root = os.environ.get("SYSTEMROOT") or os.environ.get("WINDIR")
    system_apps = {"notepad.exe", "calc.exe", "explorer.exe", "mspaint.exe"}
    candidate = (str(Path(windows_root) / "System32" / executable)
                 if windows_root and executable.casefold() in system_apps else None)
    if not candidate or not Path(candidate).is_file():
        candidate = shutil.which(executable)
    if not candidate or not trusted_install_executable(candidate):
        raise FileNotFoundError("確認できる場所にインストールされたアプリが見つかりません")
    return candidate


def resolve_start_shortcut(path: Path) -> str:
    """Resolve locally via Windows Shell Link, then launch the EXE, not the .lnk.

    Windows COM stays on the calling worker thread; we never trust the .lnk
    name alone or pass its arguments to subprocess. No shell=True.
    """
    if os.name != "nt" or path.suffix.casefold() != ".lnk" or path.is_symlink():
        raise ValueError("Windowsの通常のショートカットだけ起動できます")
    import pythoncom
    from win32com.client import Dispatch
    pythoncom.CoInitialize()
    try:
        link = Dispatch("WScript.Shell").CreateShortcut(str(path))
        target = validated_shortcut_target(str(link.TargetPath), str(link.Arguments))
        if not Path(target).is_file() or not trusted_install_executable(str(Path(target).resolve())):
            raise FileNotFoundError("信頼できるインストール場所に起動先がありません")
        return target
    finally:
        pythoncom.CoUninitialize()


def _is_password_control(control) -> bool:
    """UIA backend versions expose the password flag as a bool or callable."""
    try:
        value = getattr(control.element_info, "is_password", False)
        if callable(value):
            value = value()
        return bool(value)
    except Exception:
        # Unknown password state should not permit blind filling.
        return True


@lru_cache(maxsize=1)
def start_menu_shortcuts() -> dict[str, Path]:
    """Discover user-installed apps without ever accepting an arbitrary command."""
    roots = [
        Path(os.environ.get('APPDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs',
        Path(os.environ.get('PROGRAMDATA', '')) / 'Microsoft' / 'Windows' / 'Start Menu' / 'Programs',
    ]
    shortcuts: dict[str, Path] = {}
    for root in roots:
        if not root.is_dir():
            continue
        for path in root.rglob('*.lnk'):
            if path.is_symlink() or BLOCKED_APP.search(path.stem):
                continue
            shortcuts.setdefault(path.stem.casefold(), path)
    return shortcuts


def app_shortcut(name: str) -> Path:
    name = name.strip().casefold()
    if len(name) < 2 or len(name) > 90 or BLOCKED_APP.search(name):
        raise ValueError('アプリ名を確認してください（コマンド実行はできません）')
    shortcuts = start_menu_shortcuts()
    if name in shortcuts:
        return shortcuts[name]
    candidates = [path for title, path in shortcuts.items() if name in title]
    if len(candidates) == 1:
        return candidates[0]
    if not candidates:
        raise FileNotFoundError(f'インストール済みアプリが見つかりません: {name}')
    raise ValueError('同じ名前の候補が複数あります: ' + ', '.join(p.stem for p in candidates[:5]))


def visible_window_titles(limit: int = 22) -> list[dict]:
    """Enumerate titles with the low-overhead Win32 API, not UI Automation.

    Accessing a full UIA tree just to list taskbar windows is costly, especially
    on browsers and apps with many accessibility nodes.
    """
    if os.name != "nt":
        return []
    titles: list[dict] = []
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.GetWindowTextLengthW.argtypes = (wintypes.HWND,)
    user32.GetWindowTextLengthW.restype = ctypes.c_int
    user32.IsWindowVisible.argtypes = (wintypes.HWND,)
    user32.IsWindowVisible.restype = wintypes.BOOL
    user32.EnumWindows.argtypes = (callback_type, wintypes.LPARAM)
    user32.EnumWindows.restype = wintypes.BOOL

    @callback_type
    def collect(hwnd, unused):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = min(user32.GetWindowTextLengthW(hwnd), 120)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, len(buf))
        if buf.value:
            titles.append({"title": buf.value[:100]})
        return len(titles) < limit

    user32.EnumWindows(collect, 0)
    return titles


class WindowsController:
    def __init__(self):
        # Windows HWND values are pointer-sized; ctypes defaults to c_int and
        # silently truncates them on 64-bit Python unless signatures are set.
        user32 = ctypes.windll.user32
        user32.GetForegroundWindow.restype = wintypes.HWND
        user32.GetWindowTextW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
        user32.IsWindow.argtypes = (wintypes.HWND,)
        user32.IsIconic.argtypes = (wintypes.HWND,)
        user32.SetForegroundWindow.argtypes = (wintypes.HWND,)
        user32.ShowWindow.argtypes = (wintypes.HWND, ctypes.c_int)
        user32.MoveWindow.argtypes = (
            wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.BOOL,
        )
        self._visible_controls = []
        self._visible_metadata = []
        self._last_control_window = None
        # Prefer the Chrome DOM adapter. If it is unavailable, a bounded UIA
        # traversal can still reach ordinary Chrome buttons and text fields.
        self.allow_chrome_uia_fallback = False

    @staticmethod
    def _foreground_hwnd():
        return ctypes.windll.user32.GetForegroundWindow()

    def foreground_hwnd(self) -> int:
        return self._foreground_hwnd()

    def focus_handle(self, hwnd: int) -> bool:
        """Try to return focus to the app used before clicking our own panel."""
        if not hwnd or not ctypes.windll.user32.IsWindow(hwnd):
            return False
        if ctypes.windll.user32.IsIconic(hwnd):
            ctypes.windll.user32.ShowWindow(hwnd, 9)  # Restore only minimized windows.
        return bool(ctypes.windll.user32.SetForegroundWindow(hwnd))

    def _dictation_focus(self) -> tuple[str, str]:
        """Return focused editor identity without reading its value or text.

        This uses UI Automation's focused *control*, rather than a broad UI tree
        or a best-guess field based on the current window title. Never fall back
        to blind keyboard typing if UIA cannot identify an editable control.
        """
        self._ensure_not_terminal()
        import pythoncom
        pythoncom.CoInitialize()
        try:
            from pywinauto.uia_element_info import UIAElementInfo
            focused = UIAElementInfo.get_active()
            if focused is None:
                raise ValueError("現在の入力欄を検出できません")
            control_type = str(focused.control_type or "")
            if control_type not in {"Edit", "Document"}:
                raise ValueError("テキスト入力欄にカーソルを置いてください")
            element = focused.element
            if bool(element.CurrentIsPassword):
                raise ValueError("パスワード欄には音声入力できません")
            if not bool(element.CurrentIsEnabled) or bool(element.CurrentIsOffscreen):
                raise ValueError("入力欄が利用できません")
            if not bool(element.CurrentIsKeyboardFocusable):
                raise ValueError("キーボード入力できない欄です")
            # An accessibility name sometimes encodes a credential field label.
            if SENSITIVE_CONTROL.search(str(focused.name or "")[:150]):
                raise ValueError("機密情報の入力欄は利用できません")
            # ValuePattern is optional (notably on Office/VS Code Documents).
            from pywinauto.windows.uia_defines import IUIA
            i = IUIA()
            try:
                value_pattern = element.GetCurrentPattern(i.UIA_dll.UIA_ValuePatternId)
                if value_pattern is not None:
                    iface = value_pattern.QueryInterface(i.ui_automation_client.IUIAutomationValuePattern)
                    if bool(iface.CurrentIsReadOnly):
                        raise ValueError("読み取り専用の入力欄です")
            except ValueError:
                raise
            except Exception:
                pass  # Not all editors support the ValuePattern.
            # Prevent replacing selected text. Try UIA TextPattern selection,
            # then classic Win32 EM_GETSEL. An unknown selection fails closed.
            selection_checked = False
            try:
                text_pattern = element.GetCurrentPattern(i.UIA_dll.UIA_TextPatternId)
                if text_pattern is not None:
                    pattern = text_pattern.QueryInterface(i.ui_automation_client.IUIAutomationTextPattern)
                    ranges = pattern.GetSelection()
                    if ranges is not None and ranges.Length > 0:
                        for index in range(ranges.Length):
                            selected = ranges.GetElement(index)
                            if selected.CompareEndpoints(0, selected, 1) != 0:
                                raise ValueError("選択された文章の上書きは許可していません")
                        selection_checked = True
            except ValueError:
                raise
            except Exception:
                pass
            if not selection_checked:
                try:
                    hwnd = int(focused.handle or 0)
                    buf = ctypes.create_unicode_buffer(100)
                    user32 = ctypes.windll.user32
                    user32.GetClassNameW.argtypes = (wintypes.HWND, wintypes.LPWSTR, ctypes.c_int)
                    user32.GetClassNameW.restype = ctypes.c_int
                    if hwnd and user32.GetClassNameW(hwnd, buf, len(buf)):
                        klass = buf.value.lower()
                        if klass.startswith(('edit', 'richedit')):
                            start, end = wintypes.DWORD(), wintypes.DWORD()
                            user32.SendMessageW.argtypes = (
                                wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)
                            user32.SendMessageW.restype = wintypes.LPARAM
                            user32.SendMessageW(
                                hwnd, 0x00B0, ctypes.addressof(start), ctypes.addressof(end))
                            if start.value != end.value:
                                raise ValueError("選択された文章の上書きは許可していません")
                            selection_checked = True
                except ValueError:
                    raise
                except Exception:
                    pass
            if not selection_checked:
                raise ValueError("このアプリではカーソル位置を安全に確認できません")
            runtime_id = tuple(int(v) for v in element.GetRuntimeId())
            if not runtime_id:
                raise ValueError("入力欄を識別できません")
            return (control_type, ",".join(map(str, runtime_id)))
        finally:
            pythoncom.CoUninitialize()

    def dictation_probe(self) -> str:
        control_type, identity = self._dictation_focus()
        return control_type + ":" + identity

    def dictation_insert(self, text: str, expected: str) -> None:
        """Write literal Unicode at the caret without interpreting hotkey syntax."""
        from .dictation import DictationError, MAX_CHARS
        if (not isinstance(text, str) or not 0 < len(text) <= MAX_CHARS or
                any(ord(c) < 32 and c != "\n" for c in text)):
            raise DictationError("入力できない文字が含まれています")
        current = self.dictation_probe()
        if current != expected:
            raise DictationError("入力欄が変わりました。入力モードを開始し直してください")
        # Enter can submit messages in many desktop applications. Only permit
        # a newline in a UIA Document (rich editor), never a generic Edit field.
        if "\n" in text and not expected.startswith("Document:"):
            raise DictationError("この入力欄では改行の安全性を確認できません。文章入力を続ける前に入力先を確認してください")
        _send_literal_unicode(text, on_chunk=lambda: self._dictation_check(expected))

    def _dictation_check(self, expected: str) -> None:
        if self.dictation_probe() != expected:
            raise ValueError("入力中にフォーカスが変わりました。入力を中断します")

    def inspect_point(self, x: int, y: int) -> dict:
        """Identify the real UIA control under a visual click when possible."""
        self._ensure_not_terminal()
        try:
            from pywinauto import Desktop
            item = Desktop(backend='uia').from_point(x, y)
            kind = (item.element_info.control_type or '')[:40]
            # Edit window_text may contain the user's private typed value.
            if kind in {'Edit', 'Document'} or _is_password_control(item):
                return {'name': '入力欄（内容は非表示）', 'type': kind}
            return {'name': (item.window_text() or '')[:90], 'type': kind}
        except Exception:
            return {}

    def pointer_control(self) -> dict:
        """Read-only hint under the real mouse cursor, with editor contents redacted."""
        if os.name != 'nt':
            return {}
        self._ensure_not_terminal()
        point = wintypes.POINT()
        if not ctypes.windll.user32.GetCursorPos(ctypes.byref(point)):
            return {}
        return self.inspect_point(point.x, point.y)

    def click_point(self, x: int, y: int, double: bool = False) -> str:
        self._ensure_not_terminal()
        user32 = ctypes.windll.user32
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        if not (left <= x < left + width and top <= y < top + height):
            raise ValueError('画面外へのクリックは禁止しています')
        from pywinauto import mouse
        if double:
            mouse.double_click(button='left', coords=(x, y))
        else:
            mouse.click(button='left', coords=(x, y))
        return '画面上の指定した位置を操作しました' 

    def drag_point(self, start_x: int, start_y: int, end_x: int, end_y: int) -> str:
        """Controlled on-screen drag (the agent always requests approval)."""
        self._ensure_not_terminal()
        user32 = ctypes.windll.user32
        left, top = user32.GetSystemMetrics(76), user32.GetSystemMetrics(77)
        width, height = user32.GetSystemMetrics(78), user32.GetSystemMetrics(79)
        if not all(left <= x < left + width and top <= y < top + height
                   for x, y in ((start_x, start_y), (end_x, end_y))):
            raise ValueError("ドラッグ先が画面の外です")
        from pywinauto import mouse
        mouse.move(coords=(start_x, start_y))
        mouse.press(button="left", coords=(start_x, start_y))
        try:
            for step in range(1, 6):
                fraction = step / 5
                pos = (round(start_x + (end_x - start_x) * fraction),
                       round(start_y + (end_y - start_y) * fraction))
                mouse.move(coords=pos)
                time.sleep(0.025)
        finally:
            mouse.release(button="left", coords=(end_x, end_y))
        return "指定した位置までドラッグしました"

    def active_window_title(self) -> str:
        if os.name != "nt":
            return ""
        hwnd = self._foreground_hwnd()
        buf = ctypes.create_unicode_buffer(512)
        ctypes.windll.user32.GetWindowTextW(hwnd, buf, len(buf))
        return buf.value

    def is_chrome_foreground(self) -> bool:
        title = self.active_window_title().lower()
        return "chrome" in title or "クローム" in title

    @staticmethod
    def _foreground_process_name() -> str | None:
        """Get the *actual* foreground executable, not a spoofable window title.

        Failure is handled as unsafe for synthesized keyboard/mouse events.
        PROCESS_QUERY_LIMITED_INFORMATION does not request elevated access.
        """
        if os.name != "nt":
            return None
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None
        pid = wintypes.DWORD()
        user32.GetWindowThreadProcessId.argtypes = (wintypes.HWND, ctypes.POINTER(wintypes.DWORD))
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD
        if not user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid)) or not pid.value:
            return None
        kernel32.OpenProcess.argtypes = (wintypes.DWORD, wintypes.BOOL, wintypes.DWORD)
        kernel32.OpenProcess.restype = wintypes.HANDLE
        process = kernel32.OpenProcess(0x1000, False, pid.value)
        if not process:
            return None
        try:
            buf = ctypes.create_unicode_buffer(32768)
            size = wintypes.DWORD(len(buf))
            kernel32.QueryFullProcessImageNameW.argtypes = (
                wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD))
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
            if not kernel32.QueryFullProcessImageNameW(process, 0, buf, ctypes.byref(size)):
                return None
            return Path(buf.value).name.casefold()
        finally:
            kernel32.CloseHandle.argtypes = (wintypes.HANDLE,)
            kernel32.CloseHandle(process)

    def _ensure_not_terminal(self):
        # Title-only checks are bypassable by changing a terminal's title.
        # Refuse unknown/elevated processes when sending synthetic input.
        if os.name == "nt" and is_unsafe_foreground(
                self.active_window_title(), self._foreground_process_name()):
            raise ValueError("操作先のプロセスを安全に確認できません。操作を中止しました")

    def quick_state(self) -> dict:
        """Cheap postcondition probe, without touching the UIA tree."""
        return {"active_title": self.active_window_title(),
                "windows": visible_window_titles(limit=35)}

    def snapshot(self) -> dict:
        # A Win32 title list is far cheaper than Desktop(...).windows().
        hwnd = self._foreground_hwnd()
        title = self.active_window_title()
        result = {"windows": visible_window_titles(), "active_title": title[:120], "controls": [],
                  "window_ref": str(hwnd) if hwnd else ""}
        self._visible_controls = []
        self._visible_metadata = []
        self._last_control_window = hwnd
        # Do not traverse Chrome's accessibility tree when the fast DOM bridge
        # is working.  If disconnected, use the same bounded UIA fallback as
        # other apps; a separate extension should not be required to try.
        if ((self.is_chrome_foreground() and not self.allow_chrome_uia_fallback)
                or UNSAFE_WINDOW.search(title)):
            return result
        try:
            from pywinauto import Desktop
            active = Desktop(backend="uia").window(handle=hwnd)
            # pywinauto.descendants() traverses the ENTIRE UI tree before a
            # Python slice can trim it. Instead bound traversal itself.
            queue = deque([(active, 0)])
            scanned = 0
            while queue and scanned < 220 and len(self._visible_controls) < 65:
                parent, depth = queue.popleft()
                scanned += 1
                if depth >= 8:
                    continue
                try:
                    children = parent.children()[:90]
                except Exception:
                    continue
                for control in children:
                    if len(queue) < 250:
                        queue.append((control, depth + 1))
                    try:
                        name = control.window_text().strip()
                        control_type = control.element_info.control_type or ""
                        if not (name or control_type in {"Edit", "Button", "ComboBox", "ListItem", "CheckBox"}):
                            continue
                        # UIA Edit.window_text() is sometimes the user's *input
                        # value*, not its label. Never send that value to an LLM.
                        if not control.is_visible():
                            continue
                        if control_type == "Edit" and (SENSITIVE_CONTROL.search(name) or
                                _is_password_control(control)):
                            continue
                        public_name = "入力欄" if control_type == "Edit" else name[:70]
                        idx = str(len(self._visible_controls))
                        self._visible_controls.append(control)
                        # Keep the raw value ONLY in RAM to detect target changes;
                        # it must not be serialized into an AI context.
                        self._visible_metadata.append((name[:70], control_type))
                        result["controls"].append({"id": idx, "name": public_name, "type": control_type})
                        if len(self._visible_controls) >= 65:
                            break
                    except Exception:
                        continue
        except Exception:
            # UIA can be unavailable for elevated or custom-rendered windows;
            # the agent can then request a one-shot screenshot.
            pass
        return result

    def _check_fill_target(self, item) -> None:
        self._ensure_not_terminal()
        if (self._foreground_hwnd() != self._last_control_window
                or not item.is_visible()
                or _is_password_control(item)):
            raise RuntimeError("文字入力先が変化しました。入力を中止します")

    def act(self, name: str, args: dict):
        from pywinauto import Desktop, keyboard
        if name == "windows_focus":
            title = str(args.get("title", "")).strip().lower()
            if len(title) < 2:
                raise ValueError("ウィンドウ名は2文字以上必要です")
            matches = [w for w in Desktop(backend="uia").windows()
                       if title in w.window_text().lower() and w.is_visible()]
            if not matches:
                raise ValueError("指定したウィンドウが見つかりません")
            if len(matches) > 1:
                exact = [w for w in matches if w.window_text().strip().casefold() == title.casefold()]
                if len(exact) != 1:
                    raise ValueError("同じような名前のウィンドウが複数あります。より詳しく指定してください")
                matches = exact
            matches[0].set_focus()
            return "ウィンドウを切り替えました"
        if name == "windows_launch":
            app = str(args.get("app", "")).lower()
            if app not in APP_COMMANDS:
                shortcut = app_shortcut(app)
                executable = resolve_start_shortcut(shortcut)
                subprocess.Popen([executable], shell=False)
                return f'{shortcut.stem} を起動しました' 
            if app == "settings":
                os.startfile("ms-settings:")
            elif app == "chrome":
                try:
                    binary = resolved_known_app(app)
                except (FileNotFoundError, ValueError):
                    # Chrome often isn't on PATH. Its Start Menu link is
                    # resolved to a vetted EXE, never executed as a .lnk.
                    binary = resolve_start_shortcut(app_shortcut("Google Chrome"))
                subprocess.Popen([binary], shell=False)
            else:
                if app == "vscode":
                    path = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" /
                            "Microsoft VS Code" / "Code.exe")
                    binary = str(path) if path.is_file() else resolved_known_app(app)
                    if not trusted_install_executable(binary):
                        raise ValueError("不明な場所のプログラムは実行できません")
                else:
                    binary = resolved_known_app(app)
                subprocess.Popen([binary], shell=False)
            return "アプリを起動しました"
        if name in {"windows_arrange", "windows_minimize", "windows_maximize"}:
            self._ensure_not_terminal()
            windows = [w for w in Desktop(backend="uia").windows() if w.is_visible()]
            def match(title):
                if len(title.strip()) < 2:
                    raise ValueError("ウィンドウ名は2文字以上にしてください")
                candidates = [w for w in windows if title.lower() in w.window_text().lower()]
                if not candidates:
                    raise FileNotFoundError(f"ウィンドウが見つかりません: {title}")
                if len(candidates) > 1:
                    exact = [w for w in candidates if w.window_text().strip().casefold() == title.strip().casefold()]
                    if len(exact) != 1:
                        raise ValueError("同じような名前のウィンドウが複数あります。より詳しく指定してください")
                    candidates = exact
                return candidates[0].handle
            user32 = ctypes.windll.user32
            if name == "windows_arrange":
                left, right = match(args.get("left_title", "")), match(args.get("right_title", ""))
                if left == right:
                    raise ValueError("異なる2つのウィンドウを指定してください")
                screen_w, screen_h = user32.GetSystemMetrics(0), user32.GetSystemMetrics(1)
                if screen_w < 400 or screen_h < 300:
                    raise RuntimeError("画面サイズを取得できません")
                # Primary display; Windows taskbar area may overlap slightly.
                half = screen_w // 2
                for hwnd, x, width in [(left,0,half),(right,half,screen_w-half)]:
                    user32.ShowWindow(hwnd,9)  # SW_RESTORE
                    if not user32.MoveWindow(hwnd,x,0,width,screen_h-50,True):
                        raise RuntimeError("ウィンドウの移動に失敗しました")
                return "2つのウィンドウを左右に配置しました"
            hwnd = match(args.get("title", ""))
            user32.ShowWindow(hwnd, 6 if name == "windows_minimize" else 3)
            return "ウィンドウの表示状態を変更しました"
        if name in {"windows_click", "windows_fill"}:
            self._ensure_not_terminal()
            if self._foreground_hwnd() != self._last_control_window:
                raise RuntimeError("操作対象のウィンドウが変化しました。もう一度指示してください")
            try:
                idx = int(args.get("id", "-1"))
            except ValueError as exc:
                raise ValueError("UI要素のIDが不正です") from exc
            if not 0 <= idx < len(self._visible_controls):
                raise ValueError("UI要素が見つかりません")
            item = self._visible_controls[idx]
            if not item.is_visible():
                raise RuntimeError("要素が見えなくなりました。もう一度指示してください")
            # UIA wrapper IDs are index-based. The window can update its UI
            # while keeping the same HWND and wrapper; recheck semantics first.
            metadata = getattr(self, "_visible_metadata", [])
            if idx >= len(metadata):
                raise RuntimeError("操作対象の記録がありません。画面を再確認してください")
            expected_name, expected_type = metadata[idx]
            if (item.window_text().strip()[:70] != expected_name or
                (item.element_info.control_type or "") != expected_type):
                raise RuntimeError("ボタンや入力欄が変化しました。もう一度画面を確認してください")
            if hasattr(item, "is_enabled") and not item.is_enabled():
                raise RuntimeError("操作対象が無効になっています。画面を再確認してください")
            if name == "windows_click":
                try:
                    item.invoke()
                except Exception:
                    item.click_input()
                return "画面上の要素をクリックしました"
            text = str(args.get("text", ""))
            if not text or len(text) > 3000 or any(ord(ch) < 32 for ch in text):
                raise ValueError("入力する文字数または制御文字が不正です")
            if ((item.element_info.control_type or "") != "Edit" or
                    SENSITIVE_CONTROL.search(item.window_text()) or
                    _is_password_control(item)):
                raise ValueError("この欄への文字入力は許可していません")
            try:
                item.set_edit_text(text)
            except Exception:
                item.set_focus()
                self._ensure_not_terminal()
                if self._foreground_hwnd() != self._last_control_window:
                    raise RuntimeError("入力先が変化したため停止しました")
                keyboard.send_keys("^a")  # Fixed hotkey, never user-supplied text.
                _send_literal_unicode(text, on_chunk=lambda: self._check_fill_target(item))
            try:
                actual = item.get_value()
            except Exception:
                try:
                    actual = item.window_text()
                except Exception:
                    actual = None
            # Compare only; never return the field's sensitive text.
            return {"verified": actual == text}
        self._ensure_not_terminal()
        if name == "windows_type":
            value = str(args.get("text", ""))
            if (not value or len(value) > 3000 or
                    any(ord(ch) < 32 for ch in value)):
                raise ValueError("入力する文章が不正です。改行や制御文字は使えません")
            # Never pass generated text to pywinauto.send_keys: ^, %, +,
            # and {ENTER} are interpreted as real keyboard shortcuts.
            expected = self._dictation_focus()  # Refuse unknown editor or selected text.
            target = expected[0] + ":" + expected[1]
            _send_literal_unicode(value, on_chunk=lambda: self._dictation_check(target))
            return "文字列を入力しました（ショートカットは実行していません）"
        if name == "windows_hotkey":
            key = str(args.get("keys", "")).lower()
            if key not in ALLOWED_KEYS:
                raise ValueError("許可されていないショートカットです")
            keyboard.send_keys(ALLOWED_KEYS[key])
            return f"ショートカット {key} を実行しました"
        if name == "windows_scroll":
            direction = args.get("direction")
            keys = {"down": "{PGDN}", "up": "{PGUP}",
                    "bottom": "^{END}", "top": "^{HOME}"}
            if direction not in keys:
                raise ValueError("スクロール方向が不正です")
            keyboard.send_keys(keys[direction])
            return "スクロールしました"
        raise ValueError("未対応のWindows操作です")


def user_roots() -> list[Path]:
    home = Path.home()
    folders = [home / "Desktop", home / "Documents", home / "Downloads"]
    for drive in home.glob("OneDrive*"):
        if drive.is_dir():
            folders.extend([drive / "Desktop", drive / "Documents", drive / "デスクトップ", drive / "ドキュメント"])
    return list(dict.fromkeys(p for p in folders if p.is_dir()))


def _within_user_roots(path: Path) -> bool:
    resolved = path.resolve()
    for root in user_roots():
        try:
            resolved.relative_to(root.resolve())
            return True
        except ValueError:
            pass
    return False


def find_files(query: str, cancelled=None) -> list[str]:
    query = query.strip().lower()
    if len(query) < 2 or len(query) > 100:
        raise ValueError("検索語を2〜100文字で指定してください")
    found, seen = [], 0
    deadline = time.monotonic() + 4.0
    for root in user_roots():
        for base, dirs, filenames in os.walk(root, followlinks=False):
            if cancelled is not None and cancelled():
                raise InterruptedError("ファイル検索を中止しました")
            dirs[:] = [d for d in dirs if not (Path(base) / d).is_symlink() and not d.startswith(".")]
            for name in filenames:
                seen += 1
                if seen % 32 == 0 and cancelled is not None and cancelled():
                    raise InterruptedError("ファイル検索を中止しました")
                path = Path(base) / name
                if query in name.lower() and path.suffix.lower() in SAFE_EXTENSIONS:
                    found.append(str(path))
                    if len(found) >= 20:
                        return found
                if seen > 10000 or time.monotonic() > deadline:
                    return found
    return found


def open_file(path: str) -> str:
    file = Path(path)
    if not file.is_file() or not _within_user_roots(file):
        raise ValueError("自分のデスクトップ・ドキュメント・ダウンロード内のファイルだけ開けます")
    if file.suffix.lower() not in SAFE_EXTENSIONS:
        raise ValueError("その形式は安全のため開けません")
    os.startfile(str(file))
    return f"{file.name} を開きました"


def show_folder(folder: str) -> str:
    if folder not in {"desktop", "documents", "downloads"}:
        raise ValueError("未対応のフォルダです")
    keywords = {
        "desktop": {"desktop", "デスクトップ"},
        "documents": {"documents", "ドキュメント"},
        "downloads": {"downloads", "ダウンロード"},
    }[folder]
    target = next((p for p in user_roots() if p.name.lower() in keywords), None)
    if target is None:
        raise FileNotFoundError("フォルダが見つかりません")
    os.startfile(str(target))
    return f"{target.name} を表示しました"


def _validate_file_mutation(path: str) -> Path:
    source = Path(path)
    if source.is_symlink() or not source.is_file() or not _within_user_roots(source):
        raise ValueError("自分のユーザーフォルダ内の通常のファイルだけ操作できます")
    return source


def move_file(path: str, folder: str) -> str:
    source = _validate_file_mutation(path)
    if folder not in {"desktop", "documents", "downloads"}:
        raise ValueError("移動先はデスクトップ・ドキュメント・ダウンロードのみ")
    names = {"desktop": {"desktop", "デスクトップ"},
             "documents": {"documents", "ドキュメント"},
             "downloads": {"downloads", "ダウンロード"}}[folder]
    target = next((p for p in user_roots() if p.name.lower() in names), None)
    if target is None:
        raise ValueError("移動先フォルダが見つかりません")
    destination = target / source.name
    if destination.exists() or destination.is_symlink():
        raise FileExistsError("同じ名前のファイルがあるため、上書きせず停止しました")
    source.rename(destination)  # Fails rather than silently replacing across disks.
    return f"{source.name} を {target.name} に移動しました"


def rename_file(path: str, new_name: str) -> str:
    source = _validate_file_mutation(path)
    if (not new_name or len(new_name) > 160 or new_name in {'.','..'} or
            any(c in new_name for c in '\\/:*?"<>|') or new_name.endswith((' ', '.'))):
        raise ValueError("ファイル名が不正です")
    if Path(new_name).suffix.lower() != source.suffix.lower():
        raise ValueError("ファイル形式（拡張子）は変更できません")
    dest = source.with_name(new_name)
    if dest.exists() or dest.is_symlink():
        raise FileExistsError("同じ名前のファイルがあるため上書きしません")
    source.rename(dest)
    return f"{source.name} を {dest.name} に変更しました"


# A dedicated literal-text path: pywinauto.send_keys uses ^/+/%%/{...} as
# shortcuts. Dictation must never interpret words like ^s or {ENTER} as keys.
def _send_literal_unicode(text: str, on_chunk=None) -> None:
    if os.name != "nt":
        raise RuntimeError("Windows専用の入力機能です")
    user32 = ctypes.windll.user32
    ULONG_PTR = ctypes.c_size_t

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                    ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                    ("dwExtraInfo", ULONG_PTR)]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                    ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                    ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                    ("wParamH", wintypes.WORD)]

    class INPUT_UNION(ctypes.Union):
        _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]

    class INPUT(ctypes.Structure):
        _fields_ = [("type", wintypes.DWORD), ("data", INPUT_UNION)]

    user32.SendInput.argtypes = (wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int)
    user32.SendInput.restype = wintypes.UINT

    # Don't use simulated clipboard paste: it could overwrite the user's
    # clipboard or leak confidential content into another application.
    encoded = text.encode("utf-16-le")
    units = [int.from_bytes(encoded[i:i+2], "little")
             for i in range(0, len(encoded), 2)]
    for offset in range(0, len(units), 64):
        if on_chunk is not None:
            on_chunk()
        chunk = units[offset:offset + 64]
        events = []
        for code in chunk:
            # VK_RETURN only for explicitly allowed Document editors.
            # For all other characters use KEYEVENTF_UNICODE (literal WM_CHAR).
            is_enter = code == 10
            for released in (False, True):
                flags = (0 if is_enter else 0x0004) | (0x0002 if released else 0)
                events.append(INPUT(1, INPUT_UNION(ki=KEYBDINPUT(13 if is_enter else 0,
                                                               0 if is_enter else code, flags, 0, 0))))
        arr = (INPUT * len(events))(*events)
        if user32.SendInput(len(events), arr, ctypes.sizeof(INPUT)) != len(events):
            raise RuntimeError("Windowsが文字入力を受け付けませんでした")