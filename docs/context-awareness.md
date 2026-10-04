# Audio context evidence

Implementation and acceptance contract, October 4, 2026. Planned and reviewed
with Astra. Deployment and physical-device evidence are recorded separately in
INSTALL-NOTES.md.

## Purpose

Show what was observed around a recording: Mac output activity, phone audio/call state, and actual recorder stops. This is temporal context, not proof of the source of recorded speech. Never use these signals to assign a speaker, change a transcript, or merge a meeting.

## Evidence boundaries

Mac CoreAudio process output IO can identify an app with output activity. It does not prove audible sound, identify a particular browser tab, or measure what the iPhone microphone heard. The first rollout must keep this signal useful even if optional browser metadata is unavailable.

YouTube/X page titles are page hints, not playback evidence. Optional metadata must come from allowlisted tabs and directly observed media state, with existing scripting permissions only. Store capped sanitized titles and host categories, not URLs, query strings, history, or scraped content. Multiple playing tabs remain ambiguous.

iPhone other-audio state and categorical calls are observations made while the app can execute. They are not continuous background coverage. Do not store call handles, contact identities, or raw call identifiers. An interruption and a call may overlap without establishing that the call caused the gap.

A recording gap opens when capture actually stops. It closes only after capture successfully starts again. An interruption-ended notification is not proof of recovery. Relaunch can reveal an unfinished gap but cannot reconstruct an exact unseen termination time.

## Storage and transport

Use a dedicated immutable observation table and the existing authenticated, pinned, durable context outbox. Context delivery cannot block audio receipts. A versioned bounded payload carries producer/session UUIDs, sequence, observation timestamp, signal, state, provenance, and bounded metadata. The receiver adds its receipt time. Identical UUID/payload retries acknowledge the original commit; conflicting reuse is rejected.

Derive deterministic intervals for human display. Keep independent signals independent. Mac live activity expires after30 seconds without a refresh; sample every2 seconds and refresh active signals every15 seconds. Restarts cannot extend old activity. Flag large clock skew and exclude it from precise recording associations. Existing transcript-only machine credentials gain no access to this context.

## Acceptance and rollout

- Receiver tests: authenticated bounded ingestion, durable retry/conflict behavior, ordering, session expiry, open gaps, escaping, and unchanged audio/transcript/identity state.
- Phone tests: repeated/nested callbacks, failed restart, quiet-hours crossing, relaunch, and delivery failures; then simulator build/tests.
- Mac probe: public CoreAudio works on this host, observer errors are unknown rather than stopped, and source/helper bytes are release-attested.
- Render a fixture showing overlapping Mac activity, phone interruption, and recording recovery.
- Follow OPERATIONS.md for baseline, private consistent backup, migration rehearsal twice, reviewed commit, private immutable release, existing-job reload, and independent readback. Do not push publicly.
- Report browser-specific playback, installed phone build, real calls, and locked-screen callback coverage separately until physically observed.

Keep the existing microphone, mixing, recording resumption, quiet-hours, upload, and speaker-enrollment policies unchanged.

## Review repairs and verified software evidence

The review required preserving overflowed stop/start observations, replaying the
original gap identity/time, keeping unseen termination/startup boundaries
unknown, marking sampled transitions uncertain, and rejecting incomplete helper
coverage. These cases now have regression checks. Only expendable periodic
refreshes may coalesce in the phone's fallback queue; unacknowledged transitions
are not discarded. A relaunch retries the immutable original stop observation;
legacy boundaries without a saved observation remain unknown.

The receiver suite passes 389 tests. The isolated iOS suite passes 58 tests with
no failures or skips, including pinned HTTPS audio delivery and more than 64
failed-enqueue transitions recovered through relaunch. A silent synthetic PCM
host trial detected output IO becoming active then inactive; it did not prove
audible sound or YouTube/X-specific attribution. A rendered synthetic Context
fixture showed open/recovered gaps, uncertainty, and hostile title text without
HTML injection. Both schema-migration rehearsal passes preserved all 47 existing
table snapshots and database integrity. Tests and fixtures are not live deployment
or physical call/locked-screen evidence.
