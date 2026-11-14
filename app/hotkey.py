"""Two OS-level global hotkeys; no continuous key logging or hook."""
from __future__ import annotations

import ctypes
import threading


def register_windows_hotkey(toggle_callback, cancel_callback, on_error=None):
    def listen():
        from ctypes import wintypes
        user32 = ctypes.windll.user32
        control_alt = 0x0002 | 0x0001 | 0x4000  # ctrl + alt + no repeat
        registered = []
        for identifier, virtual_key in ((8701, 0x20), (8702, 0x1B)):
            if user32.RegisterHotKey(None, identifier, control_alt, virtual_key):
                registered.append(identifier)
            elif on_error is not None:
                on_error("ショートカットが他のアプリと競合しています。VoicePilotの『話す』ボタンから操作できます。" if identifier == 8701
                         else "停止ショートカットが使用できません。VoicePilotの停止ボタンをご利用ください。")
        if not registered:
            return
        try:
            message = wintypes.MSG()
            while user32.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                if message.message == 0x0312:
                    if message.wParam == 8701:
                        toggle_callback()
                    elif message.wParam == 8702:
                        cancel_callback()
        finally:
            for identifier in registered:
                user32.UnregisterHotKey(None, identifier)

    thread = threading.Thread(target=listen, daemon=True, name='voicepilot-hotkeys')
    thread.start()
    return thread