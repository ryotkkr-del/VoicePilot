"""VoicePilot: a compact Windows microphone panel and UI thread coordinator."""
from __future__ import annotations

import gc
from contextlib import nullcontext
import json
import os
from pathlib import Path
import secrets
import sys
import threading
import time
import tkinter as tk
import tempfile
import webbrowser
from tkinter import ttk, messagebox

from .agent import VoiceAgent, RunOutcome
from .control_permit import DesktopControlPermit
from .task_policy import LongTaskLimits
from .planner import Planner
from .audio import Microphone, SpeechRecognizer
from .bridge import BrowserBridge
from .hotkey import register_windows_hotkey
from .win_actions import WindowsController, current_process_elevated
from .ux import presentation, meter_level, friendly_error
from .ui_dispatch import UIDispatcher
from .voice_feedback import SpeechFeedback, result_announcement
from .chatgpt_handoff import extract_chatgpt_request, prepare_chatgpt_handoff
from .dictation import DictationController, dictation_intent, DictationError, DictationTarget
from .handsfree import HandsFreeSession, handsfree_control
from .interrupt import ActionGate, VoiceInterruptMonitor
from .screen_understanding import ScreenAssistant, screen_question
from .performance import OperationProfiler

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "VoicePilot"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULTS = {
    "token": "", "provider": "ollama", "gemini_model": "gemini-2.5-flash",
    "ollama_model": "gemma3:4b", "whisper_model": "small", "allow_cloud_ui": False,
    "auto_stop": True, "silence_seconds": 1.4, "allow_cloud_image": False,
    "voice_feedback": True, "measure_performance": False,
}
BG, WHITE, INK = "#F6F7FA", "#FFFFFF", "#19222E"
MUTED, ACCENT, BORDER = "#64748B", "#2563EB", "#E2E8F0"
FONT = "Yu Gothic UI"
# Whisper is loaded on first use and released after inactivity.
# This is a memory policy, not a reduction in speech recognition quality.
SPEECH_MODEL_IDLE_SECONDS = 75


def speech_model_idle_seconds(model_name: str) -> int:
    """Unload very large unused speech models sooner to reduce idle RAM.

    Frequent use still keeps the model loaded while an operation is running.
    A later utterance after unloading needs a fresh model load.
    """
    if model_name == "large-v3-turbo":
        return 30
    if model_name == "medium":
        return 45
    return SPEECH_MODEL_IDLE_SECONDS


def bootstrap_security_revision() -> int:
    """Inspect the bootstrap that will actually run at the next launch.

    Automatic app source updates deliberately do not overwrite this file.
    A newer app running under an older bootstrap must not claim to be fully
    protected by newer update-integrity checks.
    """
    try:
        import voicepilot_bootstrap as bootstrap
        value = getattr(bootstrap, "BOOTSTRAP_SECURITY_REVISION", 0)
        return value if type(value) is int and value >= 0 else 0
    except ImportError:
        return 0


def bootstrap_signing_key_configured() -> bool:
    """Read only the installed immutable bootstrap, never a downloaded key."""
    try:
        import voicepilot_bootstrap as bootstrap
        key = getattr(bootstrap, "PINNED_ED25519_PUBLIC_KEY_HEX", "")
        return isinstance(key, str) and len(key) == 64 and all(
            char in "0123456789abcdef" for char in key
        )
    except ImportError:
        return False


def load_config():
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    data = dict(DEFAULTS)
    if CONFIG_FILE.exists():
        try:
            loaded = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            data.update({k:v for k,v in loaded.items() if k in DEFAULTS})
        except (OSError, json.JSONDecodeError):
            pass
    if not data.get("token"):
        data["token"] = secrets.token_urlsafe(24)
    save_config(data)
    return data


