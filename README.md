# VoicePilot

**Voice-controlled Windows PC assistant (prototype)**

VoicePilot is a Windows 11 + Chrome experiment for controlling desktop applications, browser pages, and files using Japanese speech. The goals are **flexible control, no mandatory paid API, and Windows-wide coverage**.

> **Prototype warning:** This project has not been verified on a real Windows machine end-to-end. Automated tests passing do not guarantee that browser and desktop actions are reliable or safe. Do not allow unattended operations on sensitive or high-impact data.

## v0.23.21: single-use approval before cloud screen disclosure (P0-3)

The stored Gemini image checkbox is **not** sufficient to upload a screenshot. Each model-initiated screenshot requires a fresh local GUI confirmation, and a captured image is discarded if the foreground window changes or the five-minute desktop permit expires before model dispatch.

For Gemini screen explanations, a fresh confirmation is required **before reading visible Chrome page paragraphs or taking the monitor screenshot**. Declining or having no confirmation mechanism fails closed. The current-screen answer is shown to the user but no longer copied into the diagnostic log. Ollama local-only behavior is unchanged.

This is not automatic privacy masking: screenshots may include notifications, other windows and secrets, and cloud-transferred data cannot be recalled. Windows real-device validation and an authentic signed release are still outstanding. See [cloud disclosure safety](docs/CLOUD_DISCLOSURE.md).

## v0.23.20: revoke in-progress dictation at each Windows input unit

Normal GUI dictation now rechecks the volatile desktop permit and stop event before restoring the target, after restoration, before any Chrome dictation request, and before **each UTF-16 input unit** during native Windows dictation. The existing pinned-editor guard remains. Chrome requests already in flight and input already delivered cannot be undone; short OS scheduling races remain. No new cloud data or disk logging is introduced.

This is a **mock-test-backed prototype change**, not a Windows hardware safety certification. The public ZIP is still independently unsigned. Continue to use the microphone-only `try_voicepilot.bat` for first trials; see [hardware acceptance](docs/P0_HARDWARE_ACCEPTANCE.md).

## v0.23.19: normal desktop GUI now starts locked by default

A new **volatile, five-minute, opt-in desktop authorization** now protects the regular VoicePilot GUI. On every launch it starts locked. Only an explicit click on the local GUI's permission button followed by confirmation permits the normal voice/text/agent/Chrome/dictation operations; neither config files, models, browser content nor voice can arm it. Stop (including keyboard and hands-free cancellation), closing or expiry revokes authorization. The shared fast/reference/AI dispatch boundary rechecks the permit before each effect, and direct dictation/handoff paths are separately guarded.

The regular GUI also declines to perform a background release check unless a revision-3 immutable bootstrap contains a developer key; the unsigned legacy bootstrap's pre-launch behavior is **not** magically corrected.

**Still not production-safe:** in-flight native side effects cannot be rolled back, the real Windows device is untested, and the public GitHub ZIP remains independently unsigned. Read [docs/DEFAULT_LOCKED_CONTROL.md](docs/DEFAULT_LOCKED_CONTROL.md) before trying. The separate `try_voicepilot.bat` microphone-only trial remains the recommended first test.

## v0.23.18: P0-2 opt-in scratch Notepad Escape stop and input deadline

The manually authorized **scratch-only Notepad diagnostic** now checks the physical Escape key before every UTF-16 input unit. Holding Escape cancels remaining simulated input; it also stops after 20 seconds or if key-state inspection fails. It retains the v0.23.17 pinned HWND / actual process / unique scratch document / focused editor checks. It does not install system-wide keyboard hooks, elevate privileges, or modify normal dictation behavior.

Already-sent input cannot be undone, and a focus switch between target checking and native SendInput cannot be fully eliminated. This does **not** constitute real-hardware acceptance or safe unattended PC automation. The GitHub prototype release remains independently unsigned, with owner-signed distribution and safe bootstrap migration outstanding. See [docs/P0_HARDWARE_ACCEPTANCE.md](docs/P0_HARDWARE_ACCEPTANCE.md).

