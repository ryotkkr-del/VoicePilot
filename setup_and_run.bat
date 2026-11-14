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
if not exist ".venv\.deps_v011_ok" (
  echo [VoicePilot] 初回セットアップを実行します。ネット接続が必要です。
  ".venv\Scripts\python.exe" -m pip install --upgrade pip
  ".venv\Scripts\python.exe" -m pip install -r requirements.txt
  if errorlevel 1 (
    echo インストールに失敗しました。Pythonバージョンとネット接続を確認してください。
    pause
    exit /b 1
  )
  echo installed>".venv\.deps_v011_ok"
)
".venv\Scripts\python.exe" -m app.main
if errorlevel 1 pause
endlocal