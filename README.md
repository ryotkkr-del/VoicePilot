# VoicePilot

**Voice-controlled Windows PC assistant (prototype)**

VoicePilot is a Windows 11 + Chrome experiment for controlling desktop applications, browser pages, and files using Japanese speech. The goals are **flexible control, no mandatory paid API, and Windows-wide coverage**.

> **Prototype warning:** This project has not been verified on a real Windows machine end-to-end. Automated tests passing do not guarantee that browser and desktop actions are reliable or safe. Do not allow unattended operations on sensitive or high-impact data.

## Features

- Japanese speech recognition using faster-whisper (local)
- Windows UI Automation and Chrome extension for UI actions
- Optional Ollama (local) / Gemini (free-tier limits apply) for planning
- Optional ChatGPT Web handoff **for drafting only**; it does not send messages automatically or use Codex/Work
- Action confirmations, cancellation, and limited voice feedback
- **v0.17: explicit voice dictation mode** — place the caret in a supported editor, click "文章入力" or say "文章入力を始めて", then dictate via Ctrl+Alt+Space. Only the selected editor receives literal text. Say "入力終了" to quit. No AI API is used for dictation.

- **v0.18: opt-in sequential hands-free sessions** — click **ハンズフリー開始** once, then speak multiple separate commands without pressing the hotkey each time. The microphone pauses during transcription, UI operations, and SAPI voice feedback, then rearms when quiet. Stop by saying **ハンズフリー終了** or using the stop button. Sessions time out after three minutes without a recognized utterance and do not survive restart. No external voice API, wake-word listener, or extra inference model is required.

## Hands-free listening (v0.18)

1. Start VoicePilot and click **ハンズフリー開始** (or **連続ON** in the compact view).
2. Speak commands one at a time, leaving a short silence between utterances. The existing local Whisper model converts speech into instructions; existing confirmation rules still apply.
3. After an action finishes and voice feedback ends, VoicePilot automatically listens for the next utterance. Dictation mode may be used within a hands-free session.
4. Say **ハンズフリー終了**, click **ハンズフリー終了**, or use **停止** to stop. An inactive session shuts down after 3 minutes.

**Limits:** This is **sequential** hands-free, not a continuously listening wake-word assistant. Voice input is intentionally suspended **while the current command runs**, so spoken interruptions during a long task cannot be processed; `Ctrl+Alt+Esc` remains available to abort. The microphone uses bounded audio buffering and a low-cost energy threshold, not a separate learned VAD. It can still detect environmental speech or misrecognize audio; do not rely on it for unattended high-impact actions. For speaker echo protection, listening resumes only after built-in spoken feedback is finished; headphones are recommended when other audio is playing. Physical Windows microphone and application interoperability have not yet been verified.

## Voice typing (v0.17)

Focus a text field in Chrome (with the extension connected), or a supported Windows UI Automation text control in Word/VS Code/Notepad. Start **文章入力**, speak one phrase at a time using `Ctrl + Alt + Space`, then end with **入力終了**. Standalone "改行", "句点", "読点", "スペース" are formatting commands. With v0.18, **ハンズフリー開始** permits consecutive utterances within a limited, explicit session.

The insertion path never clears existing editor content, does not simulate paste or hotkeys, does not automatically submit forms, and refuses to change input target. It checks common sensitive fields. Rich editors and apps that hide the caret from accessibility APIs may reject dictation; test on non-sensitive documents first. Windows UI Automation control insertion has not been validated on a physical Windows PC.

## Getting started (Windows 11)

1. Install **Python 3.11** and optionally **Ollama**.
2. Run `setup_and_run.bat` (creates a local `.venv`).
3. In Chrome, visit `chrome://extensions`, enable Developer mode, and **Load unpacked** from `chrome-extension/`.
4. Pair the extension with the local VoicePilot app using the code shown in the app.
5. Start speech input with `Ctrl + Alt + Space`; stop with `Ctrl + Alt + Esc`.

The application stores settings locally under `%APPDATA%\VoicePilot\config.json`. Do **not** commit that file, API keys, personal recordings, or AI models.

## Automatic updates (v0.16+)

- Install **v0.16 once** by extracting its ZIP, running `setup_and_run.bat`, and loading `chrome-extension/` from that folder. A v0.15 folder cannot update itself.
- The application checks this repository's latest **published GitHub Release** quietly in the background (no Codex, Work, or paid APIs).
- It downloads a matching `VoicePilot-vX.Y.Z.zip` and verifies GitHub's SHA-256 asset digest. It rejects path traversal, symlinks, unexpected files, and downgrades before staging under `%LOCALAPPDATA%\VoicePilot\updates`.
- At **next launch**, `setup_and_run.bat` applies the staged update before starting the app, preserving `%APPDATA%\VoicePilot\config.json` and the Python virtual environment. If interrupted it restores the backup.
- If an updated version has problems, close VoicePilot and run `rollback_update.bat`. Rolled-back versions are blocked from being automatically redownloaded.
- Chrome must be loaded unpacked from the *same stable installation folder*. The extension can request its own reload after an update; the first migration from v0.15 may require manually loading the new extension once.
- A GitHub Actions workflow builds and publishes a *new version* only after Windows unit tests pass. Changes without a `VERSION` bump do not create a replacement Release.
- The trusted bootstrap script is pinned and not hot-replaced. Major bootstrap upgrades require a manual installer update. This project is still a prototype; test on Windows before relying on unattended updates.

## Tests

Run `run_tests.bat` on Windows. Python and Node.js are required for the complete suite. On other systems, run `python -m unittest discover -s tests` for applicable tests.

## Documentation

See [Japanese documentation / README_JA.md](README_JA.md) for functionality and update history through v0.18.

## Security and permissions

- Web content and UI labels are untrusted; review high-impact operations.
- Keep the local bridge bound to localhost, and keep pairing secrets private.
- Automation may be blocked by Windows privilege boundaries and browser security restrictions.
- Do not submit content or change third-party services without authorization.

## License

No license has been selected yet. The code is viewable, but no permission to reuse or redistribute it is granted by default until a license is added.