## v0.23.17: P0-1/P0-5 scratch Notepad focus pinning

The explicit opt-in `tests/windows_safety_check.py --interactive-notepad` test now pins the **real notepad.exe process, foreground HWND, unique disposable document token, and UIA caret identity**. It rechecks the same target **before every UTF-16 input unit**, rejecting any switch to another document, window or process. The characters are harmless literal test text, not hotkeys; no save is performed. The optional stricter callback leaves ordinary VoicePilot dictation batching unchanged.

This improves the **test harness**, not proof of zero race risk or permission to automate personal files. The test launches only with typed `YES` approval and requires the user to inspect the result; `MANUAL` is never an automatic production PASS. Actual Windows hardware acceptance and authentic developer-signed updates remain outstanding. See [docs/P0_HARDWARE_ACCEPTANCE.md](docs/P0_HARDWARE_ACCEPTANCE.md).

## v0.23.16: preview deterministic voice commands without executing them

The opt-in `try_voicepilot.bat` first-run microphone UI now displays a **read-only candidate action preview** for a small, exact set of existing deterministic local commands (e.g. launch Notepad, scroll, navigate back). Unknown phrases stay unknown; this is not full AI planning, foreground verification, approval or a live execution test. Search terms and other user arguments are never echoed into preview labels; microphone transcripts are still displayed separately in the local GUI and are not saved.

The audio model worker never calls Tk APIs, including `root.after`; it delivers its result through an in-memory queue that the UI thread polls. Canceled or closed UI generations discard both the transcript and candidate preview. This improves isolation without claiming native inference is immediately interruptible.

**No actual PC/browser operations** are enabled in this mode. Official GitHub ZIPs remain unsigned prototypes, and real Windows device tests, owner signer enrollment and safe bootstrap migration remain incomplete. See [docs/FIRST_TRIAL_GUIDE.md](docs/FIRST_TRIAL_GUIDE.md).

## v0.23.15: opt-in first-run microphone-only trial (P0-5)

The distribution now includes `try_voicepilot.bat` and a separate `python -m app.first_trial` UI that only captures up to eight seconds of audio after a button click and runs **local** Japanese Whisper transcription. It never loads the desktop action agent, browser bridge, cloud planner or updater; no transcript or audio is saved. It requires Windows 11 non-admin execution.

The trial launcher creates an isolated Python 3.11 venv and installs **only audio dependencies** if the user explicitly types `INSTALL`; first-time Whisper model download is denied by default (`local_files_only=True`) and enabled only when selected in the trial UI. A cancellation during inference suppresses its output but does not pretend to kill native model computation instantly. Normal VoicePilot behavior remains unchanged.

Read [docs/FIRST_TRIAL_GUIDE.md](docs/FIRST_TRIAL_GUIDE.md) before trying. The release ZIP itself is still an **unsigned prototype**, and the trial is not proof that unrestricted PC automation, future updates, or hardware operation is safe.

## v0.23.14: offline owner signing key enrollment and verified candidate ZIP

Adds `scripts/owner_release.py` (available in the source checkout, not shipped inside the app ZIP). `new-key` creates a **password-encrypted Ed25519 PKCS8 private key outside the repository** using exclusive creation, never accepting an existing path or a short passphrase. `prepare` validates an unsigned source ZIP, pins that owner's public key into a temporary copy of the immutable revision-3 bootstrap, constructs a new ZIP and detached `.sig.json`, and verifies the complete result with the independent offline migration verifier before and after placement in a fresh external directory. No network, app execution, existing installation modification, or automated publication.

**Not production signed yet:** no real owner key has been created here and the CI's GitHub Release remains an unsigned prototype. The owner must independently verify/distribute the signer fingerprint and sign an **unpublished** release version; never overwrite a previously published unsigned ZIP under the same tag. See [docs/OWNER_SIGNED_RELEASE.md](docs/OWNER_SIGNED_RELEASE.md). Real Windows hardware acceptance and safe legacy bootstrap migration remain outstanding.

