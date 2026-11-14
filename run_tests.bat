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
echo テスト完了
pause
exit /b 0
:failure
echo テストでエラーが出ました。
pause
exit /b 1