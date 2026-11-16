"""Opt-in, LOCAL microphone/transcription trial. Not a PC control agent.

Does not import the planner, BrowserBridge, WindowsController, VoiceAgent,
update bootstrap, cloud integrations, or task execution code. It never saves
audio or transcripts, operates the OS, opens browsers or auto-starts recording.
An initial Whisper model download is forbidden unless the user explicitly
allows it in this window. No microphone samples are sent to the cloud.
"""
from __future__ import annotations

import os
import queue
import sys
import threading
import time
import tkinter as tk

from .command_preview import preview_command

TRIAL_RECORD_MAX_SECONDS = 8.0
TRIAL_MODEL = "small"


def trial_platform_ok(*, platform: str | None = None,
                      elevated: bool | None = None,
                      build: int | None = None) -> bool:
    """Keep first-run trial off admin and non-Windows platforms."""
    name = platform if platform is not None else os.name
    if name != "nt":
        return False
    if elevated is None:
        try:
            import ctypes
            elevated = bool(ctypes.windll.shell32.IsUserAnAdmin())
        except (OSError, AttributeError, ValueError):
            return False
    if build is None:
        try:
            build = int(sys.getwindowsversion().build)
        except (OSError, AttributeError, ValueError):
            return False
    return elevated is False and type(build) is int and build >= 22000