## v0.23.13: offline signed-release verification and opt-in migration to a separate folder

Adds `python -m app.signed_migration`: an **offline** verifier requiring a developer Ed25519 public key and its SHA-256 fingerprint authenticated independently of GitHub. It checks the detached signature against the exact ZIP bytes, validates Windows-safe archive paths and contents, and refuses a ZIP whose embedded bootstrap pins any different key. The default is verification-only. With explicit `--install-to` and typed `MIGRATE` confirmation, it prepares a **new, nonexistent folder** under a Windows 11 non-admin account; it never upgrades an existing installation in-place, copies secrets, starts the app, installs packages or downloads files.

**Not yet a signed production release:** current automated ZIP releases have no authenticated developer signature or pinned production key, so the tool correctly rejects them. Independent developer key enrollment, truly signed release publication and real-hardware tests are still required. See [docs/OFFLINE_SIGNED_MIGRATION.md](docs/OFFLINE_SIGNED_MIGRATION.md).

## v0.23.12: privacy-minimal Windows P0 hardware acceptance evidence

Adds `python -m app.p0_acceptance`, a strictly **read-only** preflight for Windows 11 build, non-admin privilege, Python version, app/Chrome version coherence, immutable bootstrap revision and *presence* (not authenticity) of a pinned developer signing key. Opt-in `--interactive` records only fixed test identifiers and PASS/FAIL/SKIP statuses; it never actuates a PC, records audio, opens user files or sends cloud requests. An optional `--output` creates only a new JSON report, with no window titles, usernames or personal data. A report cannot approve production, even if all answers are PASS.

The detailed manual protocol is [docs/P0_HARDWARE_ACCEPTANCE.md](docs/P0_HARDWARE_ACCEPTANCE.md). **Windows CI tests the reporter, not real Windows microphone/desktop operations.** Production signer key enrollment, legacy bootstrap migration and real-device tests remain outstanding.

## v0.23.11: fail closed without a verified developer signing key

The revision-3 immutable updater now **refuses automatic updates** unless a genuine Ed25519 developer public key has been independently verified and pinned. No empty-key fallback to GitHub-only SHA-256 remains. Any unsigned pending stage from an older bootstrap is also rejected, and every staged file is rechecked against a ZIP whose SHA-256 is bound to the Ed25519 signature. Existing updater/rollback tests now use ephemeral signed ZIPs; the Windows key-path safety check is normalized.

**This is an unsigned prototype release.** No production developer signer has been provisioned, and the automated publisher does not have a developer private key. A fresh revision-3 bootstrap therefore cannot automatically update until a trusted key and signed release are supplied. Existing installations retain their old immutable bootstrap through normal updates, so the old unsigned updater remains unprotected and requires a separate safe manual migration. No end-to-end real Windows test has been performed.

## v0.23.10: P0-4 optional pinned Ed25519 verifier (trust anchor not provisioned)

The dependency-free bootstrap **code** now supports independent Ed25519 signature verification through the maintained `cryptography` package (new dependency). In the new revision-3 bootstrap, a genuine developer public key **must be manually pinned** or **all new automatic updates fail closed**. Missing/forged signatures, mismatched version/repository/hash, and untrusted signature URLs are rejected, with no GitHub-hash-only fallback. A signed archive is retained and revalidated immediately before applying each staged file, so tampering with both local staged files and metadata cannot bypass the trust anchor.

`scripts/sign_release.py` is an offline-only helper that signs a validated release ZIP with a developer-provided Ed25519 PEM private key **outside the repository**; tests use ephemeral in-memory keys only. The GUI warns when no trust key is pinned.

**This is not yet an independently signed production update:** `PINNED_ED25519_PUBLIC_KEY_HEX` remains deliberately empty, there is no live developer secret/public-key fingerprint, and existing immutable bootstraps do not update themselves. New revision-3 bootstrap installations refuse unsigned and unpinned updates; legacy immutable bootstraps still use the older GitHub-hash-only behavior. Manual out-of-band trust establishment, safe legacy migration, actual signed release publication and Windows hardware integration tests are outstanding. See [docs/SECURE_UPDATE_MIGRATION.md](docs/SECURE_UPDATE_MIGRATION.md).

