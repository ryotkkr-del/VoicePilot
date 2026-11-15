## v0.23.2: Confirm fast-path Undo, and test PRs before release

An explicit Japanese "undo this app's edit" command previously bypassed the approval that the AI-planned Ctrl+Z action already required. The deterministic shortcut now asks for confirmation **before** sending Ctrl+Z, because undo may revert the user's own work. Refusal or cancellation prevents the key action. Ctrl+V still requires approval; Ctrl+C does not gain an unnecessary prompt. Mock-based regressions cover these boundaries without touching a real desktop.

GitHub Actions now runs the Windows test suite on pull requests, while publishing remains disabled for pull-request events. Local speech, browser and desktop capabilities are retained. Real-machine operation accuracy, interruption reliability and CPU usage have not yet been measured.


# VoicePilot

**Voice-controlled Windows PC assistant (prototype)**

VoicePilot is a Windows 11 + Chrome experiment for controlling desktop applications, browser pages, and files using Japanese speech. The goals are **flexible control, no mandatory paid API, and Windows-wide coverage**.

> **Prototype warning:** This project has not been verified on a real Windows machine end-to-end. Automated tests passing do not guarantee that browser and desktop actions are reliable or safe. Do not allow unattended operations on sensitive or high-impact data.

## v0.23.1: Windows output compatibility and release consistency

Fixed the optional microphone diagnostic tests on Windows hosts configured with legacy console encodings. The diagnostic CLI configures UTF-8 output when run directly. Release publishing checks that the workflow commit is still `main` HEAD before creating a release, avoiding stale assets from queued workflow runs. No change to autonomous permissions.

## v0.23: safer screen context + opt-in local microphone diagnostic

**Privacy fix:** In v0.22, foreground terminal/admin/unknown processes were denied UIA tree traversal but could still leak their window title and the titles of other open apps into the AI context. Now protected snapshots and quick-state observations return **no titles, controls or window identifiers**; the title enumerator is not even invoked for denied foregrounds. If the foreground changes during UIA scanning or the Chrome-DOM fallback, the partial snapshot is discarded and cached control targets are cleared.

**Wrong-target fix:** Screen explanations re-check the foreground immediately before the AI provider call, **including Gemini text-only mode**. Browser page edits/navigation (click/fill/scroll/back/forward/tab switch) refuse to run after Chrome loses foreground; explicit open/search still work. This reduces unintended cross-app actions without claiming to eliminate time-of-check/time-of-use races.

**Hardware diagnostics:** `run_tests.bat` now offers two *separate opt-in* exercises: the scratch-only Notepad input test from v0.22 and a new microphone-to-local-Whisper test. The microphone test requires the user to type `YES`, captures at most eight seconds into RAM, and checks one fixed phrase without printing or saving the recognized transcript or issuing an AI command. Its elapsed recognition time is reported locally. **Both tests are disabled unless the user actively opts in.** Initial model download may require internet; the recorded audio is not uploaded.

The standard CI suite runs **255 Python unit tests** (one Windows-only test skipped on Linux) and Chrome JavaScript tests; it does not conduct real microphone or desktop automation on the user's PC. The physical-laptop tests still require user initiation and review. No new paid services, elevation, file deletion, arbitrary shell commands, or unrestricted AI permissions were added. Signed update authenticity and full isolation remain unimplemented.

## v0.22: Windows safety verification and fail-closed desktop boundaries

**Security fixes:** Chrome foreground detection checks the actual process executable (`chrome.exe`), rather than a spoofable window title. Elevated VoicePilot instances are refused at app startup; synthetic input also checks privilege before acting. The agent and read-only screen assistant reject screenshots/accessible content from blocked or unidentifiable foreground processes, and discard frames if focus changes while a screenshot is captured. An interrupted view is never forwarded to a model as fallback text.

**Automated tests:** `python -m unittest discover -s tests` now also runs Windows-specific, read-only Win32 process probes and real rename/move/overwrite-rejection tests confined to `TemporaryDirectory` scratch files (all other user roots are mocked). These Windows CI checks do **not** use real desktop clicks, microphones, keyboard events or privileged actions. `run_tests.bat` additionally prints a short local safety preflight with no personal filenames or screenshots in the report. It optionally offers a **separately consented, test-only Notepad input** exercise: creates a blank document under `%TEMP%`, requires explicit `YES`, checks the foreground executable and scratch-window title, and uses the dictation focus guard before typing one harmless string. It does not save, close or click any real application.

**Limits:** Windows CI process APIs + scratch NTFS tests are real Windows-platform checks, but are not a physical-laptop, live-microphone or actual user-desktop certification. The Notepad exercise must be run by the user and can be refused by the accessibility guard on unsupported editor configurations. Model errors, other app behavior, incomplete interruption/undo, time-of-check/time-of-use races and unsigned GitHub Releases still present risk. Do not run VoicePilot as administrator or entrust it with unattended important operations. This hardening does not imply full isolation from the PC.

To run the safe local check on Windows, first launch `setup_and_run.bat` as a normal user and then double-click `run_tests.bat`. The default checks do not send keyboard/mouse input. If the optional Notepad exercise is offered, you can decline it with `N`.

## v0.21: opt-in bounded multi-step tasks & PC safety hardening