class LocalTrial:
    """A GUI containing only user-triggered capture and local transcription."""

    def __init__(self, root: tk.Tk):
        self.root = root
        self.root.title("VoicePilot | 安全なマイク試用")
        self.root.geometry("560x480")
        self.root.minsize(440, 390)
        self.root.protocol("WM_DELETE_WINDOW", self.close)

        self.mic = None
        self.recording = False
        self.working = False
        self.closed = False
        self.generation = 0
        self.started = 0.0
        self.recognizer = None
        # The worker MUST NOT call Tk, even via root.after(). A SimpleQueue
        # cannot deadlock a finished worker if the UI briefly stops polling;
        # the GUI's busy guard still permits one inference at a time.
        self.worker_results = queue.SimpleQueue()
        self.allow_download = tk.BooleanVar(value=False)
        self.status = tk.StringVar(value="マイクは停止しています")
        self.preview_state = tk.StringVar(value="指示候補：未確認（PC操作なし）")
        self.platform_safe = trial_platform_ok()

        panel = tk.Frame(root, padx=20, pady=18)
        panel.pack(fill="both", expand=True)
        tk.Label(panel, text="VoicePilot — 音声認識だけ試す",
                 font=("Yu Gothic UI", 17, "bold")).pack(anchor="w")
        tk.Label(panel,
                 text="この試用モードはPCやChromeを操作しません。"
                      "録音・音声認識の結果をファイルに保存しません。",
                 wraplength=510, justify="left").pack(anchor="w", pady=(10, 8))
        tk.Label(panel, text="録音はボタン操作で開始し、最大8秒で終了します。",
                 wraplength=510).pack(anchor="w")
        tk.Checkbutton(
            panel, variable=self.allow_download,
            text="モデル未取得の場合に限り、初回ダウンロードを許可する",
            anchor="w"
        ).pack(anchor="w", pady=(12, 10))

        buttons = tk.Frame(panel)
        buttons.pack(anchor="w", pady=(0, 8))
        self.record_btn = tk.Button(buttons, text="● 録音を開始", command=self.start,
                                    width=16)
        self.record_btn.pack(side="left")
        self.stop_btn = tk.Button(buttons, text="■ 停止", command=self.stop,
                                  state="disabled", width=12)
        self.stop_btn.pack(side="left", padx=(10, 0))
        tk.Label(panel, textvariable=self.status, anchor="w",
                 wraplength=510, justify="left").pack(fill="x", pady=(4, 8))
        tk.Label(panel, text="認識結果（表示だけ・PC操作には使われません）",
                 anchor="w").pack(fill="x")
        self.output = tk.Text(panel, height=4, wrap="word")
        self.output.pack(fill="both", expand=True, pady=(4, 4))
        self.output.configure(state="disabled")
        tk.Label(panel, text="音声指示のプレビュー（実行はしません）",
                 font=("Yu Gothic UI", 10, "bold"), anchor="w").pack(
                     fill="x", pady=(7, 2))
        tk.Label(panel, textvariable=self.preview_state, anchor="w",
                 wraplength=510, justify="left").pack(fill="x", pady=(0, 7))
        tk.Label(panel,
                 text="Whisperモデルの初回取得を許可した場合だけモデル配布元へ通信します。"
                      "音声データは送信しません。",
                 font=("Yu Gothic UI", 9), wraplength=510,
                 justify="left", fg="#555555").pack(anchor="w")

        if not self.platform_safe:
            self.record_btn.configure(state="disabled")
            self.status.set("Windows 11 の通常権限でのみ録音試用できます")

    def _status(self, value: str) -> None:
        if not self.closed:
            self.status.set(value)

    def _set_output(self, value: str) -> None:
        if self.closed:
            return
        self.output.configure(state="normal")
        self.output.delete("1.0", tk.END)
        self.output.insert("1.0", value[:2000])
        self.output.configure(state="disabled")

    def _set_preview(self, result: tuple[str, str]) -> None:
        if not self.closed:
            # Labels are constant phrases from a narrow allowlist; the
            # untrusted transcript is shown only in the separate text area.
            self.preview_state.set(result[1])

    def _poll_worker(self) -> None:
        """Called exclusively by the Tk main thread; never from the worker."""
        if self.closed:
            return
        try:
            generation, text, status = self.worker_results.get_nowait()
        except queue.Empty:
            if self.working:
                self.root.after(120, self._poll_worker)
            return
        # A stalled UI may have several completions waiting. Draining avoids
        # displaying an older transcript after a newer canceled generation.
        while True:
            try:
                generation, text, status = self.worker_results.get_nowait()
            except queue.Empty:
                break
        self.working = False
        if generation == self.generation:
            self._set_output(text)
            self._set_preview(preview_command(text))
            self._status(status)
        # A canceled result never reaches either text or action preview.
        self.record_btn.configure(state="normal")
        self.stop_btn.configure(state="disabled")

    def start(self) -> None:
        if self.closed or not self.platform_safe or self.working or self.recording:
            return
        # Explicitly import mic only after the user clicks.
        try:
            from .audio import Microphone
            trial_mic = Microphone()
            trial_mic.start()
        except Exception:
            self._status("マイクを起動できません。接続とWindowsのマイク許可を確認してください")
            return
        self.mic = trial_mic
        self.recording = True
        self.generation += 1
        generation = self.generation
        self.started = time.monotonic()
        self._set_output("")
        self._set_preview(("empty", "指示候補：録音中（PC操作なし）"))
        self._status("録音中：短く話してください。もう一度停止を押しても終了できます")
        self.record_btn.configure(state="disabled")
        self.stop_btn.configure(state="normal")
        self.root.after(150, lambda: self._tick(generation))

    def _tick(self, generation: int) -> None:
        if self.closed or self.generation != generation or not self.recording:
            return
        if time.monotonic() - self.started >= TRIAL_RECORD_MAX_SECONDS:
            self.stop()
        else:
            self.root.after(150, lambda: self._tick(generation))

    def stop(self) -> None:
        if self.closed:
            return
        if self.recording:
            self.recording = False
            self.stop_btn.configure(state="normal")
            self._status("ローカル音声認識中（操作は実行されません）")
            try:
                samples = self.mic.stop()
            except Exception:
                self._status("録音の終了に失敗しました")
                self.mic = None
                self.record_btn.configure(state="normal")
                self.stop_btn.configure(state="disabled")
                return
            self.mic = None
            self.working = True
            generation = self.generation
            allow_network = bool(self.allow_download.get())
            threading.Thread(target=self._transcribe,
                             args=(samples, generation, allow_network),
                             daemon=True).start()
            # Schedule the poller from Tk main thread; background threads
            # must not call root.after / configure / StringVar.set.
            self.root.after(120, self._poll_worker)
        elif self.working:
            # Native ASR cannot be interrupted safely. Invalidate its result
            # but keep the busy guard until its worker really exits so a
            # second recognition cannot run concurrently on the same model.
            self.generation += 1
            self._status("結果を破棄しました。計算が終了するまで録音を開始できません")
            self.stop_btn.configure(state="disabled")

    def _transcribe(self, samples, generation: int, allow_network: bool) -> None:
        try:
            from .audio import SpeechRecognizer
            # A prior model loaded locally is reusable. Otherwise construct
            # one with explicit local-files-only behavior when not consented.
            if self.recognizer is None:
                self.recognizer = SpeechRecognizer(
                    TRIAL_MODEL, local_files_only=not allow_network
                )
            else:
                self.recognizer.local_files_only = not allow_network
            text = self.recognizer.transcribe(samples)
            status = "ローカル音声認識が完了しました" if text else "認識できる音声がありませんでした"
        except Exception:
            text = ""
            status = ("音声認識に失敗しました。モデル未取得なら許可して再試行してください"
                      if not allow_network else
                      "音声認識に失敗しました。モデル・マイク設定を確認してください")
        finally:
            del samples
        # Never call Tk from the worker thread (including root.after).
        # The UI poller handles generation mismatch, cancellation and display.
        if not self.closed:
            self.worker_results.put((generation, text, status))

    def close(self) -> None:
        if self.closed:
            return
        self.generation += 1
        self.closed = True
        if self.mic is not None:
            try:
                self.mic.stop()
            except Exception:
                pass
            self.mic = None
        self.root.destroy()


def main() -> int:
    root = tk.Tk()
    LocalTrial(root)
    root.mainloop()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
