# October 2 improvements implementation

Authorized delivery order: health; retrieval and stable source citations; speaker
enrollment/corrections and evaluation-gated clusters; meeting controls and topic
suggestions; manual/foreground places and opt-in background trial; audio and
quiet-upload experiments. Reviewed source/documentation may be pushed to origin,
never upstream. Runtime data and credentials remain outside Git. No additional
runtime backup system is requested; pre-migration safety copies remain required.

## Acceptance ledger

- [x] Health API, viewer and device-scoped phone status; idle/delayed/attention tests.
- [x] All transcribed clips searchable; grouping-independent citations and clip reads.
- [x] Frozen 120-case relevance benchmark; ranking remains experimental (held-out recall 0.8333 below 0.90).
- [x] Profile provenance, sample withdrawal and conflict-safe identity undo.
- [ ] Consented held-out speaker evaluation tooling; clusters gated on real evidence.
- [x] Durable phone/manual viewer meeting markers, conservative closures, participant-change candidates and editable suggestions.
- [x] Grok topic worker code with pinned model/route proof, validated output and isolated failures; live route unavailable and disabled.
- [x] Place management, expiring observations, pinned location queue and separate agent scope; physical-phone rollout pending.
- [ ] Opt-in background/motion trial with physical battery/coverage measurement protocol.
- [ ] Enhanced playback comparison tooling; listening approval required for default change.
- [ ] Seven-day quiet-upload evaluation; suppression inactive until the explicit gate passes.
- [ ] Cellular physical-device receipt/processing/search/playback and recovery check.
- [x] Full tests, rendered viewer, two-pass migration rehearsal and live receiver rollout; origin readback is recorded separately.

Absent human listening, consented speaker recordings or duration-based phone
measurements are pending evidence, never synthetic proof of those gates.

## Verified source checks

Full Python suite: 322 tests passed, including rendered desktop/mobile viewer,
scope denial, stale revisions, single-clip and midnight events, source citation
continuity, and optional-context failures preserving durable audio receipts.
The suite emitted synthetic JWT/SQLite warnings and a browser disconnect
BrokenPipe trace; none failed a check. App simulator build and build-for-testing
passed; XCTest execution remains unverified after simulator service failure.
The signed device-build attempt failed because Xcode has no signed-in account
and no cached provisioning profile. The existing working phone app was not
replaced, and no permissions or recorder settings were changed.

Migration rehearsal on a fresh private safety copy: two passes, second unchanged,
SQLite integrity OK, event IDs/cursor preserved, 51 keyed/FTS transcripts and
51 stable source citations verified. Copies are deployment safeguards, not an
ongoing backup service. New uploads remain in the live database.

The local DeepFilterNet Apple Silicon executable was restored outside Git from
its [official release](https://github.com/Rikorose/DeepFilterNet/releases/tag/v0.5.6).
A public fixture passed with 0.03 seconds duration difference. This is a technical
check, not twelve representative pairs or listening approval. Enhanced playback
is only a selectable disposable derivative; the original remains the default
and all recognition/enrollment input.

## Remaining acceptance gates

- Xcode account sign-in/provisioning, in-place phone install, rendered controls
  and fresh cellular/disconnection/recovery evidence.
- Consented enrollment and held-out recordings covering noise, distance, brief
  and overlapping speech and unknown people; report correct/false/abstained
  outcomes per pseudonymous identity and condition. Clustering suggestions remain off.
- Independently verified configured Grok model/route for topic jobs. No replacement
  model/provider was selected. Manual/local timeline functionality works without it.
- Matched four-hour background battery runs: no audio loss/upload regression and
  at most three additional battery percentage points. The trial remains opt-in.
- Twelve level-matched original/enhanced pairs and user listening approval before
  any playback-default change.
- Seven representative shadow days with zero speech-containing holds, plus
  battery/storage/network evidence before a hold queue or production suppression.
- Relevance targets and a separately selected/evaluated local hybrid model if
  paraphrase coverage warrants it; no production ranking change is enabled.

Live rollout and origin readback are recorded in INSTALL-NOTES.md after observation.
