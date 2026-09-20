# Life Recorder Mac receiver install notes

Date: 2026-09-19. Host: local Apple Silicon Mac (`/opt/homebrew` present).
Mac receiver configured by worker; iPhone build and installation checks performed by lead.

## Lead verification and phone next step

- Simulator SDK build succeeded with code signing disabled; build output in `/tmp/life-recorder-simulator-build`.
- Independent receiver unit test run: 10 tests passed. Authenticated live receiver health returned HTTP 200; unauthenticated returned HTTP 401.
- Physical iPhone not detected and no valid code-signing identities found. Connect and trust an unlocked iPhone, then sign in under Xcode Settings > Accounts to enable device installation.
- Simulator boot completed, but install/launch commands stalled. A usable simulator preview was not verified.
- Xcode project opened for setup. Physical-device recording, pairing, and end-to-end capture remain unverified.

## Runtime

Live recording verified: physical iPhone clip started 2026-09-19T10:30:18.731Z, duration 40.6 seconds. Receiver reports one completed chunk, zero failed attempts, no error, and 435 transcript characters. Markdown export is 625 bytes. Source audio was deleted after completion per upstream behavior. Authenticated health reports ok. Transcript contents and device identifiers are omitted from these notes.

Physical iPhone update: user completed Apple sign-in; device build succeeded with automatic provisioning and bundle ID `co.jonbennett.liferecorder`. CoreDevice confirmed installation and launch with the private pairing URL. Actual microphone capture remains pending user interaction. Receiver needed a restart after a database-open error; authenticated health then returned HTTP 200 and database readability was verified. Device build and logs are stored in the private runtime directory.

- Data directory: `~/Library/Application Support/LifeRecorder` (mode 0700; outside Dropbox)
- Model: `models/ggml-small.en.bin` (ggml-small.en, 487614201 bytes, sha256 c6138d6d58ecc8322097e0f987c32f1be8bb0a18532a3f88f734d1bbf9c41e5d)
- Python: `/opt/homebrew/bin/python3` -> `/opt/homebrew/opt/python@3.14/bin/python3.14` (3.14.7)
- whisper.cpp: Homebrew formula `whisper.cpp` 1.9.4; CLI `/opt/homebrew/bin/whisper-cli`
- ffmpeg: `/opt/homebrew/bin/ffmpeg`
- Public JFK fixture: `/opt/homebrew/share/whisper.cpp/jfk.wav`

## Port

Default 8765 was already in use and was left untouched. The receiver uses the Mac's private hostname on port 8766 (`--host 0.0.0.0 --port 8766`).

## Launch agent

- Plist: `~/Library/LaunchAgents/com.browseruse.life-recorder.receiver.plist`
- Label: `com.browseruse.life-recorder.receiver`
- State after `setup.py --install-agent`: running, pid 37599, runs=1, last exit never
- Logs: `receiver.log`, `receiver-error.log` in the data directory
- Pairing page and start script exist in the data directory; contents are private and not copied here

`/health` without a bearer token returns HTTP 401. That matches `receiver.py` (all GET/POST require Authorization). Listener confirmed on `*:8766`.

## Tests

- `/opt/homebrew/bin/python3 tests/test_receiver.py`: 10 tests, OK, 5.240s
- `tests/audio_smoke.py` with ggml-small.en and the public JFK wav: passed in 5.08s (HTTPS upload, durable checksum, audio deleted after transcription, retry after deletion). `iphone_recording_tested`: false

## Blockers / out of scope

- No physical iPhone and no signing identities in this worker. Pairing and live phone upload were not run.
- No public tunnel or paid transcription service was added.

## 2026-09-19 receiver FD leak and queued-upload check

Cause: `sqlite3.Connection` as a context manager commits/rolls back but does not close. Live process had 113 SQLite FDs (`inbox.sqlite3`/`-wal`) after parent restart; worker died at `connect()` with `unable to open database file` while HTTP still listened. Reproduced locally: 50 unclosed `with conn` opens = +50 FDs; explicit close = +0.

Fix in `receiver/receiver.py`: `Inbox.connect()` is a context manager that yields inside `with db` and always `db.close()` in `finally` (including PRAGMA failure). Worker poll catches `sqlite3.Error` and retries instead of killing the thread.

