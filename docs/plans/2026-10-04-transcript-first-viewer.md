# Life Recorder simpler viewer plan

Prepared with Astra on October 4, 2026. The initial request was planning only.
The user subsequently authorized full implementation and verification.

Restore the familiar workflow: choose a recording, read its transcript, name its
speakers and listen in the same workspace. Keep the cleaner typography and
responsive layout. Remove the need to understand backend event types before
using the recorder.

## Why the current flow fails

The library mixes expandable groups, individual recordings and a second list of
derived speech events. These similar-looking rows do different things. Group
titles expand instead of opening text; speech events open audio-only details
with no links to their sources. Even Flat list still appends speech events.
Speaker bubbles say Play to assistive technology, then start playback and open
the naming dialog together. These are interaction problems, not color problems.

The current legacy viewer also has the audio-only speech-event branch. A literal
rollback would preserve that defect. Keep its useful transcript-and-speaker
workflow, not every inherited behavior.

## Recommended layout

Use a compact top navigation with Recordings and People visible. Put Review,
Context and Health under a labeled More menu; preserve their direct routes.
Below it, show a chronological recording list on the left and the reading
workspace on the right. Keep one persistent player at the bottom. Avoid a
separate large app sidebar beside the recording sidebar.

On a phone, show the list first. One tap opens the same reading workspace, with
a clear Back control restoring the selected row and scroll position. The player
remains reachable without covering transcript or naming controls.

Use restrained borders, compact rows, a clear selected state, readable contrast
and comfortable transcript line spacing. Limit reading width instead of scaling
text across a large empty panel. Keep existing light and dark themes, local
fonts, reduced motion and accessible touch targets. No new component framework,
decorative dashboard cards or generated imagery is needed.

## Recording list

Default to individual recordings, with unobtrusive hour or date headings. A row
shows time, duration, a transcript preview and an actionable status when needed.
Clicking it immediately opens available text; it does not start audio.

Keep date and search visible. Move quiet-recording and processing filters into
a labeled Filters control. Failed processing remains visible by default. Remove
the parallel speech-event rows and repetitive AI summary off notices.

Preserve saved titles, boundaries and edits in a secondary Groups view. A group
opens its source transcripts in time order, with explicit recording boundaries
and per-recording playback; expanding source details is a separate control.
Automatic proximity groups remain labeled as suggested groups, not established
conversations. Do not create new grouping rules or rewrite saved boundaries.

Search opens the recording containing the match, scrolls to the actual matching
passage and highlights it without changing the stored text. Grouped results must
retain the matching source recording rather than opening an unrelated preview.

## Reading and speaker naming

The default detail view contains a compact time heading, readable full
transcript, speaker controls and a clear Play recording button. No normal row
lands on a generic audio-only explanation when source text exists.

Speaker names or Unknown buttons open the existing identity picker. A separate
Play button auditions that speaker stretch. Naming must never start playback.
Show short stretches in a compact, clearly labeled disclosure; do not hide the
only available naming controls. Preserve confirmed versus unconfirmed names.

Keep the full transcript visible while speaker details load or fail. Do not
invent a chat transcript or assign individual words to people when the available
evidence supports only recording text and speaker time spans. Naming still acts
on exact source turn IDs with the current revision checks. Saving a reusable
voice sample remains a separate, unchecked opt-in.

Use distinct states: Processing transcript, No speech detected, Transcript
processing needs attention, Speaker analysis pending, and Audio no longer
retained. Missing audio must not hide an existing transcript. An empty People
catalog should explain how to add a person without claiming historical people
were restored. Recovering old identities is a separate data task.

## Listening options

Original recording is the default. Keep the player stable through filtering,
refresh and navigation, with an explicit now-playing label when the selected
recording differs from the audio still playing.

Offer related speech-only or enhanced clips inside Audio options, not as a
second library. Before playback, show the clip's real time range and every
included source recording. A derived clip spanning several recordings must not
be described as enhancement of just the selected recording. Source links open
the correct transcript and naming controls.

Do not use source-recording seconds to seek compressed speech-only audio.
Defer combined group playback, synchronized transcript highlighting and precise
word-level seek until source-span and playback-offset mapping is verified.
Preserve the original/enhanced choice without pretending that skipped silence
or enhancement preserves a one-to-one source clock.

## Implementation sequence

1. Build synthetic desktop and mobile states for a normal recording, unknown
   speaker, missing audio, pending processing and a derived multi-source clip.
   Verify hierarchy and the click path before changing production.
2. Simplify list construction and selection in `web/src/lib/recordings.ts` and
   `web/src/features/recordings/`. Add the group selection model without changing
   canonical group IDs or persisted boundaries. Keep old deep links resolvable;
   a speech-event link opens its source context and listening options.
3. Separate speaker naming from auditioning, expose derived listening in source
   context, and simplify navigation in `AppShell`. Reuse the existing APIs,
   identity dialog, player, private licensed components and safety logic.
4. Verify the complete flow before rollout. If current APIs cannot safely supply
   required source spans, revisions or pagination, stop and scope a minimal
   human-only read extension; do not fake timing or fetch every detail on a day
   load. Limit concurrent speaker reads and fetch visible/selected content.
5. After separate implementation authorization, commit reviewed source, create
   a frozen manifest-covered bundle, take the required private runtime backup,
   and reload the same job. Verify the deployed UI and auth boundaries. Rollback
   changes source or the UI-default flag, never restores an older database.

## Acceptance checks

- One click from any normal recording row shows its available full transcript,
  speaker naming controls and playback without a mode change or extra expansion.
- A saved or suggested group opens readable source transcripts, preserves titles
  and boundaries, and makes source recordings easy to find.
- Naming opens the picker without starting or seeking audio. Unknown names remain
  unknown unless explicitly assigned; sample enrollment stays off by default.
- Search exposes the exact matching passage. Loading, empty, failed and
  expired-audio states explain the missing capability without hiding valid text.
- Speaker loads, filters, refresh, Back and stale responses cannot switch audio
  to the wrong recording. Original and derived clocks are not confused.
- Desktop, 320px and 390px views retain readable text, visible naming/play controls,
  keyboard focus, usable Back and no horizontal overflow.
- Existing local and remote authentication, machine restrictions, revision
  conflicts and uncertain-save readback remain intact. Mutation tests use only
  synthetic data; live checks do not rename people or enroll samples.
- The user walkthrough covers find, read, listen, name and return. Passing unit
  tests or a polished screenshot alone does not establish good usability.

## Evidence and limits

This plan uses the supplied October 4 screenshot and current source inspection:
`RecordingList.tsx`, `RecordingDetail.tsx`, `RecordingsView.tsx`, list/playback/route
helpers, `AppShell.tsx`, and the legacy `receiver/viewer.py`. Astra recommended a
flat default, distinct Play and Name actions, contextual derived listening and
deferral of unverified synchronization. No new rendered prototype was built or
tested in this planning turn. Intermittent audible playback and historical
identity recovery remain separate unresolved issues.
