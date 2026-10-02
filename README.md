# Life Recorder

A native iPhone recorder and private Mac receiver. The iPhone records approximately one-minute AAC chunks. A Mac receiver transcribes them locally and maintains searchable daily transcripts plus one continuous Markdown transcript. Completed audio is retained locally for seven days (up to 4 GiB) for playback and offline speaker diarization; selected clips can be kept longer. Transcription stays on this Mac. Optional event summaries are separate and stay off unless `LIFE_RECORDER_REMOTE_SUMMARIES=1`.

The loopback-only Mac viewer supports transcript search, retained-audio playback, time-grouped events, speaker review, and manual speaker naming. Current behavior is [Architecture](#architecture). See [OPERATIONS.md](OPERATIONS.md) for safe restart and recovery; [INSTALL-NOTES.md](INSTALL-NOTES.md) is a dated setup journal, not the source of truth. Contributors should also read [AGENTS.md](AGENTS.md).

Future work is tracked in [TODO.md](TODO.md); planned items are not current capabilities.

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

The receiver also serves a loopback-only authenticated viewer at `http://127.0.0.1:8767`. Open `open-viewer.command` from the private data directory to launch it without copying its token into the browser history. The viewer provides a day picker, transcript search, capture-session markers, sidebar event groups, speaker badges, and pending/error counts. Flat list restores one row per clip.

Optional remote viewing for `lr.genr8ive.ai` stays bound to `127.0.0.1:8767`. Enable it with `--viewer-remote-host lr.genr8ive.ai` plus Cloudflare Access `--access-team-domain` and `--access-aud` (or `LIFE_RECORDER_ACCESS_TEAM_DOMAIN` / `LIFE_RECORDER_ACCESS_AUD`). The origin then accepts that exact Host, exact `https://lr.genr8ive.ai` Origin on mutations, and a verified `Cf-Access-Jwt-Assertion` RS256 JWT. Local `open-viewer.command` bearer flow is unchanged. Do not publish receiver port 8766. Install `PyJWT[crypto]` from `requirements-viewer.txt`.

The viewer presents the original recordings and also derives speech events by joining nearby speech across chunk boundaries. Those speech events are playback spans. They are not the sidebar groups. A speech event omits long quiet regions while preserving enough padding for natural playback. When DeepFilterNet is configured, an event can also have an enhanced playback copy; the original recording is always retained according to the normal retention policy and remains selectable.

Enhancement is deliberately playback-only. Transcription, diarization, and voiceprint learning continue to use the original recording so denoising cannot silently change recognition evidence. Derived event files are disposable cache: the storage manager evicts them before retained originals, and the receiver can recreate original event playback from retained source audio. Enhanced copies are regenerated for newly processed events rather than automatically after cache eviction. Use `--no-vad` or `--no-enhance` to disable either optional stage.

The iPhone still uploads every completed chunk. It also computes a shadow activity decision from 20 ms peak/RMS windows and may send `X-Activity-Shadow`. That header never changes upload, retry, or local delete. `would_hold` is conservative and only emitted for complete finite coverage below -60 dBFS RMS and -45 dBFS peak; recovered, unsupported, or incomplete audio is `unknown`. The Mac stores valid telemetry, ignores invalid telemetry, and compares it only against completed VAD in the viewer summary.

The iPhone upload panel distinguishes clips still queued locally, active upload, retry/backoff, a Mac storage receipt, and an interrupted clip needing local recovery. A receipt means the Mac durably stored audio, **not** that a transcript exists. While the app is foregrounded it asks the Mac, over the same pinned HTTPS connection, for the processing state of up to ten recent receipts. That bounded request is device-scoped and carries no transcript or audio. The Mac can report `pending`, `complete`, or `needs_attention`; the phone calls a completed job *processed* because a quiet recording may have no words. If the Mac cannot be reached, the panel says processing status is unavailable rather than guessing from the upload receipt. On verified acknowledgement, the phone atomically marks the queue record before deleting its audio; recovery removes only audio with that marker or a retained verified receipt ID. Audio without either proof is kept and reported for manual recovery, even when its manifest is missing or corrupt. Simulator compilation is not proof of a live phone transfer; see the latest dated installation note for the deployed phone build.

The FluidAudio CLI currently needs the repository patch in `scripts/fluidaudio-vad-output-json.patch` to expose strict JSON from `vad-analyze --output-json`. Apply that patch to a compatible FluidAudio checkout and build its release CLI; do not commit the built binary or downloaded models. Install DeepFilterNet's official Apple Silicon release outside the repository and pass its absolute path through `--enhance-cli`.

## Recording behavior

Tap the recorder switch once. Recording continues while the screen is locked and while other apps are used. If the iPhone is rebooted or the app is force-quit, iOS requires opening Life Recorder once before microphone capture can resume. Pending audio remains on the phone until the receiver acknowledges it. Upload tasks are retried and stale connectivity tasks are cancelled so they cannot hold the queue indefinitely.

The recorder pauses automatically from 10:00 PM to 5:00 AM America/New_York and resumes at 5:00 AM while the app remains in memory. iOS can still require one manual open after a reboot or force-quit.

The receiver supports local Whisper, MLX Whisper, and FluidAudio Parakeet engines. This installation uses FluidAudio Parakeet; the receiver records engine/model provenance per clip. It removes common stage-direction markers and highly repetitive hallucinated noise, then writes Eastern hourly markers and session headings after 15 minutes without captured audio. Session breaks are capture-time gaps, not speaker identity and not sidebar events. This is cleanup, not a guarantee of perfect transcription.

## Architecture

This section is the current contract. When it disagrees with `INSTALL-NOTES.md` or an older sentence in this file, this section wins.

### Transcription

The viewer's Receiver health panel reports durable uploads separately from
completed processing, pending/retrying/attention counts, original-audio storage
and free disk space, processing-stage status, a verified immutable launch-source
revision and the last committed search reconciliation. The launch job pins the
release manifest SHA-256; the complete readonly receiver source tree is checked
before application imports and cached for the process lifetime. Later checkout
edits never become a running revision. Unmanaged or altered releases report
source identity unavailable rather than guessing. No Git runs during startup
attestation or health/device requests. Pending work older
than ten minutes is delayed; an idle recorder or a completed quiet clip is not a
failure. `GET /v1/health` is human-authenticated. Phone status checks receive only
their own device's processing totals, never receiver-wide metadata or transcripts.

This installation transcribes with FluidAudio Parakeet TDT v3 through `fluidaudiocli`. Whisper.cpp and an isolated MLX Whisper command remain optional engines. Each clip stores its engine and model. Markdown session headings mark 15 minutes without captured audio.

The receiver rejects empty decoded WAVs, including zero-byte cache leftovers from earlier attempts. A failed chunk retains its source audio, a sanitized stage/error code, and an automatic retry budget of five attempts. Permanent failures or an exhausted budget become `needs_attention` instead of looping forever. Those clips stay visible in the viewer even when quiet/pending clips are hidden. A human can select one and press **Retry processing** after diagnosis; retry is unavailable if its original audio is missing, and never overwrites a completed transcript or deletes the recording. This is Mac processing state, separate from the iPhone upload queue.

### Shared decode and diarization

ASR, voice-activity detection, and diarization share one 16 kHz decode of each clip. Diarization is offline. Speaker embeddings are 256-dimensional, extraction version 3. Loading a day does not enroll a voice, assign a name, or call a model.

### Names and badges

In a recording's Transcript view, speaker stretches shorter than one second appear in a single expandable “Brief turns” row. Speaker turns still shows every stretch with playback and identity controls. Recordings containing only brief turns start expanded; opening a brief turn's identity editor also keeps the row expanded. This changes presentation only: audio, transcripts, stored speaker turns, and enrollment rules are preserved.

Automatic naming requires a profile of two accepted samples from two clips and 10 seconds of clean speech. A name is written automatically only when the best cosine score is at least 0.85 and leads the runner-up by at least 0.10. A confirmed name is a human assignment and is not replaced by a later automatic match. Automatic labels do not become enrollment samples. Sidebar badges use stored names only. A confirmed name is solid. Any other stored name is outlined and marked unconfirmed. Anonymous speaker keys are not shown.

The viewer's Review tab pages through uncertain speaker turns, prioritizing turns with playable retained audio. Each card can play its own speaker span and open the source recording. Ranked names are explicitly unconfirmed and never preselected. Confirming a name is a human label for that speaker stretch; saving it as a reusable voice sample is a separate opt-in and still requires enough clean speech. Rejecting a suggested person is remembered for that turn without changing a stored label or training a voice. Voice-check counts are an explicit, read-only diagnostic, never computed as part of day loading. A held-out check on this installation produced no safe automatic acceptances, so the matching thresholds have **not** been loosened and suggestions are not evidence of a reliable auto-assign rate.

### Sidebar events

Sidebar groups come from `display_blocks` in `receiver/viewer.py`. That function only arranges clips already loaded for the day. A group contains at least two transcribed clips on the same America/New_York date, and each following transcribed clip starts within 120 seconds of the previous transcribed clip's end. Blank transcripts stay inside a group only when transcribed clips on both sides join, and they do not extend the gap. A longer gap, a different local date, or an invalid timestamp is its own row. Quiet hours do not split these groups. The visible label is Event. The group id is the first 16 hex characters of the SHA-256 of the joined member ids. Opening a group does not fetch each child recording.

A person can save a title and explicit first and last clip for a group. That edit lives in `event_edits`, separate from automatic grouping, speech-event playback, and capture-session headings. Its id stays stable when the title or boundaries change. A saved boundary wins over automatic grouping, keeps clips that later upload between its anchors, and is not replaced when a summary is regenerated. Explicit human edits may cover one clip or cross midnight; each day shows its portion under the same identity. They must stay in time order and not overlap another edit. Saving sends the revision last read; a mismatch is rejected and nothing is written. The route is human-authenticated. Machine credentials cannot use it. Sidebar names on a group are confirmed speaker assignments only.

### Identity and context controls

People shows accepted sample provenance, readiness, withdrawal and conflict-safe
undo. Labeling and sample enrollment remain separate. Anonymous cross-recording
suggestions require a consented, clip/session-separated evaluation; manual
merge/split/name operations preview selected stretches and never enroll samples.
`scripts/evaluate-speaker-identity.py` reports correct/false assignments and
abstentions by pseudonymous identity and condition. Thresholds remain 0.85/0.10;
two clips, two samples and ten clean seconds are still required.

The phone queues Start/End meeting markers independently of audio. The viewer's
Context tab exposes inferred closure reasons, human topics and split/merge
previews. Stable, disjoint confirmed participant changes produce local candidates,
not proof a meeting ended. Unknown identities do not create a split. Human saved
boundaries take precedence; source clips and citations are preserved.

Optional cloud topic jobs are separate from summaries. They require
`LIFE_RECORDER_TOPIC_ANALYSIS=1`, an absolute `LIFE_RECORDER_TOPIC_AUTH_FILE`
pointing to the existing private Pi xAI credentials, explicit
`LIFE_RECORDER_TOPIC_MODEL`, and adapter `xai-pi-oauth` (the default).
The tool-free Responses adapter checks the provider-returned effective model on
every response and stores source/input, adapter-code and configuration fingerprints.
Missing authentication or mismatched provider metadata fails closed; there is no
provider/model fallback. Legacy CLI/self-attested receipt routes are unsupported.
Only bounded clip IDs/transcript text go to xAI; audio and location never do.
Requests disable provider storage and register no tools. One job runs at
a time after 120 seconds of settling, with bounded retries and validated output.
Optional failures do not block recording, transcription, retrieval or timeline
reads. Health reports an unavailable route rather than claiming it is ready.
Credentials are read-only; Pi owns renewal. Expired OAuth credentials report
`authentication_expired_renew_in_pi` and disable requests until renewed through
the existing Pi account flow. This adapter never rotates tokens or rewrites
other providers' credentials.

Cross-recording speaker proposals persist their supporting turn/vector/track IDs,
score, margin, extraction version and generation fingerprint after the consented
evaluation gate. Reads hide stale generations; edits and sample withdrawal
invalidate them. A proposal never merges, names or enrolls anyone automatically.

Known places and explicit event/place tags are human-managed. Phone foreground
location, background battery trial and motion are separate opt-ins, initially off.
Location uses a pinned durable context queue and never creates an audio clip.
Each recording journals an observation ID at capture start; future fixes cannot
be applied retroactively. Five-minute freshness and uncertainty containment govern
place candidates. Raw observation coordinates expire after 24 hours; the latest
phone record clears after that interval. Derived clip labels remain until explicit
history deletion. Place deletion erases its coordinates and derived labels.
History deletion also prevents delayed retries from recreating old context.
Location failure cannot block a durable audio receipt.

`LIFE_RECORDER_LOCATION_CLIENT_IDS` independently permits coordinate-free
`POST /v1/location/last-known`; responses describe the phone, not a person's
whereabouts. Transcript-only credentials receive no location access. A search
with a place filter requires both scopes. Precise-coordinate machine responses
are unavailable. Existing credentials are not silently granted this new scope.

Background trial requests coarse accuracy, automatic pausing and a 100 m distance
filter. Transmission is limited to once per minute moving or once per fifteen
minutes stationary, not guaranteed iOS delivery. Vehicle activity does not mean
driving. See [physical-device and listening gates](docs/device-trials.md) for
four-hour battery comparisons, cellular recovery, twelve-pair audio review and
seven-day quiet-upload evaluation. No gate is satisfied by synthetic tests.

### Event summaries

Summaries stay off unless the receiver process has `LIFE_RECORDER_REMOTE_SUMMARIES=1`. One background worker handles one unchanged event at a time, newest first, after 120 seconds without a change to that event. The transcript goes to the local `grok` command on standard input, not in the process arguments. A stored summary is at most two sentences and 16 words, so it fits the two-line event card. Transcript text over the size budget is sampled from the beginning, middle, and end and labeled a partial summary. The cache key changes when membership, order, transcript text, or the prompt changes. A speaker-name change does not invalidate it. A failure retries after 15 minutes and does not block the next event. The day request only reads the cache. Whether this Mac has the flag set is recorded in `INSTALL-NOTES.md`, not here.

### Agent search

Every nonempty transcribed clip is searchable, including recordings outside a
sidebar event. Results identify `kind: recording` or `kind: event`; the existing
event read route also accepts standalone recording IDs. Each exact match adds
`citation` (clip ID, SHA-256 transcript revision, capture timestamp and exact
Unicode offsets) and `clip_read_path`. Copy the citation unchanged into
`POST /v1/clips/{id}/read` with `mode: transcript`, optional `context_before`
and `max_chars`; continuation repeats those fields plus the returned cursor.
Source citations survive event regrouping and naming. Changed source text or
capture timestamp returns 409; removed/blank sources return 404. Event anchors
and cursors retain their existing revision checks. Reads also return bounded
speaker time spans with confirmed/unconfirmed/unknown identity states, separate
from quoted text; word attribution remains unknown.

The frozen 120-case `tests/fixtures/agent-retrieval-v2.json` includes explicit
Unicode evidence ranges, single recordings, multiple-record relevance,
person/time filters and negative cases. Its unique-marker wording remains a
synthetic limitation. The evaluator compares current coverage-complete strict
literal search with an offline lexical experiment. The experiment cannot be
enabled through HTTP; its synthetic scores do not establish production ranking
quality. `--baseline-ref` now records an informational historical reference;
the earlier 60-case historical comparison below remains dated evidence.

Search results include `match`: an exact passage, its clip ID and clip-start timestamp, and half-open `start_offset`/`end_offset` measured in Unicode code points (not bytes or audio seconds). `match.anchor` can open that passage with `POST /v1/events/{id}/read` using `{"mode":"transcript","anchor":{...},"context_before":160,"max_chars":2000}`. Copy the returned anchor unchanged. Continue by sending the same anchor, context and size plus `next_cursor` as `cursor`. Changed event revisions return 409; invalid clip membership or offsets return 400. Context is bounded within the matched clip; pagination then continues through subsequent clips. Every returned excerpt carries exact offsets.

`people` and legacy `speaker` fields describe event/clip associations, not attribution of the quoted words. The response marks passage `attribution` as `unknown`; matching names must not be presented as proof of who spoke the excerpt. Clip-start timestamps are not word timings. Existing literal ranking and time/person filters are unchanged.

Run `python3 scripts/evaluate-agent-retrieval.py` for the frozen 120-case suite.
Its auxiliary normalized/stemmed FTS index and IDF-weighted partial coverage are
offline only. Held-out recall/precision improve from 0.6667/0.6667 to
0.8333/0.8333, but recall misses the 0.90 target, so ranking remains unreleased.
The older 60-case comparison preserved 45 literal hits and improved match
visibility from 0 to 45; that is dated excerpt evidence, not the current benchmark.

Approved agents call `https://lr.genr8ive.ai`. They send `CF-Access-Client-Id` and `CF-Access-Client-Secret` to Cloudflare. The viewer only trusts the `Cf-Access-Jwt-Assertion` Cloudflare adds, and only when that token's `common_name` is listed in `LIFE_RECORDER_AGENT_CLIENT_IDS`. That credential can `POST /v1/search` and `POST /v1/events/{id}/read`. It cannot open a day, play audio, or change anything. Search text goes in the JSON body. Results are short. Exact transcript text is a separate paged read. The global skill `life-recorder` is the agent procedure. An empty allowlist means no agent access.

The receiver maintains stable event IDs and a private SQLite FTS index as clips change; it does not rebuild search during a read. Search can filter by literal words, time, and a confirmed person. Event reads offer a short overview or bounded transcript pages. Signed cursors detect changed search results or event content and require a fresh request rather than silently continuing stale pagination. A human-confirmed speaker change updates search generation. A missing or unknown JWT key is unauthorized; unavailable verification material fails closed as temporarily unavailable. The index schema migrates on a receiver reconciliation, so use the backup and two-pass rehearsal in [OPERATIONS.md](OPERATIONS.md) before deploying changes.

### Boundaries

The phone uploads over HTTPS with its certificate pin. The receiver accepts uploads on port 8766 and does not serve the viewer there. The viewer listens on `127.0.0.1:8767`. Remote viewing, when enabled, is Cloudflare Access in front of that loopback port.

## Using Codex to reproduce the setup

The accompanying `SKILL.md` is a reusable Codex procedure. Codex can inspect and edit this source, build it with Xcode, and use Apple CoreDevice tooling to install and launch it on a connected iPhone. For visual phone interaction it uses the CUA iPhone Mirroring surface. Codex must not guess or bypass the iPhone passcode; the user handles protected prompts, trust dialogs, Developer Mode, and microphone/local-network approval. Codex should never print or commit runtime tokens, private keys, pairing pages, audio, transcripts, device identifiers, or user-specific paths.

## Security and limits

The default upload path is local-LAN HTTPS with certificate pinning and a 256-bit random bearer token. Cellular uploads need a private VPN. The viewer stays on loopback unless optional Cloudflare Access is configured. Event summaries, when enabled, send transcript text to Grok. Audio stays in the private runtime directory. Anyone who can read that directory can read the token and transcript, so keep it private and out of backups or repositories as appropriate.

## License

MIT. See [LICENSE](LICENSE).
