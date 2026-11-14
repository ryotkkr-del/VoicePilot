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

See [Japanese documentation / README_JA.md](README_JA.md) for functionality and update history through v0.16.

## Security and permissions

- Web content and UI labels are untrusted; review high-impact operations.
- Keep the local bridge bound to localhost, and keep pairing secrets private.
- Automation may be blocked by Windows privilege boundaries and browser security restrictions.
- Do not submit content or change third-party services without authorization.

## License

No license has been selected yet. The code is viewable, but no permission to reuse or redistribute it is granted by default until a license is added.