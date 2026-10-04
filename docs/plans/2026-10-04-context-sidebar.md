# Context grouping in the primary sidebar

The user's correction supersedes the previous flat-default UI decision. The
primary sidebar should organize recordings into meaningful contexts/topics,
not expose one-minute capture files as its main navigation unit.

## Scope

Use the already enabled and verified Grok topic worker's persisted suggestions.
Do not add a provider, transmit more types of private data, or change capture,
speaker enrollment, original audio, topic acceptance, or source retention.

## Group precedence

1. Saved human titles and boundaries.
2. Current validated topic segments, visibly inferred.
3. Explicit meeting intervals on uncovered sources.
4. Clearly labeled time groups or individual uncovered recordings.

Participant-change proposals remain advisory in Context. Observed phone/call/Mac
context is not proof of who spoke or that a conversation ended. Similar topic
titles do not justify merging independent model windows.

The projection reads only. Validate each proposal's complete source window
before slicing its segment anchors or clipping to a day. Changed, deleted or
inserted sources invalidate it. Manual overlap suppresses a whole inferred
segment, not only the intersecting piece. No implicit acceptance or writes.

## UX

Grouped sidebar is default. Meaningful titles, time ranges, source counts and
concise evidence labels appear directly in each group. Expand source recordings
or select the group for ordered full text. Name and Play remain separate.
Individual recordings remains a secondary view. Search can match a title or
source passage. Selection and grouping changes must not switch global playback.

## Acceptance

- Each day recording occurs exactly once before user-selected filters.
- Topic singleton, quiet, pending and expired recordings remain reachable.
- Stable IDs, deterministic overlap precedence and stale-proposal rejection.
- No model calls, mutations or schema changes on reads.
- Manual edits, explicit sample opt-in and uncertain-save locks remain intact.
- Default/flat routes, source deep links, search and regrouping recovery pass.
- Rendered desktop, 320px and 390px checks; full frontend and Python suites.
- Immutable release, consistent backup and live auth/index/identity checks.

One bounded Astra planning consultation selected this approach. Separate backend
and frontend workers have disjoint file ownership; the lead integrates, verifies
and owns rollout. Final review is conditional on execution evidence.
