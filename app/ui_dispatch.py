"""Thread-safe handoff of UI changes to the sole Tk event-loop thread.

Workers and the Windows hotkey listener only enqueue callbacks; they never call
Tk methods (including ``root.after``) directly. A bounded drain keeps mouse and
keyboard input responsive even when many log messages arrive at once.
"""
from __future__ import annotations

from queue import Empty, SimpleQueue


class UIDispatcher:
    def __init__(self, root, interval_ms: int = 35, batch_size: int = 64):
        self.root = root
        self.interval_ms = interval_ms
        self.batch_size = batch_size
        self._pending = SimpleQueue()
        self._closed = False
        root.after(interval_ms, self._drain)

    def post(self, callback) -> None:
        if not self._closed:
            self._pending.put(callback)

    def _drain(self) -> None:
        if self._closed:
            return
        try:
            for _ in range(self.batch_size):
                try:
                    callback = self._pending.get_nowait()
                except Empty:
                    break
                try:
                    callback()
                except Exception as exc:
                    reporter = getattr(self.root, "report_callback_exception", None)
                    if reporter:
                        reporter(type(exc), exc, exc.__traceback__)
                    else:
                        raise
        finally:
            if not self._closed:
                self.root.after(self.interval_ms, self._drain)

    def shutdown(self) -> None:
        self._closed = True
        while True:
            try:
                self._pending.get_nowait()
            except Empty:
                break