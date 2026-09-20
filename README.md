# Life Recorder

A native iPhone recorder and private Mac receiver. The iPhone records approximately one-minute AAC chunks. A Mac receiver transcribes them locally and maintains searchable daily transcripts plus one continuous Markdown transcript. Completed audio is retained locally for seven days (up to 4 GiB) for playback and offline speaker diarization; selected clips can be kept longer. No paid transcription service or cloud backend is required.

The loopback-only Mac viewer supports transcript search, retained-audio playback, anonymous speaker turns, manual speaker naming, and opt-in voice samples. Voice-name suggestions appear only after at least three confirmed samples across two recordings and 20 seconds of speech, and always require human confirmation.

## Requirements

- macOS with Xcode and an Apple developer account capable of installing a development build on the iPhone
- iPhone running a supported iOS version, with Developer Mode enabled for development installation
- Python 3.10+, `ffmpeg`, `whisper-cli` from whisper.cpp, and a downloaded GGML Whisper model. Optional: a project-local `.transcription-venv` with `mlx-whisper` for an Apple Silicon experiment, not the default receiver.
- A reachable HTTPS path between phone and Mac (same LAN by default; use a private VPN for cellular access)

## Build and install

Open `ios/LifeRecorder.xcodeproj` in Xcode, select the connected iPhone, choose your Apple development team, and Build and Run. The app requests microphone and local-network access. Keep the source tree free of runtime credentials.

For command-line builds, use an Apple development team and the connected device identifier:

```sh
DEVELOPER_DIR=/Applications/Xcode.app/Contents/Developer \
  xcodebuild -project ios/LifeRecorder.xcodeproj -scheme LifeRecorder \
  -destination 'id=YOUR_DEVICE_UDID' -allowProvisioningUpdates \
  DEVELOPMENT_TEAM=YOUR_TEAM_ID build
```

Before building for a private Tailscale hostname, replace the example
`your-mac.example.ts.net` key in `ios/LifeRecorder/Info.plist` with the exact
hostname used by the pairing URL. Keep the exception exact-host, HTTPS-only,
TLS 1.2 or newer, and continue using the app's certificate pin.

## Configure the Mac receiver

```sh
python3 receiver/setup.py \
  --data-dir /absolute/private/runtime \
  --model /absolute/path/to/ggml-small.bin \
  --install-agent
```

Setup creates a random bearer token, a self-signed TLS certificate, and a private pairing page in the data directory. Open that page only on the intended iPhone. The token is stored in the iPhone Keychain and in the private Mac runtime; it is ignored by Git. The receiver binds an authenticated upload endpoint and does not expose transcript downloads or arbitrary Mac access.

The receiver writes the combined transcript to `life.md`, daily Markdown files to `days/`, and its private SQLite ledger to `inbox.sqlite3` in the data directory. Runtime audio, transcripts, credentials, models, and databases stay outside the Git repository.

## Mac viewer

The receiver also serves a loopback-only authenticated viewer at `http://127.0.0.1:8767`. Open `open-viewer.command` from the private data directory to launch it without copying its token into the browser history. The viewer provides a day picker, transcript search, capture sessions, meeting/event hints, and pending/error counts.

The viewer lists captured chunks and transcripts, but it cannot play completed audio because the receiver deletes audio after successful transcription. Changing that behavior requires an explicit retention policy and additional private storage.

## Recording behavior

Tap the recorder switch once. Recording continues while the screen is locked and while other apps are used. If the iPhone is rebooted or the app is force-quit, iOS requires opening Life Recorder once before microphone capture can resume. Pending audio remains on the phone until the receiver acknowledges it. Upload tasks are retried and stale connectivity tasks are cancelled so they cannot hold the queue indefinitely.

The recorder pauses automatically from 10:00 PM to 5:00 AM America/New_York and resumes at 5:00 AM while the app remains in memory. iOS can still require one manual open after a reboot or force-quit.

The receiver supports local Whisper, MLX Whisper, and FluidAudio Parakeet engines. This installation uses FluidAudio Parakeet; the receiver records engine/model provenance per clip. It removes common stage-direction markers and highly repetitive hallucinated noise, then writes Eastern hourly markers and session headings after 15 minutes without captured audio. Session breaks are capture-time gaps, not speaker identity. This is cleanup, not a guarantee of perfect transcription.

## Using Codex to reproduce the setup

The accompanying `SKILL.md` is a reusable Codex procedure. Codex can inspect and edit this source, build it with Xcode, and use Apple CoreDevice tooling to install and launch it on a connected iPhone. For visual phone interaction it uses the CUA iPhone Mirroring surface. Codex must not guess or bypass the iPhone passcode; the user handles protected prompts, trust dialogs, Developer Mode, and microphone/local-network approval. Codex should never print or commit runtime tokens, private keys, pairing pages, audio, transcripts, device identifiers, or user-specific paths.

## Security and limits

The default connection is local-LAN HTTPS with certificate pinning and a 256-bit random bearer token. The receiver has no public tunnel or cloud storage built in. A private VPN is required for cellular uploads outside the home network. Anyone who can read the private runtime directory can read the token and transcript, so keep that directory private and out of backups or repositories as appropriate.

## License

MIT. See [LICENSE](LICENSE).
