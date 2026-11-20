"""Opt-in, bounded hands-free session state (no audio, Tk or network imports).

This session listens sequentially for normal requests. During an executing task,
an independent bounded barge-in monitor can pause, cancel, or explicitly replace
the request. It is deliberately not an always-on wake-word listener.
"""
from __future__ import annotations

import re
import threading
import time

IDLE_TIMEOUT_SECONDS = 180
MAX_CONSECUTIVE_ERRORS = 3
_END_PHRASES = frozenset({
    "ハンズフリー終了", "ハンズフリーを終了", "ハンズフリーを終了して",
    "ハンズフリーを止めて", "ハンズフリー停止",
    "連続対話終了", "連続対話を終了", "連続対話を終了して",
    "聞き取りを終了して", "聞き取りを止めて",
})


def handsfree_control(text: str) -> str | None:
    """Only unambiguous entire utterances control the session.

    Never interpret an embedded phrase inside dictated text or an arbitrary
    document as a command. '停止' alone is intentionally not sufficient.
    """
    normalized = re.sub(r"[\s、。！？!?]+", "", text).strip()
    return "stop" if normalized in _END_PHRASES else None


class HandsFreeSession:
    def __init__(self, idle_timeout: float = IDLE_TIMEOUT_SECONDS,
                 clock=time.monotonic):
        self.idle_timeout = max(30.0, float(idle_timeout))
        self.clock = clock
        self._lock = threading.Lock()
        self._enabled = False
        self._generation = 0
        self._last_activity = 0.0
        self._errors = 0

    @property
    def enabled(self) -> bool:
        with self._lock:
            return self._enabled

    @property
    def generation(self) -> int:
        with self._lock:
            return self._generation

    def start(self) -> int:
        with self._lock:
            self._enabled = True
            self._generation += 1
            self._last_activity = self.clock()
            self._errors = 0
            return self._generation

    def stop(self) -> None:
        with self._lock:
            self._enabled = False
            self._generation += 1  # invalidate scheduled microphone rearming
            self._errors = 0

    def is_current(self, generation: int) -> bool:
        with self._lock:
            return self._enabled and generation == self._generation

    def activity(self) -> None:
        with self._lock:
            if self._enabled:
                self._last_activity = self.clock()

    def success(self) -> None:
        with self._lock:
            self._errors = 0

    def expired(self, *, speaking: bool = False) -> bool:
        with self._lock:
            return (self._enabled and not speaking and
                    self.clock() - self._last_activity >= self.idle_timeout)

    def failure(self) -> bool:
        """Return true when a failing session should stop instead of looping."""
        with self._lock:
            if not self._enabled:
                return False
            self._errors += 1
            return self._errors >= MAX_CONSECUTIVE_ERRORS