## v0.23.9: P0-4 recoverable updater transaction and legacy bootstrap warning

Hardens the **standalone** updater: validate every backup before a rollback, checksum new backup copies, recheck each staged file immediately before replacing installed code, and retain the recovery journal until the new rollback metadata is durably written. Disk-write failure and crash-recovery tests use temporary directories, including tampered/missing backups, changed staged code, and failures during commit metadata creation. Previously created legacy recovery journals remain readable (without new per-file hashes).

The desktop GUI now warns when the actual installed immutable updater lacks `BOOTSTRAP_SECURITY_REVISION = 2`. **The bootstrap is still excluded from automatic self-updates**, so existing installs do **not** inherit the new recovery checks when application code updates. No automated root-of-trust migration or independently signed releases are claimed. See [docs/SECURE_UPDATE_MIGRATION.md](docs/SECURE_UPDATE_MIGRATION.md). Windows CI is not equivalent to testing on the user's Windows machine, and SHA-256 from GitHub metadata is not an independent developer signature.

## v0.23.8: P0 screen disclosure minimization and Windows archive path validation

Cloud screen explanations no longer include unrelated background Windows titles. Subsequent Gemini planning calls omit earlier action argument bodies, including file paths and form text; current observed task context and safety validation are retained. The local Ollama path is unchanged. Screen descriptions can still contain the currently visible page text and screenshots if their separate cloud permissions are enabled.

The updater's ZIP path validation now rejects Win32 reserved device names, alternate data stream colons, trailing spaces/dots, and case-insensitive name collisions **before extraction**. Offline tests use dummy secrets and adversarial archive names.

**Migration caveat:** The stable `voicepilot_bootstrap.py` is deliberately never self-replaced during automatic updates. Existing installs running an older bootstrap do not automatically gain the new ZIP path checks. A secure bootstrap migration / pinned developer signature is still required before go-live; this is not a signed or fully trusted update system. Hardware and failure-recovery tests remain outstanding.

## v0.23.7: minimize cloud context and reject stale Chrome tab reads/actions (P0)

For Gemini, omit unrelated previous instructions, previously discovered file paths, and historical target descriptions from the model prompt. Other Chrome tab titles or Windows window lists are excluded unless the current instruction expressly requests a tab/window switch. The original in-memory snapshot is preserved for local validation; the local Ollama path retains previous context behavior. This intentionally reduces context-dependent cloud functionality to avoid transmitting older unrelated private data.

The Chrome extension rechecks the active tab and window immediately before injected page operations, and checks actual document visibility/focus in the page's execution context before reading or mutating it. A failed snapshot now returns a constant error instead of an arbitrary page exception, tab title or URL fragment. Added mocked regression tests for focus switches, hidden documents and dummy secrets. The checks narrow, but cannot eliminate, OS-level race conditions.

Roadmap P0 status remains **partially implemented**, not approved for production. Update signing, real-device integration tests, and exhaustive cloud/screenshot disclosures are pending.

## v0.23.6: P0 roadmap — stop after ambiguous effects and redact operation logs

The user-provided go-live roadmap is now tracked in [docs/ROADMAP_PROGRESS.md](docs/ROADMAP_PROGRESS.md), with **P0 blocking P1 and P2**. This release is a partial P0-2/P0-3 improvement, not a full safety sign-off.

After an effectful operation raises an ambiguous timeout, disconnect or runtime error, both normal and long-task agents stop immediately, without planning more side effects or repeating the unknown operation. A definite `ValueError`/`PermissionError` refusal is classified as incomplete; potentially committed operations are unverified.

