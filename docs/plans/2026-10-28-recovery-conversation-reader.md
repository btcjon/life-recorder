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

- [ ] Operation-specific failures, safe diagnostics, launch preflight without attempt consumption.
- [ ] Versioned private executables/resources; isolated old/recent transcription and commit.
- [ ] Fair one-worker queue; authenticated resumable manifest-based recovery.
- [ ] Pending/failed retained audio, scoped day counts, shared truthful health and storage bytes.
- [ ] Required tests; immutable release; ten-recording canary and new-arrival checks.
- [ ] Thirty-minute observation before expansion; reconcile every manifest entry.

## Release 2

- [ ] Continuous verbatim reader, source disclosure, inference and quiet-filter behavior.
- [ ] Frozen sequential queue, explicit replacement, failure/resume actions, bounded blob lifetime.
- [ ] Frontend tests, typecheck, lint, build; desktop/320/390 rendered checks.
- [ ] Immutable deployment and live readback, legacy viewer preserved.

Rollback replaces executable source/configuration only. Never restore old database over live uploads.
