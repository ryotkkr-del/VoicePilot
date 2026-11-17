@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo [VoicePilot] Python 3.11 仮想環境の作成...
  py -3.11 -m venv .venv
  if errorlevel 1 (
    echo Python 3.11 をインストールしてください: https://www.python.org/downloads/
    pause
    exit /b 1
  )
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 (
  echo [VoicePilot] この .venv はPython 3.11ではありません。安全のため起動しません。
  echo [VoicePilot] 個人データを確認したうえで .venv だけを削除し、Python 3.11で再作成してください。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" voicepilot_bootstrap.py apply
if errorlevel 1 (
  echo [VoicePilot] 更新の確認・復元に失敗しました。起動を停止します。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" voicepilot_bootstrap.py deps-needed
if errorlevel 1 (
  echo [VoicePilot] Pythonパッケージの変更を反映します...
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo [VoicePilot] 依存ライブラリのインストールに失敗しました。
    pause
    exit /b 1
  )
  ".venv\Scripts\python.exe" voicepilot_bootstrap.py deps-mark
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -m app.main
if errorlevel 1 pause
endlocal
