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
- [x] Frozen 120-case lexical/hybrid benchmark; ranking remains experimental (held-out hybrid recall 0.881 below 0.90).
- [x] Profile provenance, sample withdrawal and conflict-safe identity undo.
- [ ] Consented held-out speaker evaluation tooling; clusters gated on real evidence.
- [x] Durable phone/manual viewer meeting markers, conservative closures, participant-change candidates and editable suggestions.
- [x] Grok topic adapter with per-response provider model proof, validated output and isolated failures; current rollout status is recorded in INSTALL-NOTES.
- [x] Place management, expiring observations, pinned location queue and separate agent scope; phone installed in place with location/motion switches off.
- [ ] Opt-in background/motion trial with physical battery/coverage measurement protocol.
- [ ] Enhanced playback comparison tooling; listening approval required for default change.
- [ ] Seven-day quiet-upload evaluation; suppression inactive until the explicit gate passes.
- [ ] Cellular physical-device receipt/processing/search/playback and recovery check.
- [x] Full tests, rendered viewer, two-pass migration rehearsal and live receiver rollout; origin readback is recorded separately.

Absent human listening, consented speaker recordings or duration-based phone
measurements are pending evidence, never synthetic proof of those gates.

## Verified source checks

Full Python suite: 355 tests passed, including rendered desktop/mobile viewer,
scope denial, stale revisions, single-clip and midnight events, source citation
continuity, and optional-context failures preserving durable audio receipts.
The suite emitted synthetic JWT/SQLite warnings and a browser disconnect
BrokenPipe trace; none failed a check. App simulator build and build-for-testing
passed. After harness repairs, all 45 native XCTest tests passed, none skipped,
including pinned HTTPS upload to an isolated synthetic loopback receiver; one
durable receipt and its audio SHA were independently verified. Xcode exited zero
after its owned stalled simulator-diagnostics helper was stopped. The result
bundle independently reports Passed. A runtime main-thread warning remains in
that bundle; no test failed. No physical phone was used for simulator fixtures.
After the user signed into Xcode, the signed app was installed in place without
resetting settings or pending recordings. A fresh physical 9.2-second clip was
received, processed, indexed and playable. This did not prove cellular recovery.

Migration rehearsal on a fresh private safety copy: two passes, second unchanged,
SQLite integrity OK, event IDs/cursor preserved, 52 keyed/FTS transcripts and
52 stable source citations verified. Copies are deployment safeguards, not an
ongoing backup service. New uploads remain in the live database.

Persisted anonymous speaker proposal evidence includes similarity, margin,
supporting turn/vector/track IDs, extraction version and source/membership
fingerprints. Transcript/voice reconciliation refreshes it; stale reads hide it.
Optional failures roll back only their savepoint. No enrolled profiles or real
consented evaluation exist in the live database, so proposals remain gated off.

The existing host-local Pi xAI credentials were used for synthetic Responses
checks reporting the configured `grok-4.7`, no tools/web and `store:false`.
Security review found a shared-credential refresh race; adapter-managed renewal
was removed before rollout. Credentials are now read-only; expired OAuth fails
closed with an instruction to renew through Pi. Each completed topic job records
provider-envelope model evidence plus input/source/code/config fingerprints.

The offline hybrid prototype uses the already-present Apple NaturalLanguage
English sentence model (revision 1, 512 dimensions), with no download or cloud
embedding. Tuning used 64 cases before evaluating 56 held-out cases. Recall and
precision were 0.881, compared with lexical 0.8333 and baseline 0.6667; negative
accuracy was 1.0, unsupported attribution zero, maximum response 2661 bytes and
held-out p95 8.813 ms including fresh query embedding/IPC. This synthetic fixture
does not prove natural-language production quality. Recall missed 0.90, so
production ranking is unchanged.

The local DeepFilterNet Apple Silicon executable was restored outside Git from
its [official release](https://github.com/Rikorose/DeepFilterNet/releases/tag/v0.5.6).
A public fixture passed with 0.03 seconds duration difference. This is a technical
check, not twelve representative pairs or listening approval. Enhanced playback
is only a selectable disposable derivative; the original remains the default
and all recognition/enrollment input.

## Remaining acceptance gates

- Physical phone controls and fresh cellular/disconnection/recovery evidence.
- Consented enrollment and held-out recordings covering noise, distance, brief
  and overlapping speech and unknown people; report correct/false/abstained
  outcomes per pseudonymous identity and condition. Clustering suggestions remain off.
- OAuth renewal remains owned by Pi; unavailable credentials disable optional
  requests. Live topic-job provenance passed after rollout, with no replacement
  model/provider. Manual/local timeline works without cloud analysis.
- Matched four-hour background battery runs: no audio loss/upload regression and
  at most three additional battery percentage points. The trial remains opt-in.
- Twelve level-matched original/enhanced pairs and user listening approval before
  any playback-default change.
- Seven representative shadow days with zero speech-containing holds, plus
  battery/storage/network evidence before a hold queue or production suppression.
- A later measured production-ranking decision; the evaluated hybrid candidate
  remains unreleased because it missed the recall target.

Live rollout and origin readback are recorded in INSTALL-NOTES.md after observation.
