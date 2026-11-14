@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo Python環境がありません。setup_and_run.batを一度実行してください。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" voicepilot_bootstrap.py rollback
if errorlevel 1 echo 復元に失敗しました。元のフォルダを保管して確認してください。
pause
endlocal