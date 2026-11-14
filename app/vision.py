"""On-demand screenshots of the monitor containing the active window.

Screenshots live only in RAM, are resized for fast vision inference, and are
never uploaded by this module.  Window/monitor selection stays within Windows.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from dataclasses import dataclass
from io import BytesIO
import time


@dataclass(frozen=True)
class ScreenFrame:
    jpeg: bytes
    width: int
    height: int
    active_hwnd: int
    captured_at: float
    left: int = 0
    top: int = 0

    def pixel(self, x: str, y: str) -> tuple[int, int]:
        """Convert 0..1000 image-relative coordinates into desktop pixels."""
        try:
            px, py = int(x), int(y)
        except (ValueError, TypeError) as exc:
            raise ValueError('座標には0〜1000の整数を指定してください') from exc
        if not (0 <= px <= 1000 and 0 <= py <= 1000):
            raise ValueError('スクリーン座標は0〜1000の範囲です')
        return (self.left + min(self.width - 1, round(px * (self.width - 1) / 1000)),
                self.top + min(self.height - 1, round(py * (self.height - 1) / 1000)))


class MonitorInfo(ctypes.Structure):
    _fields_ = [('cbSize', wintypes.DWORD), ('rcMonitor', wintypes.RECT),
                ('rcWork', wintypes.RECT), ('dwFlags', wintypes.DWORD)]


def monitor_bbox(hwnd: int) -> tuple[int, int, int, int]:
    user32 = ctypes.windll.user32
    user32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
    user32.MonitorFromWindow.restype = wintypes.HANDLE
    user32.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
    monitor = user32.MonitorFromWindow(hwnd, 2)  # MONITOR_DEFAULTTONEAREST
    info = MonitorInfo()
    info.cbSize = ctypes.sizeof(info)
    if not monitor or not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
        raise RuntimeError('アクティブなモニターを取得できません')
    rect = info.rcMonitor
    return rect.left, rect.top, rect.right, rect.bottom


def capture_screen(active_hwnd: int, longest_side: int = 1440) -> ScreenFrame:
    from PIL import ImageGrab

    left, top, right, bottom = monitor_bbox(active_hwnd)
    image = ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True).convert('RGB')
    width, height = image.size
    if not width or not height:
        raise RuntimeError('画面キャプチャが空でした')
    if max(width, height) > longest_side:
        image.thumbnail((longest_side, longest_side))
    buffer = BytesIO()
    # JPEG is a one-off fallback; avoid CPU-heavy optimized encoding.
    image.save(buffer, format='JPEG', quality=72, optimize=False)
    return ScreenFrame(buffer.getvalue(), width, height, active_hwnd,
                       time.monotonic(), left, top)