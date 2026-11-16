"""Volatile, time-bounded desktop-operation authorization for VoicePilot.

Startup is always LOCKED. Nothing in config, files or the browser can arm it.
Only a deliberate action in the local GUI is allowed to call arm(). Other
components may check or revoke the permit but cannot mint authority.
"""
from __future__ import annotations

import threading
import time
from collections.abc import Callable


class DesktopControlLocked(PermissionError):
    """No desktop/browser/file effect may be dispatched in locked mode."""


class DesktopControlPermit:
    """Per-process, revocable desktop-control permit with a monotonic expiry."""

    def __init__(self, *, ttl_seconds: float = 300,
                 clock: Callable[[], float] = time.monotonic) -> None:
        if not 30 <= ttl_seconds <= 3600:
            raise ValueError("PC操作の許可時間が不正です")
        self._mutex = threading.Lock()
        self._expires_at = 0.0
        self._clock = clock
        self._ttl = float(ttl_seconds)

    def arm(self) -> None:
        """MUST be called only by a deliberate, confirmed local GUI click."""
        with self._mutex:
            self._expires_at = self._clock() + self._ttl

    def revoke(self) -> None:
        with self._mutex:
            self._expires_at = 0.0

    def allowed(self) -> bool:
        with self._mutex:
            return self._expires_at > self._clock()

    def require(self) -> None:
        """Call at the actual effect dispatch boundary, never only at planning."""
        if not self.allowed():
            raise DesktopControlLocked(
                "PC操作はロック中です。GUIでその都度、操作を明示的に許可してください"
            )
