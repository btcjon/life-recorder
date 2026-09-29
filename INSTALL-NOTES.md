# Life Recorder Mac receiver install notes

This file is a point-in-time journal. It is not the architecture source of truth. Current behavior is the Architecture section of `README.md`; restart and recovery steps are in `OPERATIONS.md`.

Two statements later in this journal described the 2026-09-19 install and are no longer current: source audio is retained for playback and diarization rather than deleted after transcription, and this Mac transcribes with FluidAudio Parakeet rather than Homebrew `whisper-cli`.

## 2026-09-29 intermittent playback investigation (repair not confirmed)

The selected September28 12:24 capture contains repeated near-silent intervals, also seen in eight earlier captures that morning; first-eight-second checks of 71 retained captures found nine candidates. Later recordings did not show this pattern. Standard FFmpeg AAC and Apple AudioToolbox decoding both exhibit the gaps. Fixed-point AAC initially looked better by floating-point zero counts, but playable 16-bit PCM retained a 180ms silent gap. A representative gap is approximately -96dBFS even with fixed-point decoding: no usable speech was recovered. The attempted browser PCM conversion passed 237 synthetic tests but failed the decisive real-file waveform check and was removed. Do not treat that experiment as a fix or bulk-reprocess recognition data from it.

Code-only rollback restored receiver/tests exactly to their pre-experiment tree (commits `a90efae`, `3d327b2`); unrelated README/TODO work was preserved. Receiver restarted; health 200/401, SQLite integrity OK, event IDs, labels, 50 voice samples and 586 keyed/FTS transcripts matched the immediate pre-rollout baseline. Originals were unchanged. Capture-time cause remains unproven; a fresh phone-side speech recording and comparison are required before changing iOS capture. Existing enhanced derivatives were not modified.

## 2026-09-29 compact brief speaker turns (deployed)

Source commit `1152c6e` groups sub-second stretches into one expandable Transcript disclosure, preserving all playback and identity controls in Speaker turns. Disclosure state persists across labeling, refresh, and mode changes; Escape restores focus. All-brief recordings start expanded. No stored turns, enrollment rules, or schema changed.

Desktop/mobile synthetic browser regression and the updated full suite passed (232 tests); staged diff check passed. A consistent private SQLite/source/plist backup preceded restart of the existing launch job. New viewer PID 45492 listens on loopback 8767 and serves the updated JavaScript. Authenticated upload health returned 200 with viewer OK; unauthenticated returned 401. SQLite integrity, event IDs, confirmed labels, 50 voice samples, 586 keyed/FTS transcripts, index generation 204, and cursor-key fingerprint matched baseline. No new error-log entries. Live upload health used the LAN interface because an unrelated loopback-only process also occupies 8766; it was not modified. Fresh phone upload and a rendered remote production browser session were not exercised in this rollout.

## 2026-09-28 speaker maintenance repair (deployed)

Source commit `95a19c7` makes unchanged enrollment a no-op, tolerates normalization roundoff, and prevents legacy recovery from overriding a current human sample opt-in decision. Score 0.85, margin 0.10, readiness and unanimity remain unchanged. No schema migration or iOS update.

Grok 4.7 RPC worker stopped at its bounded deadline without edits; lead implemented and verified the repair. Astra reviewed it, caught a floating-point edge case, and accepted the repaired criterion. Targeted 19 and full 231 tests passed, with nonfatal test-fixture warnings; diff check passed. Private SQLite backup integrity passed. A rehearsal drained 348 jobs, preserving exact sample rows and confirmed names.

Restarted the existing launch job (PID 2875 -> 49492). Live voice queue fell 347 -> 63 -> 0 and remained empty on a subsequent check. All 50 samples retained identical voice evidence to the backup; confirmed assignments stayed unchanged. Sample IDs/timestamps changed while the old process was still reenrolling before restart, then stabilized after restart. All 916 processing records remained complete; 25 event IDs, 516 keyed/FTS transcripts, 459 representative FTS matches, schema version 2, generation 129, and cursor-key fingerprint were preserved. SQLite integrity remained OK.

Authenticated upload health returned 200 with viewer OK; unauthenticated health 401. Viewer root/day reads and retained audio HEAD returned 200; unauthenticated remote viewer returned 403. Listener scopes stayed upload `*:8766` and viewer `127.0.0.1:8767`. No fresh phone upload, live machine-credential probe, or rendered-browser check was performed; UI/auth code was not changed. No push. This fixes queue cycling, not the conservative identity rule's measured abstention rate.

## Current install (verified 2026-09-24)

