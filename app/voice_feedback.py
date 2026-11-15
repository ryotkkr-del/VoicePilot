"""Quiet, optional, local Windows speech feedback.

No microphone, inference model, network request or polling loop is started here.
The worker is created only for the first spoken result and waits on a
condition when idle. A new result replaces any queued old result.

A completion report is generated from verified *execution status*, never from
an LLM's untrusted assertion. The spoken vocabulary is intentionally small:
we do not read user commands, private file names, clipboard text or webpages.
"""
from __future__ import annotations

import sys
import threading
from collections.abc import Callable


_VERIFIED_ACTION_MESSAGES = {
    "windows_launch": "アプリを開きました。",
    "windows_focus": "ウィンドウを切り替えました。",
    "windows_arrange": "ウィンドウを配置しました。",
    "files_find": "ファイルが見つかりました。",
    "files_open": "ファイルを開きました。",
    "files_move": "ファイルを移動しました。",
    "files_rename": "ファイル名を変更しました。",
    "browser_open": "ページを開きました。",
    "browser_new_tab": "タブを開きました。",
    "browser_search": "検索しました。",
    "browser_fill": "入力しました。",
    "windows_fill": "入力しました。",
    "windows_type": "入力しました。",
}


def result_announcement(status: str, verified_action: str | None = None) -> str | None:
    """Short Japanese speech based on the authoritative execution outcome."""
    if status == "completed" and verified_action:
        return _VERIFIED_ACTION_MESSAGES.get(verified_action, "操作が完了しました。")
    return {
        "completed": "操作が完了しました。",
        "handoff": "ChatGPTに用意しました。送信は画面で確認してください。",
        "answered": "画面の説明を表示しました。",
        "handoff_clipboard": "ChatGPTを開きました。質問を貼り付けてください。",
        "unverified": "操作しましたが、結果は確認できませんでした。",
        "incomplete": "作業はまだ完了していません。",
        "error": "操作できませんでした。",
        "no_speech": "声を聞き取れませんでした。",
        # Stopping is already user-initiated; speaking after Stop is irritating.
        "interrupted": None,
    }.get(status)


class _SapiVoice:
    """SAPI COM object; constructed, used and released on ONE worker thread."""

    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Windows 11でのみ音声読み上げを利用できます")
        try:
            import pythoncom
            from win32com.client import Dispatch
        except ImportError as exc:
            raise RuntimeError("Windows音声エンジンがありません。pywin32をインストールしてください") from exc
        pythoncom.CoInitialize()
        self._com = pythoncom
        try:
            voice = Dispatch("SAPI.SpVoice")
            japanese_voices = voice.GetVoices("Language=411")
            if japanese_voices.Count == 0:
                raise RuntimeError(
                    "日本語の読み上げ音声が見つかりません。Windowsの日本語音声を追加してください"
                )
            voice.Voice = japanese_voices.Item(0)
            voice.Volume = 80
            self._voice = voice
        except Exception:
            pythoncom.CoUninitialize()
            raise

    def speak(self, text: str, is_cancelled: Callable[[], bool]) -> None:
        if is_cancelled():
            return
        # Asynchronous SAPI speech, so cancellation can be checked repeatedly.
        # SPF_ASYNC=1; SPF_PURGEBEFORESPEAK=2.
        self._voice.Speak(text, 1)
        while not self._voice.WaitUntilDone(100):
            if is_cancelled():
                self._voice.Speak("", 3)
                return
        if is_cancelled():
            self._voice.Speak("", 3)

    def close(self) -> None:
        try:
            self._voice.Speak("", 3)
            self._voice = None
        finally:
            self._com.CoUninitialize()


class SpeechFeedback:
    """Non-blocking, latest-wins speaker with immediate cancellation requests.

    stop()/close() only update state; the COM worker purges speech promptly on
    its own thread. This avoids calling COM from Tkinter or the microphone.
    """

    def __init__(self, on_error: Callable[[str], None] | None = None,
                 backend_factory: Callable[[], object] | None = None,
                 idle_seconds: float = 45.0):
        self._on_error = on_error or (lambda _message: None)
        self._backend_factory = backend_factory or _SapiVoice
        self._idle_seconds = max(1.0, float(idle_seconds))
        self._condition = threading.Condition()
        self._pending: tuple[int, str] | None = None
        self._generation = 0
        self._closed = False
        self._disabled = False
        self._speaking = False
        self._worker_thread: threading.Thread | None = None

    def say(self, text: str) -> bool:
        if not text or len(text) > 100:
            return False
        with self._condition:
            if self._closed or self._disabled:
                return False
            self._generation += 1
            self._pending = (self._generation, text)
            if self._worker_thread is None:
                self._worker_thread = threading.Thread(target=self._run, daemon=True,
                                                       name="VoicePilot-SAPI")
                self._worker_thread.start()
            self._condition.notify()
            return True

    def is_active(self) -> bool:
        """True until queued and currently spoken TTS have both finished.

        The next hands-free microphone capture MUST NOT begin during SAPI speech.
        """
        with self._condition:
            return self._pending is not None or self._speaking

    def stop(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending = None
            self._condition.notify_all()

    def close(self) -> None:
        with self._condition:
            self._generation += 1
            self._pending = None
            self._closed = True
            self._condition.notify_all()
        # No blocking join on the UI thread. The SAPI worker checks the stop
        # signal every 100 ms and releases COM before exiting.

    def _outdated(self, generation: int) -> bool:
        with self._condition:
            return self._closed or generation != self._generation

    def _run(self) -> None:
        backend = None
        try:
            backend = self._backend_factory()
            while True:
                with self._condition:
                    while self._pending is None and not self._closed:
                        # Free Windows voice resources after inactivity.  No
                        # polling happens while the app is idle.
                        if not self._condition.wait(timeout=self._idle_seconds):
                            if self._pending is None and not self._closed:
                                self._worker_thread = None
                                return
                    if self._closed:
                        return
                    generation, text = self._pending
                    self._pending = None
                    # Transfer pending -> speaking atomically: a microphone
                    # rearm check must never see a false quiet gap.
                    self._speaking = True
                try:
                    if not self._outdated(generation):
                        backend.speak(text, lambda: self._outdated(generation))
                finally:
                    with self._condition:
                        self._speaking = False
                        self._condition.notify_all()
        except Exception as exc:
            with self._condition:
                self._disabled = True  # Don't spam the user for every result.
                self._pending = None
            self._on_error(f"音声読み上げを利用できません：{exc}")
        finally:
            if backend is not None:
                try:
                    backend.close()
                except Exception:
                    pass