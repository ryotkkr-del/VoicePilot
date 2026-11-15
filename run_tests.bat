@echo off
chcp 65001 >nul
cd /d "%~dp0"
if not exist ".venv\Scripts\python.exe" (
  echo 先に setup_and_run.bat でセットアップを実行してください。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -m unittest discover -s tests -v
if errorlevel 1 goto failure
where node >nul 2>&1
if errorlevel 1 (
  echo Node.js がないためChromeの動作テストはスキップしました。
) else (
  node --check chrome-extension\background.js
  if errorlevel 1 goto failure
  for %%F in (tests_js\*.test.js) do (
    node "%%F"
    if errorlevel 1 goto failure
  )
)
echo.
echo Windowsの安全診断を実行します。個人ファイルや画面の操作はしません。
".venv\Scripts\python.exe" tests\windows_safety_check.py
if errorlevel 1 goto failure
echo.
choice /C YN /N /M "任意のメモ帳入力テストも実行しますか？ (Y/N): "
if errorlevel 2 goto microphone
".venv\Scripts\python.exe" tests\windows_safety_check.py --interactive-notepad
if errorlevel 1 goto failure
:microphone
echo.
choice /C YN /N /M "ローカル音声認識も安全に試しますか？ (Y/N): "
if errorlevel 2 goto done
".venv\Scripts\python.exe" tests\windows_safety_check.py --interactive-microphone
if errorlevel 1 goto failure
:done
echo テスト完了
pause
exit /b 0
:failure
echo テストでエラーが出ました。
pause
exit /b 1