"""Opt-in speech barge-in during an active hands-free task.

The short-utterance monitor is independent from the interactive microphone and
never calls the planner, executes a user command, saves audio or sends audio to
an API. Only exact local ASR control phrases may affect the action gate.
"""
from __future__ import annotations

import re
import threading
import time
from collections.abc import Callable

from .audio import Microphone

# Whole utterance, no substring matching. Text dictated in another context is
# never interpreted by this module as a control command.
_PAUSE = frozenset({"待って", "ちょっと待って", "少し待って", "待ってください", "ちょっと待ってください", "少し待ってください", "一旦待って", "一時停止", "一時停止して"})
_RESUME = frozenset({"続けて", "再開して", "再開", "続行して", "作業を続けて", "続けてください", "そのまま続けて"})
_CANCEL = frozenset({"やめて", "止めて", "中止して", "ストップ", "キャンセルして", "作業をやめて", "やめてください", "止めてください"})
_END = frozenset({"ハンズフリー終了", "ハンズフリーを終了して", "ハンズフリーを止めて"})
_REVISION_PREFIXES = ("やっぱり", "代わりに", "変更して", "方針を変えて", "次は")


def interrupt_intent(utterance: str, *, paused: bool = False) -> str | None:
    text = re.sub(r"[\s、。！!？?．.]+", "", utterance).strip()
    if text in _END:
        return "end"
    if text in _CANCEL:
        return "cancel"
    if text in _PAUSE and not paused:
        return "pause"
    if text in _RESUME and paused:
        return "resume"
    return None


def duplex_revision(utterance: str) -> str | None:
    """Extract an explicit replacement request from a barge-in utterance.

    A free-form background conversation must never become a desktop command.
    Requiring a complete, user-spoken revision prefix makes the change mode
    intentional while leaving ordinary pause/cancel phrases to
    :func:`interrupt_intent`.
    """
    text = re.sub(r"[\s、。！!？?．.]+", "", utterance).strip()
    for prefix in _REVISION_PREFIXES:
        if text.startswith(prefix):
            replacement = text[len(prefix):].strip()
            if 2 <= len(replacement) <= 240:
                if replacement in _PAUSE | _RESUME | _CANCEL | _END:
                    return None
                return replacement
    return None


class ActionGate:
    """Pause at *action boundaries*, never in the middle of a side effect.

    A pause expires after a bounded interval and cancels the task. This keeps
    an ASR/PortAudio failure from leaving a background worker stuck forever.
    """
    def __init__(self, timeout: float = 120, clock=time.monotonic):
        self._condition = threading.Condition()
        self._paused = False
        self._started = 0.0
        self._paused_total = 0.0
        self._pause_generation = 0
        self._timeout = max(3.0, float(timeout))
        self._clock = clock

    @property
    def paused(self) -> bool:
        with self._condition:
            return self._paused

    @property
    def pause_generation(self) -> int:
        with self._condition:
            return self._pause_generation

    def paused_seconds(self) -> float:
        with self._condition:
            current = self._clock() - self._started if self._paused else 0.0
            return self._paused_total + max(0.0, current)

    def pause(self) -> None:
        with self._condition:
            if not self._paused:
                self._paused = True
                self._pause_generation += 1
                self._started = self._clock()
                self._condition.notify_all()

    def resume(self) -> None:
        with self._condition:
            if self._paused:
                self._paused_total += max(0.0, self._clock() - self._started)
            self._paused = False
            self._condition.notify_all()

    def checkpoint(self, cancelled: threading.Event) -> bool:
        """Return False if cancelled/expired. Never clear cancellation here."""
        with self._condition:
            while self._paused and not cancelled.is_set():
                left = self._timeout - (self._clock() - self._started)
                if left <= 0:
                    cancelled.set()
                    self._paused_total += max(0.0, self._clock() - self._started)
                    self._paused = False
                    return False
                self._condition.wait(min(0.1, left))
            return not cancelled.is_set()


class VoiceInterruptMonitor:
    """One bounded microphone worker for the duration of an executing task.

    Audio is kept only in RAM. Very short or long/noisy utterances are ignored
    before invoking the *existing* speech recognizer. The worker is inactive
    whenever the hands-free task is not running.
    """
    def __init__(self, recognize: Callable, on_intent: Callable[[str], None],
                 gate: ActionGate, *, mic_factory=Microphone,
                 on_error: Callable[[str], None] | None = None,
                 on_revision: Callable[[str], None] | None = None,
                 silence: float = 0.8, max_utterance: float = 3.5):
        self._recognize = recognize
        self._on_intent = on_intent
        self._gate = gate
        self._mic_factory = mic_factory
        self._on_error = on_error or (lambda _: None)
        self._on_revision = on_revision
        self._silence = float(silence)
        self._max_utterance = float(max_utterance)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None:
            raise RuntimeError("割り込みマイクはすでに起動しています")
        self._thread = threading.Thread(target=self._run, daemon=True,
                                        name="VoicePilot-interrupt-mic")
        self._thread.start()

    def request_stop(self) -> None:
        """Non-blocking stop request suitable for Tk callbacks."""
        self._stop.set()

    def close(self) -> None:
        self.request_stop()
        if self._thread is not None and threading.current_thread() is not self._thread:
            self._thread.join(timeout=0.6)  # never block task teardown on ASR

    def _run(self) -> None:
        mic = None
        try:
            if self._stop.is_set():
                return
            mic = self._mic_factory()
            mic.start(continuous=True)
            while not self._stop.wait(0.10):
                activity = mic.activity()
                if not activity.get("heard_voice"):
                    continue
                duration = activity.get("speech_elapsed", 0)
                if activity.get("silence", 0) < self._silence and duration < self._max_utterance:
                    continue
                audio = mic.stop()
                if self._stop.is_set():
                    break
                # Large snippets such as a playing video are not commands.
                if 0.2 <= duration <= self._max_utterance and len(audio) <= int(16000 * 6):
                    try:
                        utterance = self._recognize(audio)
                        intent = interrupt_intent(utterance, paused=self._gate.paused)
                        if intent and not self._stop.is_set():
                            self._on_intent(intent)
                            if intent in {"cancel", "end"}:
                                break
                        revision = duplex_revision(utterance)
                        if revision and self._on_revision is not None and not self._stop.is_set():
                            # A replacement always stops this monitor. The next
                            # task owns a fresh monitor, avoiding two microphone
                            # streams and stale speech from the previous task.
                            self._on_revision(revision)
                            break
                    except Exception as exc:
                        if not self._stop.is_set():
                            self._on_error("停止音声を認識できませんでした: " + str(exc)[:100])
                if self._stop.is_set():
                    break
                mic.start(continuous=True)
        except Exception as exc:
            if not self._stop.is_set():
                self._on_error("割り込みマイクを開始できませんでした: " + str(exc)[:100])
        finally:
            if mic is not None and getattr(mic, "_stream", None) is not None:
                try:
                    mic.stop()
                except Exception:
                    pass
