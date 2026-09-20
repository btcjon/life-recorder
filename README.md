# Life Recorder

A native iPhone recorder and private Mac receiver. The iPhone records approximately one-minute AAC chunks. A Mac receiver transcribes them locally and maintains searchable daily transcripts plus one continuous Markdown transcript. Completed audio is retained locally for seven days (up to 4 GiB) for playback and offline speaker diarization; selected clips can be kept longer. No paid transcription service or cloud backend is required.

The loopback-only Mac viewer supports transcript search, retained-audio playback, anonymous speaker turns, manual speaker naming, and opt-in voice samples. Voice-name suggestions appear only after at least three confirmed samples across two recordings and 20 seconds of speech, and always require human confirmation.

## Requirements

- macOS with Xcode and an Apple developer account capable of installing a development build on the iPhone
- iPhone running a supported iOS version, with Developer Mode enabled for development installation
- Python 3.10+ and `ffmpeg`, plus at least one supported local transcription engine. Optional speech-event detection uses FluidAudio's Silero VAD command; optional enhanced playback uses the standalone DeepFilterNet `deep-filter` command.
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
  --vad-cli /absolute/path/to/fluidaudiocli \
  --enhance-cli /absolute/path/to/deep-filter \
  --install-agent
```

Setup creates a random bearer token, a self-signed TLS certificate, and a private pairing page in the data directory. Open that page only on the intended iPhone. The token is stored in the iPhone Keychain and in the private Mac runtime; it is ignored by Git. The receiver binds an authenticated upload endpoint and does not expose transcript downloads or arbitrary Mac access.

The receiver writes the combined transcript to `life.md`, daily Markdown files to `days/`, and its private SQLite ledger to `inbox.sqlite3` in the data directory. Runtime audio, transcripts, credentials, models, and databases stay outside the Git repository.

## Mac viewer

The receiver also serves a loopback-only authenticated viewer at `http://127.0.0.1:8767`. Open `open-viewer.command` from the private data directory to launch it without copying its token into the browser history. The viewer provides a day picker, transcript search, capture sessions, meeting/event hints, and pending/error counts.

Optional remote viewing for `lr.genr8ive.ai` stays bound to `127.0.0.1:8767`. Enable it with `--viewer-remote-host lr.genr8ive.ai` plus Cloudflare Access `--access-team-domain` and `--access-aud` (or `LIFE_RECORDER_ACCESS_TEAM_DOMAIN` / `LIFE_RECORDER_ACCESS_AUD`). The origin then accepts that exact Host, exact `https://lr.genr8ive.ai` Origin on mutations, and a verified `Cf-Access-Jwt-Assertion` RS256 JWT. Local `open-viewer.command` bearer flow is unchanged. Do not publish receiver port 8766. Install `PyJWT[crypto]` from `requirements-viewer.txt`.

The viewer presents the original recordings and also derives speech events by joining nearby speech across chunk boundaries. Events omit long quiet regions while preserving enough padding for natural playback. When DeepFilterNet is configured, an event can also have an enhanced playback copy; the original recording is always retained according to the normal retention policy and remains selectable.

Enhancement is deliberately playback-only. Transcription, diarization, and voiceprint learning continue to use the original recording so denoising cannot silently change recognition evidence. Derived event files are disposable cache: the storage manager evicts them before retained originals, and the receiver can recreate original event playback from retained source audio. Enhanced copies are regenerated for newly processed events rather than automatically after cache eviction. Use `--no-vad` or `--no-enhance` to disable either optional stage.

The iPhone still uploads every completed chunk. It also computes a shadow activity decision from 20 ms peak/RMS windows and may send `X-Activity-Shadow`. That header never changes upload, retry, or local delete. `would_hold` is conservative and only emitted for complete finite coverage below -60 dBFS RMS and -45 dBFS peak; recovered, unsupported, or incomplete audio is `unknown`. The Mac stores valid telemetry, ignores invalid telemetry, and compares it only against completed VAD in the viewer summary.

The FluidAudio CLI currently needs the repository patch in `scripts/fluidaudio-vad-output-json.patch` to expose strict JSON from `vad-analyze --output-json`. Apply that patch to a compatible FluidAudio checkout and build its release CLI; do not commit the built binary or downloaded models. Install DeepFilterNet's official Apple Silicon release outside the repository and pass its absolute path through `--enhance-cli`.

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
