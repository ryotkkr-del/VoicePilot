"""VoicePilot: a compact Windows microphone panel and UI thread coordinator."""
from __future__ import annotations

import gc
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
from .planner import Planner
from .audio import Microphone, SpeechRecognizer
from .bridge import BrowserBridge
from .hotkey import register_windows_hotkey
from .win_actions import WindowsController
from .ux import presentation, meter_level, friendly_error
from .ui_dispatch import UIDispatcher
from .voice_feedback import SpeechFeedback, result_announcement
from .chatgpt_handoff import extract_chatgpt_request, prepare_chatgpt_handoff

CONFIG_DIR = Path(os.environ.get("APPDATA", str(Path.home()))) / "VoicePilot"
CONFIG_FILE = CONFIG_DIR / "config.json"
DEFAULTS = {
    "token": "", "provider": "ollama", "gemini_model": "gemini-2.5-flash",
    "ollama_model": "gemma3:4b", "whisper_model": "small", "allow_cloud_ui": False,
    "auto_stop": True, "silence_seconds": 1.4, "allow_cloud_image": False,
    "voice_feedback": True,
}
BG, WHITE, INK = "#F6F7FA", "#FFFFFF", "#19222E"
MUTED, ACCENT, BORDER = "#64748B", "#2563EB", "#E2E8F0"
FONT = "Yu Gothic UI"
# Whisper is loaded on first use and released after inactivity.
# This is a memory policy, not a reduction in speech recognition quality.
SPEECH_MODEL_IDLE_SECONDS = 75


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
        self.bridge = BrowserBridge(self.config["token"])
        self.bridge.start()
        self.mic = Microphone()
        self.recognizer: SpeechRecognizer | None = None
        self._last_transcription_finished = 0.0
        self.windows = WindowsController()
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
        if sys.platform == "win32":
            register_windows_hotkey(
                lambda: self.ui.post(self.toggle_record),
                lambda: self.ui.post(self.abort),
                on_error=self.log,
            )
        self.log("起動しました。録音ボタンか Ctrl+Alt+Space で話してください。")

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

        talk = self.card(shell, 10)
        row = tk.Frame(talk,bg=WHITE)
        row.pack(fill="x")
        self.rec_button = self.button(row,"●  話す",self.toggle_record,True)
        self.rec_button.pack(side="left")
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

        # Keep the everyday UI sparse.  Settings are one click away.
        self.settings_card.pack_forget()
        pair=self.card(shell)
        self.pair_card=pair
        tk.Label(pair,text="Chrome拡張機能",font=(FONT,10,"bold"),bg=WHITE,fg=INK).pack(side="left")
        self.button(pair,"接続コードをコピー",self.copy_token).pack(side="right")

        logbar=tk.Frame(shell,bg=BG)
        logbar.pack(fill="x",pady=(12,6))
        tk.Label(logbar,text="操作ログ",font=(FONT,10,"bold"),bg=BG,fg=INK).pack(side="left")
        tk.Label(logbar,text="音声・実行結果を表示（ファイル保存しません）",font=(FONT,8),bg=BG,fg=MUTED).pack(side="right")
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
                            "voice_feedback":self.voice_feedback.get()})
        save_config(self.config)

    def refresh_connection(self):
        if self.bridge.error:
            self.conn_label.configure(text="● Chrome接続エラー",fg="#B91C1C")
        elif self.bridge.connected:
            self.conn_label.configure(text="● Chrome接続中",fg="#15803D")
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
        self.abort_button.configure(state="normal" if p.can_stop else "disabled")
        # Disable actions that would otherwise silently be ignored while busy.
        entry_action_state = "disabled" if self.busy or recording else "normal"
        self.send_button.configure(state=entry_action_state)
        self.chatgpt_button.configure(state=entry_action_state)
        if self._record_after_cancel:
            hint = "現在の操作を止めたあと、録音を開始します"
            self.status_hint.configure(text=hint)
        self._refresh_record_buttons()
        if self.mini and self.mini.winfo_exists():
            self.mini_status.configure(text=p.title, fg=colors[p.tone])
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
        self.recording=True
        self.record_started=time.monotonic()
        self._refresh_record_buttons()
        self.set_phase("listening")
        self.root.after(220,self.check_record_time)

    def _stop_record(self,discard=False):
        if not self.recording:return
        self.recording=False
        self._text_at_record_stop = self.command.get()
        self._refresh_record_buttons()
        try:samples=self.mic.stop()
        except Exception as e:
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
        threading.Thread(target=self.transcribe_then_execute,args=(samples,runtime),daemon=True).start()

    def check_record_time(self):
        if not self.recording:return
        a=self.mic.activity()
        elapsed=a["elapsed"]
        self.phase = "listening"
        self._status_detail = None
        self._apply_phase(heard_voice=a["heard_voice"], elapsed=elapsed)
        # Mic level updates are derived from the existing recording callback.
        lit = meter_level(a["level"], a["threshold"])
        for i, seg in enumerate(self.meter_segments):
            seg.configure(bg="#C2410C" if i < lit else BORDER)
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
        self.root.after(220,self.check_record_time)

    def release_idle_model(self):
        """Free the expensive ASR model after a quiet interval.

        Resource teardown happens off the GUI thread. No microphone stream or
        inference model is kept running while the user is inactive.
        """
        if (not self.busy and not self.recording and self.recognizer is not None
                and self._last_transcription_finished > 0
                and time.monotonic() - self._last_transcription_finished >= SPEECH_MODEL_IDLE_SECONDS):
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
                "allow_cloud_image":self.cloud_image_permission.get()}

    def transcribe_then_execute(self,samples,runtime):
        finish_stage="error"
        finish_detail=None
        verified_action=None
        try:
            model=runtime["whisper_model"]
            if self.recognizer is None or self.recognizer.model_name!=model:
                self.recognizer=SpeechRecognizer(model)
                self.log(f"音声モデル {model} を読み込んでいます（初回はダウンロード）")
            command=self.recognizer.transcribe(samples)
            if self.stop_event.is_set():
                finish_stage="interrupted"
                return
            if not command:
                self.log("音声を認識できませんでした。もう一度試してください")
                finish_stage="no_speech"
                return
            self.show_transcript(command)
            self.log(f"あなた：{command}")
            outcome=self._run_agent(command,runtime)
            finish_stage="interrupted" if self.stop_event.is_set() else outcome.status
            verified_action=outcome.verified_action if finish_stage == "completed" else None
            if finish_stage in {"error", "incomplete", "unverified", "handoff", "handoff_clipboard"}:
                finish_detail=outcome.summary
        except Exception as e:
            finish_detail=friendly_error(e)
            self.log(f"音声処理エラー: {finish_detail}")
            finish_stage="error"
        finally:
            self._last_transcription_finished = time.monotonic()
            self.mark_idle(finish_stage, finish_detail)
            self._speak_result(finish_stage, verified_action)

    def send_text(self):
        if self.busy or self.recording:return
        command=self.command.get().strip()
        if not command:
            self.log("音声で話すか、入力欄に指示を入力してください")
            return
        self.busy=True
        self.stop_event.clear()
        runtime=self.runtime_settings()
        self.log(f"あなた：{command}")
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

    def run_text_task(self,command,runtime,force_chatgpt=False):
        finish_stage="error"
        finish_detail=None
        try:
            outcome=self._run_chatgpt(command) if force_chatgpt else self._run_agent(command,runtime)
            finish_stage="interrupted" if self.stop_event.is_set() else outcome.status
            if finish_stage in {"error", "incomplete", "unverified", "handoff", "handoff_clipboard"}:
                finish_detail=outcome.summary
        except Exception as e:
            finish_detail=friendly_error(e)
            self.log(f"実行エラー: {finish_detail}")
        finally:
            self.mark_idle(finish_stage, finish_detail)

    def mark_idle(self, stage="idle", detail=None):
        def reset():
            self.busy=False
            self.phase=stage
            self._status_detail=detail
            next_record = self._record_after_cancel
            self._record_after_cancel = False
            self._apply_phase()
            if next_record and self.stop_event.is_set():
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

    def _run_agent(self,command,runtime):
        self.set_phase("acting")
        addressed = extract_chatgpt_request(command)
        if addressed is not None:
            return self._run_chatgpt(addressed)
        self.restore_target()
        planner=Planner(runtime["provider"],runtime["key"],runtime["gemini_model"],runtime["ollama_model"])
        agent=VoiceAgent(self.bridge,self.windows,planner,self.log,self.confirm,
                         runtime["allow_cloud_ui"],self.stop_event,max_steps=12,
                         session=self.session,allow_cloud_image=runtime["allow_cloud_image"])
        return agent.run(command)

    def abort(self, *, keep_record_request=False):
        self.speaker.stop()
        if not keep_record_request:
            self._record_after_cancel = False
        if not self.busy and not self.recording:
            return
        self.stop_event.set()
        if self.recording:
            self._stop_record(discard=True)
            return
        self.set_phase("cancelling")
        self.log("中止を要求しました。AIへの通信中は停止まで少し時間がかかる場合があります")

    def close(self):
        self._record_after_cancel = False
        self.speaker.close()
        self.stop_event.set()
        if self.recording:self._stop_record(discard=True)
        self.ui.shutdown()
        self.root.destroy()
        self.bridge.close()


def main():
    if sys.platform != "win32":
        raise SystemExit("VoicePilotの画面操作はWindows 11で実行してください")
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