# Processing recovery and conversation reader

Accepted scope: two staged releases; preserve capture, local Parakeet, Grok through Pi,
authentication, consent, existing transcripts and seven-day completion-based retention.

## Evidence and preservation

- Private consistent SQLite/configuration/source backup and 415 hash-verified originals
  preserved before changes, 2026-10-28. Baseline: 495 completed / 415 attention.
- Old and recent failed originals pass isolated cache maintenance and ffmpeg decode.
- Dropbox executable was online-only; private copy passes launch and signature checks.
- Pi xAI readiness reports invalid_state. Interactive renewal remains independent of ASR.

## Release 1

- [x] Operation-specific failures, safe diagnostics, launch preflight without attempt consumption.
- [x] Versioned private executables/resources; isolated old/recent transcription and commit.
- [x] Fair one-worker queue; authenticated resumable manifest-based recovery.
- [x] Pending/failed retained audio, scoped day counts, shared truthful health and storage bytes.
- [x] Required tests; immutable release; ten-recording canary and new-arrival checks.
- [ ] Thirty-minute observation before expansion; reconcile every manifest entry.

## Release 2

- [x] Continuous verbatim reader, source disclosure, inference and quiet-filter behavior.
- [x] Frozen sequential queue, explicit replacement, failure/resume actions, bounded blob lifetime.
- [x] Frontend tests, typecheck, lint, build; desktop/320/390 rendered checks.
- [ ] Immutable deployment and live readback, legacy viewer preserved.

Rollback replaces executable source/configuration only. Never restore old database over live uploads.

## Observed Release 1 rollout

Source commit `9e3507ccd7fab2d37c7ba260b4b25a9ad8da1715` deployed through the
existing launch job. FluidAudio arm64 artifact includes six hashed executable/resource
files with separately pinned manifest; original engine and model remain unchanged.
An initial launchd bootstrap raced bootout; the absent service was reconciled before
a successful bootstrap. No database rollback or upload deletion occurred.

Ten recovered recordings spanning October 14–28 completed ASR, VAD and speaker analysis.
The resumable manifest reconciled all ten as recovered. New live arrivals complete.
415 receipt byte counts were backfilled without changing processing or retention.
Preservation checks confirm 495 prior transcripts, 415 original hashes, human records,
pins, cursor identity and index continuity. Expansion is still gated on observation.

Full backend suite: 432 passing tests, with additional recovery resumption/day-count
regressions checked separately. Frontend: 85 passing tests, typecheck, lint and build.
Rendered browser checks cover sequential source advancement, retained pending audio,
known missing-source disclosure, literal text, desktop, 390px, 320px and Back focus.
The mobile reader hides library controls until Back, preserving reading space.

Topic authentication remains blocked by Pi `invalid_state`; no credentials were
written by the receiver and no alternate provider was selected.
