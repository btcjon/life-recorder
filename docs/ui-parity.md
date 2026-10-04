# Viewer parity and evidence

The new viewer reuses the owner's private ShadcnDashboard Pro primitives and
layout patterns. Source mapping and hashes are in `web/COMPONENTS.md`; vendor
files, fonts and builds remain ignored. The deployed receiver stays Python-only.

| Surface | Preserved operations |
| --- | --- |
| Recordings | Context groups default in the primary sidebar, inferred topic titles with saved-human precedence, explicit meetings and labeled time fallback, secondary individual sources, day/title/literal passage search, quiet/pending filters with failures visible, ordered source text and secondary boundary editing, separate naming/audition, retained audio, keep and guarded retry |
| Playback | Explicit play only; one stable player across navigation/filter refresh, truthful selected versus playing labels, contextual original/enhanced speech clips and cross-day source links, queued source-speaker spans, full-play clears pending span, stale requests discarded and blob URLs released |
| Review | Revision-consistent paging, local filter, explicit diagnostics, unconfirmed non-preselected suggestions, rejection, span/source open, confirmation with separate sample opt-in |
| People | Readiness and sample provenance, rename, sample/source open, confirmed withdrawal, conflict-safe undo, anonymous merge/split/name/remove-label previews; no inherited contact-delete action |
| Context | Visible-only observed-evidence refresh, uncertain open/recovered gaps, timeline/split/merge/topic accept/reject/tag, separate place revisions and save/delete controls |
| Health | Read-only processing/storage/pipeline/source and viewer-bundle state, stale refresh feedback; no automatic voice diagnostics or retries |

Identity, event and context writes do not automatically retry. Conflicts,
transport loss, dispatched-response aborts, 408 and 5xx can leave an uncertain
server outcome; a successful reread is required before another write. Labeling
never enrolls voice evidence without explicit consent.

Acceptance uses synthetic data only for mutation and screenshots. Frontend
tests cover navigation/player stability, stale reads, revision conflicts,
failed reload locks, enrollment defaults, missing originals, hostile strings,
proxy responses after commit, metadata-delayed full playback and StrictMode
cleanup. Python tests cover existing API permissions and immutable/static asset
delivery. Legacy tests remain in place.

Browser checks cover all five views, 320/390px single-pane navigation, long-name
and transcript wrapping, Back focus restoration, real blob audio controls,
local fonts, light/dark mode and the deployed CSP. The only additional style
permission is the exact hash of Base UI's fixed scrollbar rule; arbitrary
injected styles and inline scripts are not permitted. No CDN, MSW, demo data,
external telemetry or source maps are shipped.

Physical call/locked-screen callback coverage and YouTube/X tab attribution are
not software acceptance claims. Mac output IO is activity evidence, not proof
of audible playback or the recording source. See `context-awareness.md` for
those limits and `INSTALL-NOTES.md` for dated live rollout evidence.
