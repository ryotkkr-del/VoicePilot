"""Constrained Windows UI Automation and user-visible file actions.

No shell, arbitrary code execution, deletion, or programmatic writes.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from collections import deque
import os
from pathlib import Path
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

    def inspect_point(self, x: int, y: int) -> dict:
        """Identify the real UIA control under a visual click when possible."""
        self._ensure_not_terminal()
        try:
            from pywinauto import Desktop
            item = Desktop(backend='uia').from_point(x, y)
            return {
                'name': (item.window_text() or '')[:100],
                'type': (item.element_info.control_type or '')[:40],
            }
        except Exception:
            return {}

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

    def _ensure_not_terminal(self):
        if UNSAFE_WINDOW.search(self.active_window_title()):
            raise ValueError("ターミナル・管理者権限画面への操作は許可していません")

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
                os.startfile(str(shortcut))
                return f'{shortcut.stem} を起動しました' 
            if app == "settings":
                os.startfile("ms-settings:")
            elif app == "chrome":
                try:
                    subprocess.Popen(APP_COMMANDS[app], shell=False)
                except OSError:
                    # Do not open Edge / a different default browser and claim
                    # Chrome launched.  Use the installed shortcut, or report it.
                    try:
                        shortcut = app_shortcut("Google Chrome")
                    except (FileNotFoundError, ValueError) as exc:
                        raise FileNotFoundError("Google Chromeが見つかりません。インストール状況を確認してください") from exc
                    os.startfile(str(shortcut))
            else:
                if app == "vscode":
                    path = (Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Microsoft VS Code" / "Code.exe")
                    executable = str(path) if path.is_file() else shutil.which("Code.exe")
                    if executable is None:
                        raise FileNotFoundError("VS Codeが見つかりません。先にインストールしてください")
                    subprocess.Popen([executable], shell=False)
                else:
                    subprocess.Popen(APP_COMMANDS[app], shell=False)
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
            if not text or len(text) > 3000:
                raise ValueError("入力する文字数が不正です")
            if ((item.element_info.control_type or "") != "Edit" or
                    SENSITIVE_CONTROL.search(item.window_text()) or
                    _is_password_control(item)):
                raise ValueError("この欄への文字入力は許可していません")
            try:
                item.set_edit_text(text)
            except Exception:
                item.set_focus()
                keyboard.send_keys("^a")
                keyboard.send_keys(text, with_spaces=True, vk_packet=True, pause=0.005)
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
            if not value or len(value) > 3000:
                raise ValueError("入力する文章が不正です")
            keyboard.send_keys(value, with_spaces=True, vk_packet=True, pause=0.005)
            return "文章を入力しました"
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