Use **長い作業（確認付き）** to explicitly start a longer UI task after a confirmation dialog. This mode reuses the existing desktop/browser action executor. It is capped at **28 planning iterations, 12 potentially effectful PC operations, and 240 seconds**, with a checkpoint after about six operations and separate confirmation for consequential actions. An ambiguous side effect, execution exception, rejection or cancellation halts the task; it never blindly retries uncertain actions. Progress metadata (action names/status only) stays in RAM. No crash-resume or automatic undo is claimed.

**Safety fixes:** literal Unicode input replaces `pywinauto.send_keys` for AI-proposed text; input focus and selection are verified via UI Automation. Terminal/process identity checks inspect the actual Windows process (not just a spoofable window title). Start Menu shortcuts are resolved to local installed executables and cannot pass arbitrary arguments or scripts. Browser URL validation also rejects alternative numeric encodings of private loopback addresses. Existing archive verification, user-folder restrictions, action approval and stop controls remain.

**Do not consider this safe for unattended critical work.** PC automation can cause side effects in real applications; Windows hardware interoperability, microphone success and process privilege boundaries are not yet tested end-to-end. No elevated privileges or arbitrary shell/registry/delete actions are added. GitHub asset SHA-256 checks do not provide an independent developer signing key. Test in a disposable Windows user account or VM using dummy files, without running as administrator. Some legitimate applications and custom shortcuts may now be refused to fail closed.

## Features

- Japanese speech recognition using faster-whisper (local)
- Windows UI Automation and Chrome extension for UI actions
- Optional Ollama (local) / Gemini (free-tier limits apply) for planning
- Optional ChatGPT Web handoff **for drafting only**; it does not send messages automatically or use Codex/Work
- Action confirmations, cancellation, and limited voice feedback
- **v0.17: explicit voice dictation mode** — place the caret in a supported editor, click "文章入力" or say "文章入力を始めて", then dictate via Ctrl+Alt+Space. Only the selected editor receives literal text. Say "入力終了" to quit. No AI API is used for dictation.

- **v0.18: opt-in sequential hands-free sessions** — click **ハンズフリー開始** once, then speak multiple separate commands without pressing the hotkey each time. The microphone pauses during transcription, UI operations, and SAPI voice feedback, then rearms when quiet. Stop by saying **ハンズフリー終了** or using the stop button. Sessions time out after three minutes without a recognized utterance and do not survive restart. No external voice API, wake-word listener, or extra inference model is required.

## Read-only screen understanding (v0.20)

Say **「今の画面を説明して」**, **「このエラーは何？」** or **「これって何？」** (with the cursor positioned over a control), or click **「今の画面を説明」**. VoicePilot examines the active Chrome page's visible headings/paragraphs, Windows accessibility controls, and an optional one-shot screenshot, then returns a Japanese explanation **without executing UI actions**. Chrome text is bounded and explicitly excludes forms, editors and hidden elements. The cursor label is read-only, and editor values are redacted. This is a best-effort explanation, not verified recognition of arbitrary graphics.

Default **Ollama uses the local model**. Gemini text sharing requires `allow_cloud_ui`, and a screenshot is **never** sent to Gemini unless the separate `allow_cloud_image` permission is enabled. No screenshots are saved, and ordinary PC commands still use the existing confirmed action path. The answer is shown in the temporary activity log; voice feedback announces that it is available, but does not read private screen text aloud. Windows real-machine validation is still outstanding.

## Hands-free listening (v0.19)

1. Start VoicePilot and click **ハンズフリー開始** (or **連続ON** in the compact view).
2. Speak commands one at a time, leaving a short silence between utterances. The existing local Whisper model converts speech into instructions; existing confirmation rules still apply.
3. After an action finishes and voice feedback ends, VoicePilot automatically listens for the next utterance. Dictation mode may be used within a hands-free session.
4. Say **ハンズフリー終了**, click **ハンズフリー終了**, or use **停止** to stop. An inactive session shuts down after 3 minutes.

**v0.19: Voice interruption during PC tasks.** While an explicitly enabled hands-free task is executing, a separate local microphone listens only for short control phrases: **待って** pauses at the next action boundary, **続けて** resumes, and **やめて** requests cancellation. **ハンズフリー終了** ends listening and cancels. These commands cannot undo already executed actions or force a currently blocking Windows/API call to stop instantly. A pause times out after 120 seconds. `Ctrl+Alt+Esc` remains an immediate keyboard fallback.

**Limits:** This is not a continuously listening wake-word assistant. A secondary microphone stream runs only during an opted-in active task, and only recognized whole-phrase controls can affect it. Other spoken words during execution are ignored. The microphone uses bounded audio buffering and a low-cost energy threshold, not a separate learned VAD. It can still detect environmental speech or misrecognize audio; do not rely on it for unattended high-impact actions. For speaker echo protection, listening resumes only after built-in spoken feedback is finished; headphones are recommended when other audio is playing. Physical Windows microphone and application interoperability have not yet been verified.

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

See [Japanese documentation / README_JA.md](README_JA.md) for functionality and update history through v0.23.

## Security and permissions

- Web content and UI labels are untrusted; review high-impact operations.
- Keep the local bridge bound to localhost, and keep pairing secrets private.
- Automation may be blocked by Windows privilege boundaries and browser security restrictions.
- Do not submit content or change third-party services without authorization.

## License

No license has been selected yet. The code is viewable, but no permission to reuse or redistribute it is granted by default until a license is added.