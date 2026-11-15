"""Opt-in, local-only performance diagnostics for one VoicePilot operation.

Records strictly numerical timings and current-process resource samples. No
transcripts, instructions, URLs, file paths, screenshots or credentials are
stored or transmitted. There is no background resource polling.
"""
from __future__ import annotations

from contextlib import contextmanager
import ctypes
import os
import time
from collections.abc import Callable


def process_working_set_bytes() -> int | None:
    """Return this process's *current* working set on Windows, never a peak."""
    if os.name != "nt":
        return None
    try:
        from ctypes import wintypes

        class ProcessMemoryCounters(ctypes.Structure):
            _fields_ = [
                ("cb", wintypes.DWORD),
                ("PageFaultCount", wintypes.DWORD),
                ("PeakWorkingSetSize", ctypes.c_size_t),
                ("WorkingSetSize", ctypes.c_size_t),
                ("QuotaPeakPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPagedPoolUsage", ctypes.c_size_t),
                ("QuotaPeakNonPagedPoolUsage", ctypes.c_size_t),
                ("QuotaNonPagedPoolUsage", ctypes.c_size_t),
                ("PagefileUsage", ctypes.c_size_t),
                ("PeakPagefileUsage", ctypes.c_size_t),
            ]

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        psapi = ctypes.WinDLL("psapi", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        psapi.GetProcessMemoryInfo.argtypes = [
            wintypes.HANDLE, ctypes.POINTER(ProcessMemoryCounters), wintypes.DWORD
        ]
        psapi.GetProcessMemoryInfo.restype = wintypes.BOOL
        counters = ProcessMemoryCounters()
        counters.cb = ctypes.sizeof(counters)
        if not psapi.GetProcessMemoryInfo(
            kernel32.GetCurrentProcess(), ctypes.byref(counters), counters.cb
        ):
            return None
        return int(counters.WorkingSetSize)
    except (AttributeError, OSError, TypeError, ValueError):
        # A failed optional metric must not block speech or PC control.
        return None


class OperationProfiler:
    """One operation; created only after the settings opt-in is checked."""

    _PHASES = {"asr": "音声認識", "execute": "操作処理"}

    def __init__(
        self, *,
        started_at: float | None = None,
        clock: Callable[[], float] = time.perf_counter,
        cpu_clock: Callable[[], float] = time.process_time,
        memory_reader: Callable[[], int | None] = process_working_set_bytes,
    ):
        self._clock = clock
        self._cpu_clock = cpu_clock
        self._memory_reader = memory_reader
        self._started_at = clock() if started_at is None else started_at
        self._cpu_start = cpu_clock()
        self._memory_start = memory_reader()
        self._durations = {"asr": 0.0, "execute": 0.0}

    @contextmanager
    def phase(self, name: str):
        if name not in self._PHASES:
            raise ValueError("unknown performance phase")
        started = self._clock()
        try:
            yield
        finally:
            self._durations[name] += max(0.0, self._clock() - started)

    def summary(self, *, voice: bool, status: str) -> str:
        """Format fixed vocabulary + numbers; no arbitrary content in output."""
        completed_at = self._clock()
        cpu_at_end = self._cpu_clock()
        memory_end = self._memory_reader()
        total = max(0.0, completed_at - self._started_at)
        cpu = max(0.0, cpu_at_end - self._cpu_start)
        kind = "音声" if voice else "テキスト"
        # Known status names only. Never interpolate a model-generated string.
        outcome = status if status in {
            "completed", "unverified", "interrupted", "incomplete", "error",
            "no_speech", "handoff", "handoff_clipboard", "answered",
        } else "unknown"
        parts = [f"性能（{kind} / {outcome}）：合計 {total:.2f}秒"]
        if voice:
            parts.append(f"認識 {self._durations['asr']:.2f}秒")
        parts.append(f"操作処理 {self._durations['execute']:.2f}秒")
        parts.append(f"CPU処理時間 {cpu:.2f}秒")
        if self._memory_start is not None and memory_end is not None:
            before = self._memory_start / (1024 * 1024)
            after = memory_end / (1024 * 1024)
            parts.append(f"VoicePilot使用メモリ {before:.0f}→{after:.0f}MB（前後のみ）")
        else:
            parts.append("メモリ計測不可")
        return "、".join(parts)
