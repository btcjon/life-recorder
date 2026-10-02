# Planned improvements

Checked items are implemented in source; deployment status is recorded separately in INSTALL-NOTES.md. Unchecked items remain planned, and new permissions are not enabled by this roadmap.

## Prioritized delivery plan — October 2, 2026

The subsequent detailed plan is now implemented in source; deployment evidence
is recorded in INSTALL-NOTES.md and docs/implementation-20261002.md. Historical
recordings and voice profiles were not recovered. Experimental acceptance remains
separate from source completion.

1. **Visible health and source protection.** Health and device-scoped processing status are implemented. The user declined an additional runtime-backup system; Git does not protect recordings, transcripts, profiles or credentials. Private pre-migration safety copies remain required by OPERATIONS.md.
2. **Complete retrieval coverage, then relevance.** Make a single transcribed clip discoverable without falsely presenting it as a meeting. Keep citations valid through event regrouping. Expand the frozen benchmark with person/time filters, multi-event queries and negative cases before changing lexical ranking; consider local hybrid search only if measured misses justify it. Acceptance: single-clip discovery, valid anchored reads, no unsupported speaker attribution, and measured precision/recall versus baseline.
3. **Rebuild trustworthy speaker learning.** Provide clear enrollment readiness and explicit sample opt-in, uncertain suggestions, and reversible correction. Evaluate fresh consented samples on held-out recordings, reporting false assignments and abstentions before changing thresholds. Do not train on automatic labels. Cross-clip anonymous clusters, later naming and propagation come after this quality gate, with merge/split/undo and preservation of human corrections.
4. **Meaningful meeting timeline.** Combine manual start/end hints with suggested boundaries from gaps, participant evidence and topic changes. Keep suggestions editable; protect human overrides and label uncertainty. Acceptance: split/merge cases preserve every transcript span and stable source references, including missed manual end markers. Choose the topic-analysis method before implementation; no new external transcript flow by default.
5. **Optional place context.** Start with manual places, then opt-in foreground snapshots. Attach the latest observation with age/accuracy rather than forcing GPS each minute. Define expiry and deletion propagation. Last-known phone location requires a separate agent permission; background/motion work follows device battery testing. Acceptance: denied/revoked/stale/delayed fixes never block recording or leak through current transcript credentials.

Separate experiments: compare original/enhanced playback with user listening before restoring enhancement; keep quiet-upload suppression in shadow mode until speech-miss and battery/network measurements support activation. Fresh cellular-only upload testing remains a reliability check, not proof inferred from the successful connected-phone test.

Delivery evidence and remaining real-device/listening gates are tracked separately;
no remote-backup destination or key custody decision is pending under this scope.

## Agent transcript retrieval — planned, 2026-09-27

Based on the 2026-09-24 Opus review. Preserve the read-only machine routes and existing authentication boundary.

- [x] Freeze 120 synthetic questions with evidence ranges, tuning/held-out splits, person/time filters, multi-record queries and negatives. Limitations are reported by the evaluator.
- [x] Evaluate offline normalized/stemmed lexical indexing and weighted partial coverage. Recall 0.8333 misses target 0.90; production ranking is unchanged.
- [x] Implement match-centered excerpts with event ID, clip ID, clip-start timestamps, exact Unicode text offsets, and unknown passage attribution. Added October 2; live rollout status is recorded in INSTALL-NOTES.md.
- [x] Implement anchored transcript reads with bounded preceding context, lossless continuation and revision checks. Added October 2; live rollout status is recorded in INSTALL-NOTES.md.
- [x] Return bounded per-turn speaker/time information with confirmed, unconfirmed, and unknown states; quoted words retain unknown attribution.
- [x] Verify an authorized remote machine search/read and continued denial of audio, human-only, and mutation routes. Passed during the October 2 rebuild; see INSTALL-NOTES.md.
- [ ] Consider local hybrid semantic retrieval only if measured paraphrase misses remain after lexical improvements. No new external transcript flow by default.

Proposed targets, subject to evaluation design: recall@10 >= 0.90, precision@5 >= 0.70, zero incorrect speaker attributions in the test set, p95 retrieval < 300 ms on the target Mac, and responses <= 16 KiB. These are goals, not achieved results.