Tests: `/opt/homebrew/bin/python3 tests/test_receiver.py` 12 tests OK, 6.239s (added repeated-connect FD bound and PRAGMA-close).

Deployed: `launchctl kickstart -k gui/501/com.browseruse.life-recorder.receiver`; pid 53966, `*:8766`, sqlite FDs 0 after 6s and after another 6s, total FDs 38/38. Inbox counts: complete=1, pending=0, audio files=0. Unauthenticated MagicDNS `/health` HTTP 401 (TLS up). Credentials/recordings not modified.

Cellular (source, no iOS change): `UploadManager` background session sets `allowsCellularAccess = true`, `isDiscretionary = false`, `waitsForConnectivity = true`, `sessionSendsLaunchEvents = true`. Pairing copy says Wi-Fi or cellular. No `allowsConstrainedNetworkAccess`/`allowsExpensiveNetworkAccess` override (iOS defaults true). Status `Uploading when connected` is the waiting-task string, not a Wi-Fi-only gate. User confirmed Cellular Data on for Life Recorder and Tailscale, Tailscale Connected with Wi-Fi off. Phone was not reinstalled.

Tailscale (Mac): Backend Running, ShieldsUp false, MagicDNS TCP 8766 succeeds from this Mac. One of two iOS peers online with pong; the other timed out. Queued clips have not arrived on the Mac.

Next action (phone, no force-quit/reinstall): keep Life Recorder open in the foreground while Tailscale shows Connected. If status stays `Uploading when connected` after that, the remaining gap is phone-to-Mac:8766 on the Tailscale hostname, not a cellular-deny flag in app source.

## 2026-09-19 Tailscale upload ATS candidate (not installed)

Superseding lead verification: physical-device connection recovered. Lead built successfully and installed the exact-host ATS update in place, launched the app and resent the existing private pairing URL to restart queued attempts. Both previously queued clips then arrived and completed transcription (14.3 seconds / 157 characters; 6.4 seconds / 80 characters), alongside the original 40.6-second LAN test. User had confirmed Wi-Fi off, cellular permissions enabled, and Tailscale Connected. No recordings uninstalled or manually deleted. The new exact-host ATS exception retains HTTPS-only URL validation, TLS 1.2 minimum, and existing SHA-256 certificate pin checks. Added XCTest cases remain unexecuted; successful device build and real queued uploads are the decisive checks.

Live phone prefs (CoreDevice copy): HTTPS `.ts.net` host, port 8766, 64-hex pin present, `recorderEnabled=false`. App process was running. Cellular session flags unchanged (`allowsCellularAccess=true`, `isDiscretionary=false`, `waitsForConnectivity=true`). Pin callback still fail-closed on hash mismatch; `ReceiverSettings` still HTTPS-only.

ATS in shipped Info.plist was only `NSAllowsLocalNetworking`. No `NSExceptionDomains`, no `NSAllowsArbitraryLoads`. Mac default TLS verify of MagicDNS:8766 failed (`SSLCertVerificationError`); live leaf matched runtime cert. No phone syslog/-1022/-1202 this turn: later CoreDevice file/lock calls returned error 4016 (trusted-connectivity assertion). Queue listing therefore unverified here.

Candidate change in repo, not deployed at that point: an exact-host `NSExceptionDomains` entry for the private Tailscale hostname (`NSExceptionAllowsInsecureHTTPLoads=true`, TLSv1.2, no subdomains). XCTest additions for HTTPS/pin rejection and exact-host ATS were not executed in that check. No in-place install occurred in that check.

## 2026-09-19 quiet hours, sessions, MLX path

- iOS quiet hours: 22:00-05:00 America/New_York in `QuietHours.swift`; recorder stops/resumes on that boundary while the process is alive.
- Receiver Markdown: session headings after 15 minutes without captured audio. Explicitly not speaker identity. No pyannote.
- Live transcriber remains Homebrew `whisper-cli`. Isolated `.transcription-venv` (Python 3.12, mlx-whisper 0.4.3) was created for an optional Apple Silicon path. Homebrew `mlx_whisper` is broken (`libmlx.dylib` missing). `python -m mlx_whisper` is not a module entry point. Do not point the launch agent at MLX until a CLI invocation is proven offline against a local model directory.