- Data directory: `~/Library/Application Support/LifeRecorder`
- Upload listener: port 8766. Viewer: `127.0.0.1:8767`
- Launch agent `com.browseruse.life-recorder.receiver` has `LIFE_RECORDER_REMOTE_SUMMARIES=1`, so event summaries run on this Mac. The code default is off.
- Agent search is `POST /v1/search` and `POST /v1/events/{id}/read` on `https://lr.genr8ive.ai`. This Mac's launch agent allowlists the Life Recorder Cloudflare Access service token in `LIFE_RECORDER_AGENT_CLIENT_IDS`. Agents read `LIFE_RECORDER_CF_ACCESS_CLIENT_ID` and `LIFE_RECORDER_CF_ACCESS_CLIENT_SECRET` from `secrets.common.env`. Those values are not stored in this repo.
- Sidebar grouping, speaker badges, and cached summaries are specified in `README.md`.

## 2026-09-24 recovery and review rollout

- Committed Mac/iPhone source, tests, and behavior docs as `8e5f460` on `codex/local-life-recorder`; no push. The full Python suite passed 228 tests and `git diff --check` passed. The iPhone simulator app/test target built, and five focused queue-recovery/UI tests passed on an iPhone 17 simulator. Synthetic desktop/mobile browser review flows passed, including speaker confirmation and event editing.
- Before restart, a private mode-0700 consistent SQLite backup, loaded plist, and receiver source were saved outside Git. Two-pass migration rehearsal and a separate event-edit rehearsal preserved integrity, cursor key, search index, and old event references. The live pre-restart baseline was 863 clips (849 complete, 14 pending), 463 indexed transcripts/FTS rows, 20 active agent event IDs, generation 66, and integrity `ok`.
- Restarted the **existing** launch job with `launchctl kickstart -k`, without rerunning setup. New PID 49122 listens on `*:8766` and viewer `127.0.0.1:8767`. Authenticated `/health` returned 200 with viewer `ok` and Parakeet; unauthenticated upload health, local viewer API, and remote-host viewer request without JWT returned 401. A day/detail request and retained-audio HEAD succeeded. No live authorized machine-credential probe was available; machine isolation passed focused route tests.
- The 14 old pending clips had retry times 20–55 minutes in the future. After a focused Opus safety check, each **pending** clip was advanced individually through the authenticated human retry route, beginning with one canary and waiting for its outcome before the next. A private audit compared SHA-256 of all 14 original source files and hashes of all 849 pre-existing completed transcripts after each result. All 14 became complete: one contained recognized words; 13 completed with no words. No original source or pre-existing transcript changed. Final database: 863 complete, zero pending or attention, 464 indexed transcripts/FTS rows, generation 67, integrity `ok`. All 20 pre-existing agent event IDs remain resolvable; active groups are now 21 because processing added a transcript. No audio was manually removed or bulk-retried in attention state.
- The new iPhone delivery/processing panel and acknowledged-file recovery safeguard were built and installed **in place** on the connected physical iPhone; the original bundle ID was unchanged, and no uninstall or queue reset was performed. Initial automatic provisioning failed because Xcode has no signed-in account. A valid prior profile, already embedded in the earlier working build and including this phone and the local signing certificate, was restored locally; an automatic signed build then passed code-signature verification. The first CoreDevice install lost its connection, but a second install succeeded. iOS initially denied launch while the phone was locked; a later CoreDevice launch succeeded and the app process remained running. The app's PendingAudio directory had zero entries. A read-only preference check found `recorderEnabled=false`; this setting was not changed, so no fresh clip/upload was expected. The temporary preference copy was removed. The phone panel was not visually checked because this Mac's iPhone Mirroring is paired to a different iPhone. A fresh physical-phone upload and rendered phone UI remain unverified; simulator success and install success alone do not prove them.

## 2026-09-23 source and receiver rollout

- Source and project docs committed as `80e8d65` on `codex/local-life-recorder`; no push. Full Python suite: 179 tests passed; staged diff and local Markdown links checked.
- Before restart, a private consistent SQLite backup, loaded launch plist, and receiver source were saved outside Git. A copy of the schema-v1 database was migrated and reconciled twice: 400 FTS rows, 348 count-only matches for a representative term, 18 live event IDs, cursor key and generation 3 stayed stable; integrity was `ok`.
- Restarted the existing launch job with `launchctl kickstart -k`, without rerunning setup. New PID 96223 started at 09:53:21 EDT, running this checkout. Upload remains on `*:8766`; the viewer remains on `127.0.0.1:8767`.
- Authenticated upload `/health` returned 200 with viewer `ok` and Parakeet; unauthenticated returned 401. Live agent-index schema is 2 (not SQLite `user_version`), with 400 keyed transcripts, 400 FTS rows, the same 348 count-only matches, 18 live event IDs, preserved cursor-key fingerprint/generation, and `PRAGMA integrity_check=ok`.
- Local viewer shell, JavaScript, day list, and a day detail returned 200; an existing retained-audio HEAD returned 200. A remote-host request without a JWT returned 401. The error log had not changed since the previous day when checked after restart.
- Authorized remote machine search/read and a new iPhone upload were **not** exercised in this rollout; the suite covers route boundaries, but live end-to-end agent and phone delivery remain separate checks. Health showed 3 pending clips at the time of verification.

Entries below are historical setup notes; their then-current states do not supersede the current install or the verified rollout above.

## 2026-09-19 journal

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