October 2 evaluation: `scripts/evaluate-agent-retrieval.py` provides 60 synthetic cases (45 literal, 10 natural-language, 5 paraphrase). Baseline `eaf160f` and the implementation both find 45 cases; all 45 matched previews now contain evidence versus zero before. Ranking/filter results are identical. The broader evaluation item above remains open: multiple-event/person-filter relevance cases and precision/recall evaluation need expansion before changing ranking. Location work remains planned.

## Location context hints — planned, 2026-09-27

Based on Astra's review: an off-by-default “place context” feature, not continuous GPS tracking. Useful queries include “budget discussion at the office.” Location is uncertain recording context, never proof of speaker identity or meeting attendance.

- [ ] Start with manual event/place tags and optionally one-shot foreground location snapshots when opening the recorder. Request When In Use permission, accept approximate accuracy, and allow denied or failed fixes without disrupting recording.
- [ ] Define user-named places and local coordinate matching. Keep location samples separate from audio chunks, with capture time, accuracy, source, place candidate, and association interval; do not extend one snapshot across the whole day.
- [ ] Preserve unknown, stale, ambiguous, and multiple-place states. Indoor uncertainty and travel must not automatically merge or split events. Evaluate location as a supporting hint alongside speech and time continuity.
- [ ] Define retention and deletion propagation for samples, derived labels, and indexes. Prefer discarding coordinates after deriving hints; set explicit expiry if coordinates must be retained.
- [ ] Keep location out of existing agent credentials and Grok summaries by default. Separately authorized retrieval may expose bounded place labels, uncertainty, and cited time spans, not precise location history.
- [ ] Test denied/revoked permissions, stale/poor fixes, neighboring venues, travel, unchanged event boundaries, zero default disclosure, and deletion propagation using synthetic fixtures.
- [ ] Defer background capture to a separately opted-in experiment with physical-device battery and coverage measurements. Audio recording alone does not guarantee background location coverage.

Implementation deliverables: capture policy, sample schema, event aggregation rules, privacy policy, and retrieval fixtures. Acceptance checks above are proposed, not performed.

### iOS access, per-clip context, and last-known location

Requested follow-on direction, still planned rather than implemented:

- [ ] Surface location context in the iOS app: last observation, its age and accuracy, common-place label, permission/sharing controls, and access to the authenticated web viewer.
- [ ] Attach the latest available location observation to each clip, including observation time, accuracy, and freshness status. This does not require a fresh GPS request every minute; never describe an old fix as current. Missing location must not block recording or upload.
- [ ] Evaluate adaptive updates while moving and reduced updates while stationary, subject to background authorization and measured battery/coverage results. Do not promise negligible resource use before device testing.
- [ ] Let the user define common places such as Home and Office, with uncertainty-aware local matching.
- [ ] Evaluate optional Core Motion activity hints: stationary, walking, and vehicle travel, including confidence/unknown states. Vehicle travel does not establish that the user is driving.
- [ ] Maintain a separate last-known-location record, not solely clip metadata, so a permitted location report can be emitted during silence without uploading silent audio. Define emission cadence, offline queue behavior, and retention before enabling it.
- [ ] Provide a separately authorized read-only agent query for the phone's last known location. Default to place labels; precise coordinates require explicit permission. Return observation time, server receipt time, age, accuracy, and available activity confidence, so delayed uploads cannot masquerade as live tracking.
- [ ] Make clear that this locates the phone, not necessarily its owner. Test silence, offline/delayed uploads, stale observations, permission revocation, and unauthorized agent queries. Existing transcript credentials must not silently gain location access.

Example proposed response: “Office · observed 3 minutes ago · approximately ±100 m · phone stationary.” This is illustrative, not an actual observation.

Apple references: [one-shot location requests](https://developer.apple.com/documentation/corelocation/cllocationmanager/requestlocation()) and [When In Use authorization](https://developer.apple.com/documentation/corelocation/cllocationmanager/requestwheninuseauthorization()).

No location capture, new iOS location permission, or agent location access is currently enabled by this plan.