Spoken and typed command bodies are no longer appended to the ordinary operation log; editable speech transcripts remain visible in the input field. Untrusted AI messages, browser/Windows exception bodies and raw tool-result bodies are excluded from step logs and model history. Explicit screen-explanation responses still require a separate disclosure review. Synthetic secrets and stop-race regressions test these limits without real PC input. The release remains a **Windows prototype**: independent update signing, real-device tests and full P0 checks are outstanding.

## v0.23.5: prevent wrong-window keys and restrict release tokens

Hardens the final desktop-input boundary. AI-generated hotkeys, typing and scrolling are now pinned to the foreground window observed before planning; if the window changes while the model is thinking or during the user-approval dialog, the action fails closed instead of targeting the newly focused app. Deterministic fast hotkeys also pin their window before confirmation. This reduces, but does not eliminate, non-atomic focus changes during the OS input itself.

**Any AI-proposed Chrome form fill now requires explicit approval, including writes to previously empty fields:** modern pages can autosave or transmit on input. Sensitive input fields remain blocked. Approval dialogs continue to hide input text for privacy; users should inspect the typed text in the destination.

Release CI now runs tests and builds with repository **read-only** permissions, and uses a separate publish-only job with write privileges after successful tests and only for non-PR events. Chrome extension manifest version is synchronized with `VERSION`; archive production refuses mismatches. Windows mocked regressions cover the new safety boundaries. These controls are not a sandbox, code-signing system, or proof of real-device safety.

## v0.23.4: opt-in per-operation timings and local resource snapshots

Open **Settings → 性能診断** to optionally log numeric timings for real VoicePilot commands. Voice commands report duration from **recording stop** to completion, including ASR and operation timing; text commands report worker runtime. Process-wide CPU time (seconds, **not CPU%**) and Windows current-process working set samples at start/end are included when available. Samples are not peaks and **exclude separate Ollama/Chrome processes**. No audio, transcripts, URLs or user content are added by these performance diagnostics, and there is no telemetry upload, diagnostic file, or background sampling. The normal pre-existing operation log is unchanged. Diagnostics default off.

To shorten idle retention of larger locally loaded Whisper models, unused `medium` is unloaded after 45 seconds and `large-v3-turbo` after 30 seconds. `small` and `base` stay at 75 seconds. This is a memory-retention tradeoff, not measured CPU improvement; later utterances may need to reload the model. Allocator behavior may prevent an immediate RAM decrease.

**Unverified:** actual laptop RAM savings, processing latency, CPU consumption, microphone recognition quality, and desktop success rate. CI tests exercise mocks and cannot substitute for hardware measurements.

## v0.23.3: opt-in measured speech check + final cancellation boundary

Added a strictly optional, three-trial local Whisper microphone benchmark. The user must explicitly consent (`YES`); each phrase capture is bounded to eight seconds. It reports number of exact matches and warm-model inference time without saving/printing audio or recognized text, uploading personal content, or dispatching any PC action. The initial Whisper weights download may still require network access. Run `run_tests.bat` and opt in to the additional benchmark, or use `tests/windows_safety_check.py --interactive-microphone-benchmark` inside the app's Python environment. The previous one-trial diagnostic is preserved.

The agent now checks cancellation again at the shared action dispatch boundary to narrow the race between planning and execution. It does not undo an action already underway. Unit tests use mocks and do not send real keyboard or microphone input. The safety diagnostic reads `VERSION` instead of displaying a stale hard-coded version. On-device recognition and latency have **not** yet been measured.

## v0.23.2: Confirm fast-path Undo, and test PRs before release

An explicit Japanese "undo this app's edit" command previously bypassed the approval that the AI-planned Ctrl+Z action already required. The deterministic shortcut now asks for confirmation **before** sending Ctrl+Z, because undo may revert the user's own work. Refusal or cancellation prevents the key action. Ctrl+V still requires approval; Ctrl+C does not gain an unnecessary prompt. Mock-based regressions cover these boundaries without touching a real desktop.

GitHub Actions now runs the Windows test suite on pull requests, while publishing remains disabled for pull-request events. Local speech, browser and desktop capabilities are retained. Real-machine operation accuracy, interruption reliability and CPU usage have not yet been measured.

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