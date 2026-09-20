---
name: life-recorder-setup
description: Build, install, pair, and operate the Life Recorder iPhone-to-Mac local transcription system. Use when a user asks to reproduce or troubleshoot this project.
---

# Life Recorder setup

Build the native iOS target in `ios/LifeRecorder.xcodeproj`, configure the Python receiver in `receiver/`, and keep all runtime state outside the repository.

## Phone control

Use Apple CoreDevice (`xcrun devicectl`) for read-only device inspection, installation, and launching the bundle on a connected device. Use the CUA iPhone Mirroring surface for visual UI actions when the phone is available. Do not bypass a passcode or protected system dialog. If iOS reports that the device is locked, ask the user to unlock it and open Life Recorder once.

## Required user permissions

The user must handle or approve:

- Xcode signing with their Apple developer account and development team
- Trusting the Mac and enabling Developer Mode on the iPhone
- Microphone permission for Life Recorder
- Local-network permission for Life Recorder
- Any macOS firewall or protected credential prompt

Never request or store the user's Apple password, iPhone passcode, receiver token, private key, or pairing page in the repository.

## Setup workflow

1. Check that Xcode, Python, ffmpeg, whisper.cpp, and a GGML Whisper model are installed.
2. Build and install the app on the connected iPhone using the user's team and device identifier.
3. Run `receiver/setup.py` with a private data directory and `--install-agent`. Keep the generated pairing page private and delete it after pairing if desired.
4. Pair the iPhone, switch recording on, and verify a short upload and local Markdown update.
5. For a stalled queue, inspect the receiver health endpoint and the phone's pending audio count. Opening the app while unlocked reactivates its upload manager; do not force-quit it.
6. Keep `life.md`, audio, SQLite state, tokens, certificates, pairing pages, and device-specific validation artifacts outside Git.

The phone records in one-minute chunks, pauses 10:00 PM-5:00 AM America/New_York, and queues audio while offline. The Mac receiver transcribes with local whisper.cpp, filters obvious repetitive/stage-direction hallucinations, and writes a continuous Markdown transcript with Eastern hourly markers plus session headings after 15-minute capture gaps. Those headings are not speaker labels. Reboots and force-quits require one manual app open because iOS does not provide a reliable silent microphone launch at boot. Do not enable `--mlx-command` unless that isolated venv has been proven on this host.
