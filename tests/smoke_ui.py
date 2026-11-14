"""Developer-only GUI smoke test, run under a virtual X display."""
import tkinter as tk
import threading
from unittest.mock import Mock
from pathlib import Path
from PIL import ImageGrab
from app.main import VoicePilotApp

root=tk.Tk()
app=VoicePilotApp.__new__(VoicePilotApp)
app.root=root
app.config={"provider":"ollama", "ollama_model":"gemma3:4b", "whisper_model":"small", "auto_stop":True, "voice_feedback":True,"token":"test-token"}
app.mini=None
app.busy=False
app.recording=False
app._record_after_cancel=False
app.phase='idle'
app._status_detail=None
app.speaker=Mock()
app.bridge=Mock(connected=False,error=False)
app.stop_event=threading.Event()
app.windows=Mock()
app._text_at_record_stop=''
app.build_ui()
root.update_idletasks()
root.update()
print('normal root:', root.winfo_width(), root.winfo_height(), 'scrollbar:', app.scrollbar.winfo_ismapped())
app.toggle_settings()
root.update_idletasks()
root.update()
print('settings root:', root.winfo_width(), root.winfo_height(), 'scrollbar:', app.scrollbar.winfo_ismapped(), 'scrollregion', app.scroll_canvas.bbox('all'))
app.scroll_canvas.yview_moveto(1)
root.update()
print('scroll bottom:', app.scroll_canvas.yview())
ImageGrab.grab(bbox=(0,0,800,768)).save('/mnt/data/VoicePilot-v0.15/gui_smoke_settings.png')
app.toggle_settings()
root.update()
print('back normal:',root.winfo_width(), root.winfo_height())
root.destroy()