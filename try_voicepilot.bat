@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
echo [VoicePilot] 音声認識だけの安全試用モード
echo [VoicePilot] PC操作・Chrome操作・クラウドへの音声送信・自動更新は行いません。
echo.
if not exist ".venv\Scripts\python.exe" (
  echo [VoicePilot] このフォルダにPython 3.11の仮想環境を作成します。
  echo [VoicePilot] まだネットワーク通信はしません。
  py -3.11 -c "import sys" >nul 2>&1
  if errorlevel 1 (
    if not exist "%LocalAppData%\Programs\Python\Python311\python.exe" (
      echo Python 3.11をインストールしてください: https://www.python.org/downloads/
      pause
      exit /b 1
    )
    "%LocalAppData%\Programs\Python\Python311\python.exe" -m venv .venv
  ) else (
    py -3.11 -m venv .venv
  )
  if errorlevel 1 exit /b 1
)
".venv\Scripts\python.exe" -c "import sys; raise SystemExit(0 if sys.version_info[:2] == (3, 11) else 1)"
if errorlevel 1 (
  echo [VoicePilot] この .venv はPython 3.11ではありません。安全のため試用を開始しません。
  echo [VoicePilot] 個人データを確認したうえで .venv だけを削除し、Python 3.11で再作成してください。
  pause
  exit /b 1
)
".venv\Scripts\python.exe" -c "import numpy, sounddevice, faster_whisper"
if errorlevel 1 goto ASK_INSTALL
goto START_TRIAL

:ASK_INSTALL
echo.
echo [VoicePilot] 音声認識に必要な無料のライブラリが未導入です。
echo [VoicePilot] インストールを許可すると、PyPIからnumpy/sounddevice/faster-whisperと依存パッケージを取得します。
echo [VoicePilot] VoicePilot本体のWindows操作ライブラリは導入しません。
set "CONSENT="
set /p CONSENT="インストールする場合だけ INSTALL と入力してください: "
if /i not "%CONSENT%"=="INSTALL" (
  echo [VoicePilot] インストールせず終了しました。
  pause
  exit /b 0
)
".venv\Scripts\python.exe" -m pip install "numpy>=1.26,<3" "sounddevice>=0.5,<1" "faster-whisper>=1.1,<2"
if errorlevel 1 (
  echo [VoicePilot] 音声認識ライブラリを導入できませんでした。
  pause
  exit /b 1
)

:START_TRIAL
echo.
echo [VoicePilot] 音声認識テスト画面を開きます。録音はボタンを押すまで開始しません。
echo [VoicePilot] 認識モデルの初回ダウンロードも画面から明示的に許可する方式です。
".venv\Scripts\python.exe" -m app.first_trial
if errorlevel 1 (
  echo [VoicePilot] 試用モードを起動できませんでした。
  pause
  exit /b 1
)
endlocal