def save_config(data):
    """Write settings atomically so a crash never destroys the pairing code."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", suffix=".tmp",
                                         prefix="voicepilot-", dir=CONFIG_DIR,
                                         delete=False) as handle:
            temporary = Path(handle.name)
            json.dump({k: v for k, v in data.items() if k in DEFAULTS}, handle,
                      ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, CONFIG_FILE)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


class VoicePilotApp:
    def __init__(self, root: tk.Tk):
        self.root = root
        self.ui = UIDispatcher(root)
        self.config = load_config()
        # Never persist permission: all process launches begin locked.
        self.control_permit = DesktopControlPermit()
        self.bridge = BrowserBridge(self.config["token"])
        self.bridge.start()
        self._extension_version_checked = False
        self.mic = Microphone()
        self.recognizer: SpeechRecognizer | None = None
        self._last_transcription_finished = 0.0
        self.windows = WindowsController()
        self.dictation = DictationController(self.windows, self.bridge)
        self.dictation_target: DictationTarget | None = None
        # Opt-in and volatile by design: NEVER enable the microphone on startup.
        self.handsfree = HandsFreeSession()
        self._handsfree_capture = False
        self._interrupt_monitor: VoiceInterruptMonitor | None = None
        self._action_gate: ActionGate | None = None
        self._capture_generation = 0
        self._closing = False
        self.session = {"recent_instructions": [], "recent_files": []}  # RAM only; never written to disk
        self.recording = False
        self.busy = False
        # A second Speak press while working requests a safe interruption,
        # then opens the microphone only AFTER the old worker has exited.
        self._record_after_cancel = False
        self._text_at_record_stop = ""
        self.phase = "idle"
        self._status_detail = None
        self.mini: tk.Toplevel | None = None
        self.stop_event = threading.Event()
        self.speaker = SpeechFeedback(on_error=self.log)
        self.record_started = 0.0
        self.last_external_hwnd = None
        self.build_ui()
        self.root.after(500, self.track_foreground)
        self.root.after(700, self.refresh_connection)
        self.root.after(10_000, self.release_idle_model)
        self.root.after(10_000, self._refresh_control_lock_timer)
        # Check stable GitHub Releases in the background; no GUI or LLM overhead.
        self.root.after(12_000, self._start_update_check)
        if sys.platform == "win32":
            register_windows_hotkey(
                lambda: self.ui.post(self.toggle_record),
                lambda: self.ui.post(self.abort),
                on_error=self.log,
            )
        self.log("PC操作は初期状態でロックされています。5分間の一時許可が必要です。")
        revision = bootstrap_security_revision()
        if revision < 3:
            self.log("注意：旧bootstrapは署名付き更新を検証しません。アプリだけ更新しても安全な更新基盤には切り替わりません。")
        elif not bootstrap_signing_key_configured():
            self.log("安全停止：新bootstrapに信頼済み開発者公開鍵が未設定です。自動更新は拒否されます。")

    def button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command,
                         bg=ACCENT if primary else "#EDF2F7", fg=WHITE if primary else INK,
                         activebackground="#1D4ED8" if primary else "#E2E8F0",
                         activeforeground=WHITE if primary else INK, relief="flat",
                         borderwidth=0, padx=14, pady=8, font=(FONT,10,"bold"), cursor="hand2")

    def card(self, parent, y=10):
        frame = tk.Frame(parent, bg=WHITE, padx=14, pady=12,
                         highlightbackground=BORDER, highlightthickness=1)
        frame.pack(fill="x", pady=(y,0))
        return frame

    def build_ui(self):
        self.root.title("VoicePilot · PCを声で操作")
        self.root.geometry("560x600")
        # Avoid forcing a window taller than a small laptop display.
        screen_height = max(420, self.root.winfo_screenheight())
        self.root.minsize(440, min(480, screen_height - 100))
        self.root.configure(bg=BG)

        # The entire panel is scrollable: settings and error details must remain
        # reachable on compact screens and at 125–200% Windows scaling.
        holder = tk.Frame(self.root, bg=BG)
        holder.pack(fill="both", expand=True)
        holder.grid_rowconfigure(0, weight=1)
        holder.grid_columnconfigure(0, weight=1)
        self.scroll_canvas = tk.Canvas(holder, bg=BG, highlightthickness=0,
                                       borderwidth=0)
        self.scroll_canvas.grid(row=0, column=0, sticky="nsew")
        self.scrollbar = ttk.Scrollbar(holder, orient="vertical",
                                       command=self.scroll_canvas.yview)
        self.scrollbar.grid(row=0, column=1, sticky="ns")
        self.scroll_canvas.configure(yscrollcommand=self.scrollbar.set)
        shell = tk.Frame(self.scroll_canvas, bg=BG, padx=22, pady=18)
        self._shell_window = self.scroll_canvas.create_window(
            (0, 0), window=shell, anchor="nw")
        shell.bind("<Configure>", self._refresh_scroll)
        self.scroll_canvas.bind("<Configure>", self._refresh_scroll)
        self.root.bind("<MouseWheel>", self._scroll_wheel)
        head = tk.Frame(shell, bg=BG)
        head.pack(fill="x")
        tk.Label(head,text="VoicePilot",font=("Segoe UI",21,"bold"),bg=BG,fg=INK).pack(side="left")
        self.conn_label = tk.Label(head,text="○ Chrome接続確認中",font=(FONT,9),bg=BG,fg=MUTED)
        self.conn_label.pack(side="right",pady=9)
        self.settings_button = self.button(head, "設定", self.toggle_settings)
        self.settings_button.pack(side="right", padx=(0,8))
        tk.Label(shell,text="声でPCを操作。難しい画面では画像も認識します。",font=(FONT,10),bg=BG,fg=MUTED).pack(anchor="w",pady=(1,5))
        lock_card = self.card(shell, 8)
        lock_row = tk.Frame(lock_card, bg=WHITE)
        lock_row.pack(fill="x")
        self.control_lock_label = tk.Label(
            lock_row, text="● PC操作はロック中", bg=WHITE, fg="#B91C1C",
            font=(FONT, 10, "bold"))
        self.control_lock_label.pack(side="left")
        self.control_lock_button = self.button(
            lock_row, "5分間だけ操作を許可", self.toggle_control_permission)
        self.control_lock_button.pack(side="right")
        tk.Label(lock_card, text="起動時は操作禁止。停止で再ロックします。",
                 bg=WHITE, fg=MUTED, font=(FONT, 9)).pack(anchor="w", pady=(5, 0))

        talk = self.card(shell, 10)
        row = tk.Frame(talk,bg=WHITE)
        row.pack(fill="x")
        self.rec_button = self.button(row,"●  話す",self.toggle_record,True)
        self.rec_button.pack(side="left")
        self.dictation_button = self.button(row, "文章入力", self.toggle_dictation)
        self.dictation_button.pack(side="left", padx=(6,0))
        self.status_label = tk.Label(row,text="準備完了",font=(FONT,10),bg=WHITE,fg=MUTED)
        self.status_label.pack(side="left",padx=12)
        self.record_meter = tk.Frame(talk, bg=WHITE)
        self.record_meter.pack(anchor="w", pady=(8, 0))
        self.meter_segments = []
        for i in range(5):
            segment = tk.Frame(self.record_meter, bg=BORDER, width=22, height=5)
            segment.pack(side="left", padx=(0, 4))
            self.meter_segments.append(segment)
        self.record_meter.pack_forget()
        self.status_hint = tk.Label(talk, text="Ctrl + Alt + Space で話す", bg=WHITE,
                                    fg=MUTED, font=(FONT, 9), anchor="w")
        self.status_hint.pack(fill="x", pady=(8, 0))
        hfrow = tk.Frame(talk, bg=WHITE)
        hfrow.pack(fill="x", pady=(6, 0))
        self.handsfree_button = self.button(hfrow, "ハンズフリー開始", self.toggle_handsfree)
        self.handsfree_button.pack(side="left")
        tk.Label(hfrow, text="開始後は声だけで続けて指示（無操作3分で終了）",
                 bg=WHITE, fg=MUTED, font=(FONT, 9)).pack(side="left", padx=(8, 0))
        self.button(row,"小さく表示",self.open_mini).pack(side="right")
        self.abort_button=self.button(row,"■ 停止",self.abort)
        self.abort_button.pack(side="right", padx=(0,6))
        tk.Label(talk,text="録音: Ctrl + Alt + Space   ·   中止: Ctrl + Alt + Esc",bg=WHITE,fg=MUTED,font=(FONT,9)).pack(anchor="w",pady=(5,0))
        self.auto_stop = tk.BooleanVar(value=self.config.get("auto_stop",True))
        tk.Checkbutton(talk,text="話し終わったら自動で実行（約1.4秒の無音を検知）",
                       variable=self.auto_stop,command=self.save_settings,
                       bg=WHITE,fg=INK,activebackground=WHITE,font=(FONT,9)).pack(anchor="w",pady=(4,0))
        self.voice_feedback = tk.BooleanVar(value=self.config.get("voice_feedback", True))
        tk.Checkbutton(talk, text="音声操作の結果を短く読み上げる", variable=self.voice_feedback,
                       command=self.change_voice_feedback, bg=WHITE, fg=INK,
                       activebackground=WHITE, font=(FONT, 9)).pack(anchor="w", pady=(2, 0))
        self.root.bind("<Escape>", lambda e:self.abort())

        input_card = self.card(shell)
        tk.Label(input_card,text="認識した指示（間違いは修正して Enter）",font=(FONT,10,"bold"),bg=WHITE,fg=INK).pack(anchor="w")
        irow=tk.Frame(input_card,bg=WHITE)
        irow.pack(fill="x",pady=(8,0))
        self.command=ttk.Entry(irow,font=(FONT,10))
        self.command.pack(side="left",fill="x",expand=True,ipady=6)
        self.command.bind("<Return>",lambda e:self.send_text())
        # Deliberately empty: a sample command can accidentally execute on Enter.
        self.send_button = self.button(irow,"実行",self.send_text,True)
        self.send_button.pack(side="left",padx=(7,0))
        self.chatgpt_button = self.button(irow,"ChatGPT下書き",self.send_to_chatgpt)
        self.chatgpt_button.pack(side="left",padx=(7,0))
        quickrow = tk.Frame(input_card, bg=WHITE)
        quickrow.pack(fill="x", pady=(7, 0))
        self.screen_button = self.button(quickrow, "今の画面を説明", self.explain_current_screen)
        self.screen_button.pack(side="left")
        self.long_task_button = self.button(quickrow, "長い作業（確認付き）", self.start_long_task)
        self.long_task_button.pack(side="left", padx=(7, 0))
        tk.Label(quickrow, text="読み取りのみ・PCを操作しません", bg=WHITE,
                 fg=MUTED, font=(FONT, 9)).pack(side="left", padx=(8, 0))

        settings=self.card(shell)
        self.settings_card = settings
        tk.Label(settings,text="AI・音声認識",font=(FONT,10,"bold"),bg=WHITE,fg=INK).pack(anchor="w")
        prow=tk.Frame(settings,bg=WHITE)
        prow.pack(fill="x",pady=(7,3))
        self.provider=tk.StringVar(value=self.config.get("provider","ollama"))
        for label,value in [("Ollama（完全ローカル・回数無制限）","ollama"),
                            ("Gemini（無料枠・上限あり）","gemini")]:
            tk.Radiobutton(prow,text=label,variable=self.provider,value=value,
                           bg=WHITE,fg=INK,activebackground=WHITE,command=self.save_settings,
                           font=(FONT,9)).pack(side="left",padx=(0,7))
        mrow=tk.Frame(settings,bg=WHITE)
        mrow.pack(fill="x",pady=(5,0))
        tk.Label(mrow,text="音声認識",bg=WHITE,fg=MUTED,font=(FONT,9),width=12,anchor="w").pack(side="left")
        self.whisper=tk.StringVar(value=self.config.get("whisper_model","small"))
        self.whisper_combo=ttk.Combobox(mrow,textvariable=self.whisper,values=["base","small","medium","large-v3-turbo"],
                                       width=15,state="readonly")
        self.whisper_combo.pack(side="left")
        self.whisper_combo.bind("<<ComboboxSelected>>", lambda e:self.save_settings())
        tk.Label(mrow,text="small推奨（精度重視ならmedium）",bg=WHITE,fg=MUTED,font=(FONT,8)).pack(side="left",padx=9)
        orow=tk.Frame(settings,bg=WHITE)
        orow.pack(fill="x",pady=(7,0))
        tk.Label(orow,text="Ollamaモデル",bg=WHITE,fg=MUTED,font=(FONT,9),width=12,anchor="w").pack(side="left")
        self.ollama_model=ttk.Entry(orow,font=(FONT,9))
        self.ollama_model.insert(0,self.config.get("ollama_model","gemma3:4b"))
        self.ollama_model.pack(side="left",fill="x",expand=True)
        self.ollama_model.bind("<FocusOut>",lambda e:self.save_settings())
        krow=tk.Frame(settings,bg=WHITE)
        krow.pack(fill="x",pady=(7,0))
        tk.Label(krow,text="Gemini APIキー",bg=WHITE,fg=MUTED,font=(FONT,9),width=12,anchor="w").pack(side="left")
        self.key=ttk.Entry(krow,show="•",font=(FONT,9))
        self.key.pack(side="left",fill="x",expand=True)
        if os.environ.get("GEMINI_API_KEY"):
            self.key.insert(0,os.environ["GEMINI_API_KEY"])
        self.cloud_permission=tk.BooleanVar(value=self.config.get("allow_cloud_ui",False))
        tk.Checkbutton(settings,text="Geminiに指示・画面の要素名・ウィンドウ名を送ることを許可",
                       variable=self.cloud_permission,command=self.save_settings,bg=WHITE,
                       activebackground=WHITE,fg=INK,font=(FONT,9)).pack(anchor="w",pady=(7,0))
        self.cloud_image_permission=tk.BooleanVar(value=self.config.get("allow_cloud_image",False))
        tk.Checkbutton(settings,text="画像での操作が必要なとき、スクリーンショットの送信も許可（Geminiのみ）",
                       variable=self.cloud_image_permission,command=self.save_settings,bg=WHITE,
                       activebackground=WHITE,fg=INK,font=(FONT,9)).pack(anchor="w",pady=(4,0))
        tk.Label(settings,text="キーは保存しません。無料枠には回数制限があり、送信データの取扱いに注意してください。",
                 bg=WHITE,fg=MUTED,font=(FONT,8)).pack(anchor="w",pady=(2,0))
        self.performance_diagnostics = tk.BooleanVar(value=self.config.get("measure_performance", False))
        tk.Checkbutton(
            settings, text="性能診断（処理時間・CPU時間・メモリのみ、操作ログに一時表示）",
            variable=self.performance_diagnostics, command=self.save_settings,
            bg=WHITE, fg=INK, activebackground=WHITE, font=(FONT, 9)
        ).pack(anchor="w", pady=(8, 0))

        # Keep the everyday UI sparse.  Settings are one click away.
        self.settings_card.pack_forget()
        pair=self.card(shell)
        self.pair_card=pair
        tk.Label(pair,text="Chrome拡張機能",font=(FONT,10,"bold"),bg=WHITE,fg=INK).pack(side="left")
        self.button(pair,"接続コードをコピー",self.copy_token).pack(side="right")

        logbar=tk.Frame(shell,bg=BG)
        logbar.pack(fill="x",pady=(12,6))
        tk.Label(logbar,text="操作ログ",font=(FONT,10,"bold"),bg=BG,fg=INK).pack(side="left")
        tk.Label(logbar,text="操作状況のみ記録（指示本文は非表示）",font=(FONT,8),bg=BG,fg=MUTED).pack(side="right")
        self.log_box=tk.Text(shell,height=8,wrap="word",bg=WHITE,fg=INK,relief="flat",
                             font=(FONT,10),padx=12,pady=10,borderwidth=0)
        self.log_box.pack(fill="both",expand=True)
        self.log_box.configure(state="disabled")
        self.root.protocol("WM_DELETE_WINDOW",self.close)

    def _refresh_scroll(self, _event=None):
        """Fit contents to the current width; expose scrolling only as needed."""
        try:
            width = self.scroll_canvas.winfo_width()
            if width > 1:
                self.scroll_canvas.itemconfigure(self._shell_window, width=width)
            self.scroll_canvas.configure(scrollregion=self.scroll_canvas.bbox("all"))
            content_height = self.scroll_canvas.bbox("all")[3]
            if content_height > self.scroll_canvas.winfo_height() + 2:
                self.scrollbar.grid()
            else:
                self.scrollbar.grid_remove()
        except (tk.TclError, IndexError, TypeError):
            pass

    def _scroll_wheel(self, event):
        # Do not steal wheel events from the editable operation log.
        if event.widget is self.log_box:
            return
        bounds = self.scroll_canvas.bbox("all")
        if bounds and self.scroll_canvas.winfo_height() < bounds[3]:
            self.scroll_canvas.yview_scroll(-int(event.delta / 120), "units")

    def toggle_settings(self):
        if self.settings_card.winfo_manager():
            self.settings_card.pack_forget()
            self.settings_button.configure(text="設定")
        else:
            self.settings_card.pack(fill="x", pady=(10,0), before=self.pair_card)
            self.settings_button.configure(text="設定を閉じる")
        # Preserve the user's chosen window size and position.  Only the
        # scrollable content changes; never jump to an 800-pixel dialog.
        self.root.after_idle(self._refresh_scroll)

    def track_foreground(self):
        """Remember the last real target app even if the user clicks our mic UI."""
        try:
            hwnd = self.windows.foreground_hwnd()
            title = self.windows.active_window_title().lower()
            if hwnd and title and 'voicepilot' not in title:
                self.last_external_hwnd = hwnd
            self.root.after(1200, self.track_foreground)
        except (RuntimeError, tk.TclError):
            return

    def restore_target(self):
        title = self.windows.active_window_title().lower()
        if 'voicepilot' in title and self.last_external_hwnd:
            self.windows.focus_handle(self.last_external_hwnd)

    def open_mini(self):
        if self.mini and self.mini.winfo_exists():
            self.mini.lift()
            return
        self.mini=tk.Toplevel(self.root)
        self.mini.title("VoicePilot – ミニ")
        x=max(0,self.root.winfo_rootx()+40)
        y=max(0,self.root.winfo_rooty()+40)
        self.mini.geometry(f"350x126+{x}+{y}")
        self.mini.resizable(False,False)
        self.mini.configure(bg=WHITE)
        # Do not cover the user's target controls just to keep our panel visible.
        self.mini.attributes("-topmost",False)
        frame=tk.Frame(self.mini,bg=WHITE,padx=12,pady=10)
        frame.pack(fill="both",expand=True)
        row=tk.Frame(frame,bg=WHITE)
        row.pack(fill="x")
        tk.Label(row,text="VoicePilot",bg=WHITE,fg=INK,font=("Segoe UI",12,"bold")).pack(side="left")
        self.mini_status=tk.Label(row,text="準備完了",bg=WHITE,fg=MUTED,font=(FONT,9))
        self.mini_status.pack(side="right")
        self.mini_hint = tk.Label(frame, text="Ctrl + Alt + Space で話す", bg=WHITE,
                                  fg=MUTED, font=(FONT,8), anchor="w")
        self.mini_hint.pack(side="bottom", fill="x", pady=(3,0))
        btns=tk.Frame(frame,bg=WHITE)
        btns.pack(fill="x",pady=(10,0))
        self.mini_rec_button=self.button(btns,"● 話す",self.toggle_record,True)
        self.mini_rec_button.pack(side="left")
        self.mini_handsfree_button = self.button(btns, "連続ON", self.toggle_handsfree)
        self.mini_handsfree_button.pack(side="left", padx=(5,0))
        self.button(btns,"停止",self.abort).pack(side="left",padx=8)
        self.button(btns,"画面を開く",self.close_mini).pack(side="right")
        self.mini.protocol("WM_DELETE_WINDOW",self.close_mini)
        self.root.withdraw()
        self._apply_phase()

    def close_mini(self):
        if self.mini:
            self.mini.destroy()
            self.mini=None
        self.root.deiconify()
        self.root.lift()

    def copy_token(self):
        self.root.clipboard_clear()
        self.root.clipboard_append(self.config["token"])
        # Pairing tokens are credentials. Remove the token after a short time,
        # but NEVER wipe content the user copied afterwards.
        self.root.after(60_000, self._clear_expired_pairing_clipboard)
        self.log("接続コードをコピーしました（60秒後にクリップボードから自動消去）")

    def _clear_expired_pairing_clipboard(self):
        try:
            if self.root.clipboard_get() == self.config["token"]:
                self.root.clipboard_clear()
        except tk.TclError:
            pass

    def change_voice_feedback(self):
        if not self.voice_feedback.get():
            self.speaker.stop()
        self.save_settings()

    def _speak_result(self, stage: str, verified_action: str | None = None):
        """Only voice-originated operations speak; text editing stays quiet."""
        if self.config.get("voice_feedback", True) and not self.stop_event.is_set():
            message = result_announcement(stage, verified_action)
            if message:
                self.speaker.say(message)

    def save_settings(self):
        self.config.update({"provider":self.provider.get(),"whisper_model":self.whisper.get(),
                            "ollama_model":self.ollama_model.get().strip() or "gemma3:4b",
                            "allow_cloud_ui":self.cloud_permission.get(),
                            "allow_cloud_image": self.cloud_image_permission.get(),
                            "auto_stop":self.auto_stop.get(),
                            "voice_feedback":self.voice_feedback.get(),
                            "measure_performance":self.performance_diagnostics.get()
                            if hasattr(self, "performance_diagnostics") else
                            self.config.get("measure_performance", False)})
        save_config(self.config)

    def _start_update_check(self):
        def work():
            try:
                from voicepilot_bootstrap import check_and_stage, UpdateError
                status = check_and_stage(Path(__file__).resolve().parent.parent)
                if status == "downloaded":
                    self.log("新しいVoicePilotを安全に保存しました。次回起動時に自動更新されます。")
                elif status == "staged":
                    self.log("更新が準備されています。次回起動時に適用します。")
            except Exception as exc:
                # An update outage should never stop desktop control or spam popups.
                self.log("更新の確認を保留しました（通常のPC操作は利用できます）。" + str(exc)[:90])
        threading.Thread(target=work, daemon=True, name="voicepilot-update-check").start()
        try:
            self.root.after(6 * 60 * 60 * 1000, self._start_update_check)
        except tk.TclError:
            pass

    def _check_extension_compatibility(self):
        """One best-effort reconnect, never interrupt an active user action."""
        try:
            from voicepilot_bootstrap import app_version
            expected = app_version(Path(__file__).resolve().parent.parent)
            data = self.bridge.call("extension_version", {}, timeout=5)
            observed = data.get("version") if isinstance(data, dict) else None
            if observed and observed != expected:
                self.log("Chrome拡張の更新を反映しています。うまくいかなければ拡張機能を再読み込みしてください。")
                self.bridge.call("extension_reload", {}, timeout=5)
        except Exception:
            self.log("Chrome拡張の更新確認はできませんでした。操作できない場合は拡張機能を再読み込みしてください。")

    def refresh_connection(self):
        if self.bridge.error:
            self.conn_label.configure(text="● Chrome接続エラー",fg="#B91C1C")
        elif self.bridge.connected:
            self.conn_label.configure(text="● Chrome接続中",fg="#15803D")
            if not getattr(self, "_extension_version_checked", True):
                self._extension_version_checked = True
                threading.Thread(target=self._check_extension_compatibility,
                                 daemon=True, name="voicepilot-extension-compat").start()
        else:
            self.conn_label.configure(text="○ Chrome拡張なし（PC操作可）",fg=MUTED)
        self.root.after(2500,self.refresh_connection)

    def log(self,text):
        def append():
            stamp=time.strftime("%H:%M:%S")
            self.log_box.configure(state="normal")
            self.log_box.insert("end",f"[{stamp}] {text}\n")
            # Limit retained messages: the log is a short-lived aid, not a database.
            line_count = int(self.log_box.index("end-1c").split(".")[0])
            if line_count > 160:
                self.log_box.delete("1.0", f"{line_count - 160}.0")
            self.log_box.see("end")
            self.log_box.configure(state="disabled")
        self.ui.post(append)

    def _apply_phase(self, heard_voice=False, elapsed=0):
        p = presentation(self.phase, heard_voice=heard_voice, elapsed=elapsed,
                         auto_stop=self.auto_stop.get())
        colors = {"neutral": MUTED, "recording": "#C2410C",
                  "working": ACCENT, "success": "#15803D",
                  "warning": "#A16207", "error": "#B91C1C"}
        self.status_label.configure(text=p.title, fg=colors[p.tone])
        hint = self._status_detail or p.hint
        self.status_hint.configure(text=hint[:115])
        recording = self.phase == "listening"
        if recording:
            if not self.record_meter.winfo_manager():
                self.record_meter.pack(anchor="w", after=self.status_hint, pady=(5,0))
        elif self.record_meter.winfo_manager():
            self.record_meter.pack_forget()
        can_interrupt = self.busy and not recording
        can_speak = p.can_record or recording or can_interrupt
        self.rec_button.configure(state="normal" if can_speak else "disabled")
        self.abort_button.configure(state="normal" if p.can_stop or self.dictation_target else "disabled")
        self.dictation_button.configure(
            text="入力終了" if self.dictation_target else "文章入力",
            state="disabled" if self.busy or recording else "normal")
        # Disable actions that would otherwise silently be ignored while busy.
        entry_action_state = "disabled" if self.busy or recording else "normal"
        self.send_button.configure(state=entry_action_state, text="入力" if self.dictation_target else "実行")
        self.chatgpt_button.configure(state=entry_action_state)
        self.screen_button.configure(state=entry_action_state)
        self.long_task_button.configure(state=entry_action_state)
        hf_on = self.handsfree.enabled
        self.handsfree_button.configure(
            text="■ ハンズフリー終了" if hf_on else "ハンズフリー開始",
            bg="#15803D" if hf_on else "#EDF2F7",
            fg=WHITE if hf_on else INK,
            state="normal" if hf_on or not self.busy and not recording else "disabled")
        if hf_on and not recording and not self.busy:
            hint = "連続待受：声で指示できます／3分無操作で自動終了"
            self.status_label.configure(text="ハンズフリー待受中", fg="#15803D")
            self.status_hint.configure(text=hint)
        elif hf_on and self.busy:
            gate = getattr(self, "_action_gate", None)
            if gate is not None and gate.paused:
                hint = "一時停止中：「続けて」で再開／「やめて」で中止"
                self.status_label.configure(text="音声で一時停止中", fg="#A16207")
            else:
                hint = "操作中も「待って」「やめて」で割り込みできます"
            self.status_hint.configure(text=hint)
        if self.dictation_target and not self.busy and not recording:
            hint = "文章入力中：Ctrl+Alt+Spaceで話す／「入力終了」で解除"
            self.status_label.configure(text="文章入力モード", fg=ACCENT)
            self.status_hint.configure(text=hint)
        if self._record_after_cancel:
            hint = "現在の操作を止めたあと、録音を開始します"
            self.status_hint.configure(text=hint)
        self._refresh_record_buttons()
        if self.mini and self.mini.winfo_exists():
            self.mini_status.configure(text=p.title, fg=colors[p.tone])
            self.mini_handsfree_button.configure(text="連続OFF" if self.handsfree.enabled else "連続ON")
            self.mini_hint.configure(text=hint[:52])
            self.mini_rec_button.configure(state="normal" if can_speak else "disabled")

    def set_phase(self, stage):
        def update():
            self.phase = stage
            self._status_detail = None
            self._apply_phase()
        self.ui.post(update)

    def show_transcript(self, command):
        """Keep recognised text editable without overwriting an intervening draft."""
        def update():
            # Typing during a long CPU transcription must not be destroyed by
            # the eventual recognition result. The spoken command still runs;
            # its transcript is recorded in the temporary log.
            if self.command.get() != self._text_at_record_stop:
                return
            self.command.delete(0, tk.END)
            self.command.insert(0, command)
            self.command.xview_moveto(0)
        self.ui.post(update)

    def _refresh_record_buttons(self):
        if self.recording:
            title, color = "■ 録音を止める", "#DC2626"
        elif self._record_after_cancel:
            title, color = "録音予約を取消", "#A16207"
        elif self.busy:
            title, color = "■ 中断して話す", "#B45309"
        else:
            title, color = "● 話す", ACCENT
        self.rec_button.configure(text=title,bg=color)
        if self.mini and self.mini.winfo_exists():
            self.mini_rec_button.configure(text=title,bg=color)

    def _refresh_control_lock_timer(self):
        if self._closing:
            return
        self._refresh_control_lock_ui()
        self.root.after(10_000, self._refresh_control_lock_timer)

    def _refresh_control_lock_ui(self):
        if self.control_permit.allowed():
            self.control_lock_label.configure(
                text="● PC操作を一時許可中（最大5分）", fg="#166534")
            self.control_lock_button.configure(text="操作をロック")
        else:
            self.control_lock_label.configure(
                text="● PC操作はロック中", fg="#B91C1C")
            self.control_lock_button.configure(text="5分間だけ操作を許可")

    def toggle_control_permission(self):
        """Only a click in this local GUI can arm desktop execution."""
        if self.control_permit.allowed():
            self.control_permit.revoke()
            self.abort()
            self._refresh_control_lock_ui()
            self.log("PC操作を再ロックしました。実行中の処理にも停止を要求しました")
            return
        if self.busy or self.recording or self._closing:
            self.log("録音や作業が終了してからPC操作を許可してください")
            return
        if not messagebox.askyesno(
                "PC操作の一時許可 · VoicePilot",
                "この試作アプリはWindows実機で正式な安全検証を完了していません。\\n"
                "外部アプリ・ブラウザ・ファイルを操作する場合があります。\\n"
                "重要な文書を閉じ、ダミーデータでのみ試してください。\\n\\n"
                "この起動中に限り、PC操作を最大5分間許可しますか？",
                parent=self.root):
            self.log("PC操作のロックを維持しました")
            return
        self.control_permit.arm()
        self._refresh_control_lock_ui()
        self.log("PC操作を最大5分間だけ許可しました。停止・終了すると再ロックされます")

    def _control_locked(self) -> bool:
        if self.control_permit.allowed():
            return False
        self.log("PC操作はロック中です。GUIから明示的に許可してください")
        self._refresh_control_lock_ui()
        return True

    def toggle_handsfree(self):
        """Explicitly opt into a time-limited sequential microphone session."""
        if self.handsfree.enabled:
            self.stop_handsfree("ハンズフリーを終了しました")
            return
        if self.busy or self.recording or self._closing:
            self.log("処理が終了してからハンズフリーを開始してください")
            return
        self.handsfree.start()
        self.stop_event.clear()
        self.log("ハンズフリー開始。作業中は「待って」「続けて」「やめて」で制御できます")
        self._apply_phase()
        self._schedule_handsfree_resume()

    def stop_handsfree(self, reason: str = "ハンズフリーを終了しました"):
        """Disable first to invalidate every scheduled rearm callback."""
        if not self.handsfree.enabled:
            return
        self.handsfree.stop()
        monitor = getattr(self, "_interrupt_monitor", None)
        if monitor is not None:
            monitor.request_stop()
        gate = getattr(self, "_action_gate", None)
        if gate is not None:
            gate.resume()  # disabling listening must not strand a paused worker
        if self.recording and self._handsfree_capture:
            self._stop_record(discard=True)
        self._handsfree_capture = False
        if not self._closing:
            self.log(reason)
            self._apply_phase()

    def _schedule_handsfree_resume(self, delay_ms: int = 550):
        """Only a settled, silent, task-free app may reopen the microphone.

        Generation tokens ensure a stale after() callback cannot reopen it
        after Stop, a mode switch or application shutdown.
        """
        generation = self.handsfree.generation
        def rearm():
            if self._closing or not self.handsfree.is_current(generation):
                return
            if self.busy or self.recording:
                return  # mark_idle() will reschedule when the task finishes
            if self.handsfree.expired():
                self.stop_handsfree("3分間音声操作がなかったため、ハンズフリーを終了しました")
                return
            if self.speaker.is_active():
                self.root.after(250, rearm)
                return
            # A brief quiet margin after SAPI ends avoids playback tail echo.
            self.root.after(450, lambda: self._start_handsfree_capture(generation))
        self.root.after(delay_ms, rearm)

    def _start_handsfree_capture(self, generation: int):
        if (self._closing or not self.handsfree.is_current(generation)
                or self.busy or self.recording):
            return
        if self.handsfree.expired():
            self.stop_handsfree("3分間音声操作がなかったため、ハンズフリーを終了しました")
            return
        if self.speaker.is_active():
            self._schedule_handsfree_resume(300)
            return
        self.stop_event.clear()
        try:
            self.mic.start(continuous=True)
        except Exception as exc:
            # Do not repeatedly reopen a missing or in-use microphone.
            self.stop_handsfree("マイクを開始できなかったため連続対話を終了しました")
            self.log(friendly_error(exc))
            self.set_phase("error")
            return
        self._handsfree_capture = True
        self.recording = True
        self.record_started = time.monotonic()
        self._capture_generation += 1
        generation = self._capture_generation
        self._refresh_record_buttons()
        self.set_phase("listening")
        self.root.after(220, lambda: self.check_record_time(generation))

    def toggle_record(self):
        if self.recording:
            self._stop_record()
            return
        if self.busy:
            if self._record_after_cancel:
                self._record_after_cancel = False
                self.log("次の録音予約を取り消しました")
                self._apply_phase()
                return
            self._record_after_cancel = True
            self.abort(keep_record_request=True)
            self.log("操作を中断し、終了したら録音を始めます")
            self._apply_phase()
            return
        self.speaker.stop()  # Never feed our own voice back into the microphone.
        try:
            self.mic.start()
        except Exception as e:
            self.log(friendly_error(e))
            self.set_phase("error")
            return
        self._handsfree_capture = False
        self.recording=True
        self.record_started=time.monotonic()
        self._capture_generation += 1
        generation = self._capture_generation
        self._refresh_record_buttons()
        self.set_phase("listening")
        self.root.after(220, lambda: self.check_record_time(generation))

    def _stop_record(self,discard=False):
        if not self.recording:return
        self.recording=False
        handsfree_utterance = self._handsfree_capture
        self._capture_generation += 1  # invalidate stale microphone timer callbacks
        self._handsfree_capture = False
        self._text_at_record_stop = self.command.get()
        self._refresh_record_buttons()
        try:samples=self.mic.stop()
        except Exception as e:
            if handsfree_utterance:
                self.handsfree.stop()  # Don't leave an active but silent session.
            self.log(friendly_error(e))
            self.set_phase("error")
            return
        if discard:
            self.set_phase("interrupted" if self.stop_event.is_set() else "no_speech")
            return
        self.busy=True
        self.stop_event.clear()
        runtime=self.runtime_settings()
        self.set_phase("transcribing")
        record_finished_at = time.perf_counter() if runtime.get("measure_performance") else None
        threading.Thread(target=self.transcribe_then_execute,
                         args=(samples, runtime, handsfree_utterance, record_finished_at),
                         daemon=True).start()

    def check_record_time(self, generation=None):
        if (not self.recording or (generation is not None and
                                   generation != self._capture_generation)):
            return
        a=self.mic.activity()
        elapsed=a["elapsed"]
        self.phase = "listening"
        self._status_detail = None
        self._apply_phase(heard_voice=a["heard_voice"], elapsed=elapsed)
        # Mic level updates are derived from the existing recording callback.
        lit = meter_level(a["level"], a["threshold"])
        for i, seg in enumerate(self.meter_segments):
            seg.configure(bg="#C2410C" if i < lit else BORDER)
        if self._handsfree_capture:
            if not a["heard_voice"] and self.handsfree.expired():
                self.stop_handsfree("3分間音声操作がなかったため、ハンズフリーを終了しました")
                return
            if a["heard_voice"]:
                if (a["silence"] >= self.config["silence_seconds"]
                        and a.get("speech_elapsed", 0) >= 0.25):
                    self._stop_record()
                    return
                if a.get("speech_elapsed", 0) >= 30:
                    self._stop_record()
                    return
            self.root.after(220, lambda: self.check_record_time(generation))
            return
        if elapsed >= 30:
            self._stop_record()
            return
        if self.auto_stop.get():
            if a["heard_voice"] and elapsed>=1.3 and a["silence"]>=self.config["silence_seconds"]:
                self._stop_record()
                return
            if not a["heard_voice"] and elapsed>=12:
                self.log("音声が検出されなかったため録音を終了しました")
                self._stop_record(discard=True)
                return
        self.root.after(220, lambda: self.check_record_time(generation))

    def release_idle_model(self):
        """Free the expensive ASR model after a quiet interval.

        Resource teardown happens off the GUI thread. No microphone stream or
        inference model is kept running while the user is inactive.
        """
        if (not self.busy and not self.recording and self.recognizer is not None
                and self._last_transcription_finished > 0
                and time.monotonic() - self._last_transcription_finished >=
                speech_model_idle_seconds(getattr(self.recognizer, "model_name", "small"))):
            stale, self.recognizer = self.recognizer, None
            def unload(recognizer):
                recognizer.model = None
                gc.collect()
            threading.Thread(target=unload, args=(stale,), daemon=True).start()
        try:
            self.root.after(10_000, self.release_idle_model)
        except tk.TclError:
            pass

    def runtime_settings(self):
        return {"provider":self.provider.get(),"key":self.key.get().strip(),
                "ollama_model":self.ollama_model.get().strip() or "gemma3:4b",
                "gemini_model":self.config.get("gemini_model","gemini-2.5-flash"),
                "whisper_model":self.whisper.get(),"allow_cloud_ui":self.cloud_permission.get(),
                "allow_cloud_image":self.cloud_image_permission.get(),
                "measure_performance": bool(self.config.get("measure_performance", False))}

    def transcribe_then_execute(self,samples,runtime,handsfree_utterance=False,
                                record_finished_at=None):
        profile = (OperationProfiler(started_at=record_finished_at)
                   if runtime.get("measure_performance", False) else None)
        finish_stage="error"
        finish_detail=None
        verified_action=None
        is_dictation=False
        try:
            model=runtime["whisper_model"]
            if self.recognizer is None or self.recognizer.model_name!=model:
                self.recognizer=SpeechRecognizer(model)
                self.log(f"音声モデル {model} を読み込んでいます（初回はダウンロード）")
            with (profile.phase("asr") if profile else nullcontext()):
                command=self.recognizer.transcribe(samples)
            if self.stop_event.is_set():
                finish_stage="interrupted"
                return
            if not command:
                if not handsfree_utterance:
                    self.log("音声を認識できませんでした。もう一度試してください")
                finish_stage="no_speech"
                return
            if handsfree_utterance:
                # Only actual recognized speech extends the idle window;
                # microphone energy/noise by itself must not do so.
                self.handsfree.activity()
            # Strong, whole-utterance stop phrase. Never send it to the LLM or
            # insert it into a document. Disable immediately before finishing.
            if handsfree_utterance and handsfree_control(command) == "stop":
                self.handsfree.stop()
                self.ui.post(lambda: (self.log("ハンズフリーを終了しました"), self._apply_phase()))
                finish_stage = "completed"
                finish_detail = "ハンズフリーを終了しました"
                return
            is_dictation = self.dictation_target is not None or dictation_intent(command, False) is not None
            with (profile.phase("execute") if profile else nullcontext()):
                if is_dictation:
                    self.log("音声タイピング：入力モードの指示を認識しました" if not self.dictation_target
                             else "音声タイピング：認識結果を処理しています（本文はログに残しません）")
                    outcome = self._run_dictation_command(command)
                else:
                    self.show_transcript(command)
                    self.log("指示を受け付けました（本文は操作ログに残しません）")
                    outcome=self._run_agent(command,runtime)
            finish_stage="interrupted" if self.stop_event.is_set() else outcome.status
            verified_action=outcome.verified_action if finish_stage == "completed" else None
            if finish_stage in {"error", "incomplete", "unverified", "handoff", "handoff_clipboard", "answered"}:
                finish_detail=outcome.summary
        except Exception as e:
            finish_detail=friendly_error(e)
            self.log(f"音声処理エラー: {finish_detail}")
            finish_stage="error"
        finally:
            self._last_transcription_finished = time.monotonic()
            if handsfree_utterance:
                if finish_stage == "error" and self.handsfree.failure():
                    self.handsfree.stop()
                    self.log("連続して音声処理が失敗したため、ハンズフリーを終了しました")
                elif finish_stage not in {"error", "no_speech", "interrupted"}:
                    self.handsfree.success()
            if profile is not None:
                self.log(profile.summary(voice=True, status=finish_stage))
            self.mark_idle(finish_stage, finish_detail)
            if not is_dictation and not (handsfree_utterance and finish_stage in {"no_speech", "interrupted"}):
                self._speak_result(finish_stage, verified_action)

    def toggle_dictation(self):
        """Explicit UI toggle, never an implicit mode switch by page content."""
        if self.busy or self.recording:
            return
        if self.dictation_target is not None:
            self.dictation_target = None
            self.log("文章入力モードを終了しました")
            self._apply_phase()
            return
        self.busy = True
        self.stop_event.clear()
        self.set_phase("acting")
        threading.Thread(target=self._start_dictation_worker, daemon=True).start()

    def _start_dictation_worker(self):
        status, detail = "completed", None
        try:
            outcome = self._run_dictation_command("文章入力開始")
            status = outcome.status
            if status != "completed":
                detail = outcome.summary
        except Exception as exc:
            status, detail = "error", str(exc)[:150]
            self.log(detail)
        finally:
            self.mark_idle(status, detail)

    def _run_dictation_command(self, command: str) -> RunOutcome:
        try:
            intent = dictation_intent(command, self.dictation_target is not None)
            if intent is None:
                return RunOutcome("incomplete", "文章入力の指示を認識できませんでした")
            action, text = intent
            if action == "start":
                self.restore_target()
                self.dictation_target = self.dictation.start()
                self.log("文章入力モード開始。入力欄を固定しました。Ctrl+Alt+Spaceで話してください")
                return RunOutcome("completed", "文章入力モードを開始しました")
            if action == "stop":
                self.dictation_target = None
                self.log("文章入力モードを終了しました")
                return RunOutcome("completed", "文章入力モードを終了しました")
            if action == "already_on":
                return RunOutcome("completed", "文章入力モードは有効です")
            if action == "empty":
                return RunOutcome("no_speech", "入力する内容がありません")
            if self.stop_event.is_set():
                return RunOutcome("interrupted", "入力を中止しました")
            self.restore_target()
            verified = self.dictation.insert(self.dictation_target, text)
            # Never log dictated text, private labels, or current editor contents.
            self.log(f"文章入力：{len(text)}文字を挿入しました（内容は記録しません）")
            return RunOutcome("completed" if verified else "unverified",
                              "入力位置へ追記しました" if verified else
                              "文字入力を送信しました。内容は入力先で確認してください")
        except (DictationError, ValueError, RuntimeError, ConnectionError, TimeoutError) as exc:
            self.log(f"文章入力を停止しました：{str(exc)[:130]}")
            # A changed focus must never silently retarget a different editor.
            self.dictation_target = None
            return RunOutcome("incomplete", str(exc)[:180])
        except Exception as exc:
            # An unexpected accessibility/COM error must never switch to blind
            # typing. Do not echo raw exception values that may contain input.
            self.dictation_target = None
            self.log(f"文章入力を停止しました：対象を確認できません（{type(exc).__name__}）")
            return RunOutcome("incomplete", "入力先を安全に確認できませんでした。入力モードを開始し直してください")

    def explain_current_screen(self):
        """One click to ask about the screen, even during dictation mode."""
        if self.busy or self.recording:
            return
        self.busy = True
        self.stop_event.clear()
        runtime = self.runtime_settings()
        self.set_phase("acting")
        threading.Thread(target=self.run_screen_task,
                         args=("今の画面を説明して", runtime), daemon=True).start()

    def run_screen_task(self, question, runtime):
        stage, detail = "error", None
        try:
            result = self._run_agent(question, runtime)
            stage = "interrupted" if self.stop_event.is_set() else result.status
            if stage in {"answered", "error", "incomplete"}:
                detail = result.summary
        except Exception as exc:
            detail = friendly_error(exc)
            self.log(f"画面の説明エラー: {detail}")
        finally:
            self.mark_idle(stage, detail)
            self._speak_result(stage)

    def start_long_task(self):
        """Explicit per-task opt-in; never enabled by a spoken web page or model."""
        if self.busy or self.recording or self.dictation_target is not None:
            return
        command = self.command.get().strip()
        if not command:
            self.log("長い作業の目標を入力してください")
            return
        # The initial confirmation only grants a bounded *attempt*, not
        # permission for risky site actions, drafts, saves or file movement.
        if not messagebox.askyesno(
                "自律作業の開始確認 · VoicePilot",
                "最大12回のPC操作・最大240秒の作業を開始します。\n"
                "約6操作ごとに続行確認し、送信・保存・移動等は個別に確認します。\n"
                "結果が不明な操作やエラーがあれば停止します。\n\n"
                "タスクを開始しますか？",
                parent=self.mini if self.mini and self.mini.winfo_exists() else self.root):
            return
        self.busy = True
        self.stop_event.clear()
        runtime = self.runtime_settings()
        # Never log the raw goal; it may contain personal information.
        self.log("自律作業を開始しました（目標の文字列はログに保存しません）")
        self.set_phase("acting")
        threading.Thread(target=self.run_text_task,
                         args=(command, runtime, False, True), daemon=True).start()

    def send_text(self):
        if self.busy or self.recording:return
        command=self.command.get().strip()
        if not command:
            self.log("音声で話すか、入力欄に指示を入力してください")
            return
        self.busy=True
        self.stop_event.clear()
        runtime=self.runtime_settings()
        if self.dictation_target or dictation_intent(command, False):
            self.log("文章入力：文字列はログに保存しません")
        else:
            self.log("指示を受け付けました（本文は操作ログに残しません）")
        self.set_phase("acting")
        threading.Thread(target=self.run_text_task,args=(command,runtime),daemon=True).start()

    def send_to_chatgpt(self):
        """Explicit handoff button: never submits to ChatGPT or runs a planner."""
        if self.busy or self.recording:
            return
        prompt = self.command.get().strip()
        if not prompt:
            self.log("ChatGPTに渡す文章を入力してください")
            return
        self.busy = True
        self.stop_event.clear()
        runtime = self.runtime_settings()
        self.set_phase("acting")
        threading.Thread(target=self.run_text_task, args=(prompt, runtime, True), daemon=True).start()

    def _clipboard_copy(self, value: str):
        """Tk clipboard is accessed only on the GUI thread."""
        completed = threading.Event()
        errors = []
        def update():
            try:
                self.root.clipboard_clear()
                self.root.clipboard_append(value)
                self.root.update_idletasks()
            except Exception as exc:
                errors.append(exc)
            finally:
                completed.set()
        self.ui.post(update)
        if not completed.wait(3.0):
            raise RuntimeError("クリップボードへのコピーが時間切れになりました")
        if errors:
            raise RuntimeError("クリップボードにコピーできませんでした") from errors[0]

    def _run_chatgpt(self, prompt: str) -> RunOutcome:
        result = prepare_chatgpt_handoff(
            prompt, self.bridge, self._clipboard_copy, webbrowser.open,
            cancelled=self.stop_event.is_set,
        )
        self.log(result.message)
        return RunOutcome(result.stage, result.message)

    def run_text_task(self,command,runtime,force_chatgpt=False,autonomous=False):
        profile = OperationProfiler() if runtime.get("measure_performance", False) else None
        finish_stage="error"
        finish_detail=None
        try:
            with (profile.phase("execute") if profile else nullcontext()):
                outcome = (self._run_chatgpt(command) if force_chatgpt else
                           self._run_dictation_command(command) if
                           self.dictation_target or dictation_intent(command, False) else
                           self._run_agent(command, runtime, autonomous=autonomous))
            finish_stage="interrupted" if self.stop_event.is_set() else outcome.status
            if finish_stage in {"error", "incomplete", "unverified", "handoff", "handoff_clipboard", "answered"}:
                finish_detail=outcome.summary
        except Exception as e:
            finish_detail=friendly_error(e)
            self.log(f"実行エラー: {finish_detail}")
        finally:
            if profile is not None:
                self.log(profile.summary(voice=False, status=finish_stage))
            self.mark_idle(finish_stage, finish_detail)

    def mark_idle(self, stage="idle", detail=None):
        def reset():
            self.busy=False
            self.phase=stage
            self._status_detail=detail
            next_record = self._record_after_cancel
            self._record_after_cancel = False
            self._apply_phase()
            if getattr(getattr(self, "handsfree", None), "enabled", False):
                # Only the finished worker can clear its cancellation flag.
                self.stop_event.clear()
                self._schedule_handsfree_resume()
            elif next_record and self.stop_event.is_set():
                # Old worker has exited. Cancellation may have terminated with
                # an error; either way the old worker is no longer running.
                # Never open the microphone while two tasks could overlap.
                self.root.after_idle(self.toggle_record)
        self.ui.post(reset)

    def confirm(self, message):
        """Ask on the Tk thread, then return focus to the controlled window."""
        result = []
        finished = threading.Event()
        target = self.windows.foreground_hwnd()

        def show():
            try:
                self.phase="confirming"
                self._apply_phase()
                result.append(messagebox.askyesno(
                    "操作の確認 · VoicePilot", message,
                    parent=self.mini if self.mini and self.mini.winfo_exists() else self.root
                ))
            finally:
                self.phase="acting"
                self._apply_phase()
                if target:
                    self.windows.focus_handle(target)
                finished.set()

        self.ui.post(show)
        return finished.wait(timeout=90) and bool(result and result[0])

    def _handle_voice_interrupt(self, intent: str, gate: ActionGate):
        """Called on the interrupt microphone worker, never directly on Tk."""
        if self.stop_event.is_set() or getattr(self, "_closing", False):
            return
        if intent == "pause":
            gate.pause()
            self.ui.post(lambda: (self.log("一時停止しました。「続けて」で再開できます"),
                                  self._apply_phase()))
        elif intent == "resume":
            gate.resume()
            self.ui.post(lambda: (self.log("音声で作業を再開しました"), self._apply_phase()))
        elif intent in {"cancel", "end"}:
            # Cancellation is cooperative: an in-flight Windows/UIA/LLM call
            # may already have committed. Do not advertise an instant rollback.
            self.stop_event.set()
            gate.resume()
            if intent == "end":
                self.handsfree.stop()  # invalidate scheduled rearm
            self.ui.post(lambda: (self.log("音声で中止を要求しました。進行中の操作は完了済みの可能性があります"),
                                  self._apply_phase()))

    def _run_agent(self,command,runtime,autonomous=False):
        self.set_phase("acting")
        addressed = extract_chatgpt_request(command)
        if addressed is not None:
            return self._run_chatgpt(addressed)
        self.restore_target()
        planner=Planner(runtime["provider"],runtime["key"],runtime["gemini_model"],runtime["ollama_model"])
        # Explicit screen questions are read-only. Never execute an AI-suggested
        # click, hotkey, file operation or browser navigation for these requests.
        if screen_question(command):
            observer = VoiceAgent(self.bridge, self.windows, planner, self.log, self.confirm,
                                  runtime["allow_cloud_ui"], self.stop_event,
                                  session=self.session, allow_cloud_image=runtime["allow_cloud_image"])
            assistant = ScreenAssistant(
                bridge=self.bridge, windows=self.windows, planner=planner,
                context_provider=observer._context, stop_event=self.stop_event,
                allow_cloud_ui=runtime["allow_cloud_ui"],
                allow_cloud_image=runtime["allow_cloud_image"])
            try:
                answer = assistant.answer(command)
            except InterruptedError:
                return RunOutcome("interrupted", "画面の説明を中止しました")
            except PermissionError as exc:
                return RunOutcome("incomplete", str(exc))
            self.log("画面の説明：" + answer)
            return RunOutcome("answered", answer)
        gate = None
        monitor = None
        # Never open a background microphone without explicit hands-free opt-in.
        if self.handsfree.enabled and self.recognizer is not None:
            gate = ActionGate()
            self._action_gate = gate
            monitor = VoiceInterruptMonitor(
                recognize=self.recognizer.transcribe,
                on_intent=lambda intent: self._handle_voice_interrupt(intent, gate),
                gate=gate,
                on_error=lambda message: self.log(message),
            )
            self._interrupt_monitor = monitor
            monitor.start()
        try:
            agent=VoiceAgent(self.bridge,self.windows,planner,self.log,self.confirm,
                             runtime["allow_cloud_ui"],self.stop_event,
                             max_steps=(28 if autonomous else 12),
                             session=self.session,allow_cloud_image=runtime["allow_cloud_image"],
                             action_gate=gate, autonomous=autonomous)
            return agent.run(command)
        finally:
            # The regular microphone must not resume until this monitor has
            # been asked to stop and the previous task has exited.
            if monitor is not None:
                monitor.close()
            if getattr(self, "_interrupt_monitor", None) is monitor:
                self._interrupt_monitor = None
                self._action_gate = None

    def abort(self, *, keep_record_request=False):
        self.speaker.stop()
        if not keep_record_request and getattr(getattr(self, "handsfree", None), "enabled", False):
            self.stop_handsfree("停止ボタンでハンズフリーを終了しました")
        if not keep_record_request:
            self._record_after_cancel = False
        if not self.busy and not self.recording:
            if getattr(self, "dictation_target", None) is not None:
                self.dictation_target = None
                self._apply_phase()
                self.log("文章入力モードを終了しました")
            return
        self.stop_event.set()
        gate = getattr(self, "_action_gate", None)
        if gate is not None:
            gate.resume()
        monitor = getattr(self, "_interrupt_monitor", None)
        if monitor is not None:
            monitor.request_stop()
        if not keep_record_request and getattr(self, "dictation_target", None) is not None:
            self.dictation_target = None
        if self.recording:
            self._stop_record(discard=True)
            return
        self.set_phase("cancelling")
        self.log("中止を要求しました。AIへの通信中は停止まで少し時間がかかる場合があります")

    def close(self):
        self._closing = True
        self.handsfree.stop()
        monitor = getattr(self, "_interrupt_monitor", None)
        if monitor is not None:
            monitor.request_stop()
        gate = getattr(self, "_action_gate", None)
        if gate is not None:
            gate.resume()
        self._record_after_cancel = False
        self.dictation_target = None
        self.speaker.close()
        self.stop_event.set()
        if self.recording:self._stop_record(discard=True)
        self.ui.shutdown()
        self.root.destroy()
        self.bridge.close()


def main():
    if sys.platform != "win32":
        raise SystemExit("VoicePilotの画面操作はWindows 11で実行してください")
    # Running elevated would allow synthetic input to touch privileged tools.
    # This application never needs admin rights for its supported operations.
    if current_process_elevated():
        raise SystemExit("安全のため管理者権限では起動できません。通常のユーザー権限で実行してください")
    # Avoid mismatch between screenshot pixel coordinates and Windows mouse
    # coordinates when the display is scaled to e.g. 125% / 150%.
    import ctypes
    try:
        ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except (AttributeError, OSError):
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except (AttributeError, OSError):
            pass
    root=tk.Tk()
    VoicePilotApp(root)
    root.mainloop()

if __name__=="__main__":main()