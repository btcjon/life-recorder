# Recovery and conversation reader audit — October 29, 2026

The two releases and all 415 backlog recoveries are verified. Topic authentication
is still blocked by Pi xAI `invalid_state`. This audit also found a health-request
timeout gap, now repaired in source, and a separate phone capture coverage concern.

## Requirement checks

| Accepted requirement | Result and evidence |
| --- | --- |
| Preserve original engine/provider, capture and enrollment boundaries | Verified: same pinned private FluidAudio artifact and existing Pi route; no iOS or enrollment change. |
| Isolate failures; sanitize operation diagnostics | Verified in source and targeted engine/cache regressions. The earlier decoder-partial race remains repaired. |
| Private immutable runtime, launch preflight, bounded retries | Verified source/engine manifests and current ready state; unavailable-engine tests preserve attempts. |
| Backups, hashes, labels, edits, pins, index continuity | Fresh preservation audit passes: 495 prior transcripts unchanged, 415 live/preservation hashes equal, human records and index identities preserved. |
| Resumable authenticated recovery; canary and observation gates | All 415 manifest records remain recovered; zero unresolved. Prior canary/observation receipts remain intact. Rerun/resumption/gate tests pass. |
| One ASR worker, recent/backlog fairness, retention | Verified in unchanged worker/scheduler/retention source and focused tests. No new retry or retention action performed. |
| Pending/failed retained audio; human authentication and path safeguards | Targeted GET/HEAD/range and denial tests pass. Live authenticated HEAD/range and unauthenticated denial pass. |
| Speaker status, engine/operation/backlog/stall/last-success fields | Verified day and health responses. All recordings were complete at the initial snapshot; engine ready, no stalled work. |
| Explicit day/global counters and receipt storage bytes | Fresh live readback and scoped-count/receipt-size tests pass; audio accounting is complete. |
| Shared visible health polling, stale/unavailable status | Fifteen-second shared polling verified. A never-settling request lacked a deadline; the new ten-second deadline aborts it, reports unavailable, keeps prior data and rejects late replies. Regression failed before repair and passes afterward. |
| Default context groups, saved boundaries, inferred labels | Verified in source and existing grouping tests. Inference remains unavailable for new topic work until Pi sign-in. |
| Continuous chronological verbatim reader, source disclosure | Fresh live group shows nine source paragraphs, Sources and Play conversation; no repeated per-source action cards in the reading surface. |
| Literal search, deep links, keyboard/regrouping/focus behavior | Existing frontend regressions cover search and regrouping. Fresh 390px Back returns focus to the selected group. |
| Quiet groups, retained failed/pending visibility, no ad classification | Verified filters and unchanged classification/provider boundaries. |
| Mounted idle player, frozen ordered conversation queue | Source and frontend tests verify idle mounting, frozen queue across search/refresh/navigation and explicit playback replacement. |
| Unavailable sources, ended transitions, Resume/Retry/Skip | Existing player regressions pass; current source clocks and missing-source positions remain explicit. |
| Bounded blobs, cancellation, attribution/alignment boundaries | Reducer/player tests verify cancellation and revocation. No new speaker or word-alignment inference. |
| Tests, build, immutable rollout and legacy access | Fresh full backend 436 tests and frontend 86 tests pass; typecheck, lint and production build pass. Deployment follow-up is recorded below. |

Live machine-token denial was not freshly probed: no machine credential was read
for this review. Machine denial is covered by the current route tests; that is
separate from a fresh live authorized machine request.

## Current operation and capture coverage

Initial readback: 1,050 complete, zero pending/attention/retrying; subsequent
preservation readback: 1,051 complete. VAD and speaker processing were caught up.
The same installed source revision `5f61e21e0c94ba9c5ef86e31372ec3354253f9a8`
and all six processing artifact files verified. Viewer remains loopback-only on
8767; authenticated TLS upload remains on 8766. Original audio uses about 284 MiB
and storage reports no low-space condition.

There is a 23.07-hour interval between received source recordings: October 28
16:16:42 to October 29 15:21:00 Eastern. It includes daytime outside quiet hours.
Phone capture observations report `startup_failure` today at 13:00:11 and
13:45:16 Eastern, then active capture from 15:21:00. New recordings are arriving
and processing. This proves a received-recording coverage gap and failed startup
observations, not the exact cause or whether offline phone audio remains queued.
The categorical startup event does not distinguish denied permission, invalid
input format, audio-session failure or engine-start failure in current source.

## Further improvements, in order

1. **Phone capture diagnosis and recovery.** Reconcile the phone's local queue and
   enabled/quiet-hours state against receipts. Add safe startup error category,
   domain/code and stage, plus a clear foreground retry/recovery path. Preserve
   recording consent and the separate deferred physical/background trial gates.
2. **Expose capture and processing separately.** Show last audio received and
   capture observation freshness alongside processing status; distinguish manual
   off, quiet hours, capture failure, unavailable observer and upload delay.
   An empty ASR queue must not look like proof of continuous recording.
3. **Restore topics through Pi.** Interactive xAI sign-in is required. Keep the
   receiver read-only and make the Health action text point directly to Pi login.
   Then verify new topic jobs, inferred groups and preserved human boundaries.
4. **Reduce reader chrome.** At 320px, the first transcript begins around 353px.
   Compact repeated time-group wording/status details, highlight the currently
   playing source, and add clear dismiss/restart queue controls. Preserve source
   text and source-local clocks; do not fabricate word alignment.
5. **Later hardening/evaluation.** Validate required model components during
   preflight, periodically recheck long recovery batch safety, and complete the
   consented held-out speaker evaluation before changing naming thresholds.

Phone behavior was outside the accepted release scope and is not changed here.
No physical connectivity/quiet-upload/enrollment experiment was run.

Private fresh evidence and screenshots are under
`/Users/jonbennett/Library/Application Support/LifeRecorder-Review-20261029`.
