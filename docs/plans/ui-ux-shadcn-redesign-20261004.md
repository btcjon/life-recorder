# Life Recorder UI/UX redesign: premium shadcn components

Date: 2026-10-04. Status: implementation and acceptance checks completed; live rollout is recorded separately in INSTALL-NOTES.md.

Requested by Jon as a separate Astra plan. Astra (`gpt-6-astra`, low) recommended the architecture, screen priorities and acceptance gates below. The lead inspected the current viewer and real template files, and expanded the recommendation into an implementation plan. The existing recorder and context deployment are separate work.

## Goal and scope

Replace the Mac web viewer's hand-built interface with a coherent, responsive interface using the existing paid ShadcnDashboard Pro components. Make the daily work fast: find a recording, read it, play the right stretch, identify a speaker, and understand capture/processing/context state without confusing evidence with guesses.

Primary surface is the MacBook browser. The same viewer must work on a narrow phone browser. Native iPhone capture UI remains SwiftUI: the React components cannot be dropped into it. A later native visual-alignment plan can reuse the design language, not the React source.

No recording/processing-model changes, database redesign, new hosting, auth provider, public release, phone recording activation, or feature expansion is included.

## Recommendation

Build a small React/TypeScript/Vite static frontend under `web/`, initially mounted at `/ui-v2/` by the existing Python viewer on port 8767. Reuse selected premium layout and feature components, plus their minimal primitive dependency closure. Keep the Python APIs, authentication, audio range delivery and permissions authoritative.

This is actual component reuse, not a CSS imitation. Do not install the complete admin template into Life Recorder. Do not introduce Next.js, a Node production server, Supabase, template authentication, MSW, or a parallel database. Vite is a build/development tool; deployed JS/CSS/fonts are local static assets.

Keep the legacy `/` viewer available throughout migration. New assets must be part of the verified immutable deployment artifact; no mutable checkout or development server becomes the production viewer. Route names and artifact directories below are proposed, not existing capabilities.

Alternative considered: restyle the current vanilla viewer. That is a smaller initial change, but it does not directly reuse the paid React components and leaves the large embedded UI monolith intact. Use it only if the initial React integration spike finds an unresolved blocker.

## Verified sources and limits

- App owner: `/Users/jonbennett/Library/CloudStorage/Dropbox/Projects/life-recorder`; contracts are `AGENTS.md` and `README.md`, with rollout procedure in `OPERATIONS.md`.
- Existing browser interface: `receiver/viewer.py`, embedding HTML/CSS/JavaScript and serving human APIs. It covers Recordings, Review, People, Context, places/timeline controls, and health.
- Paid source: `/Users/jonbennett/Library/CloudStorage/Dropbox/Projects/shadcndashboard/ReactJS/main`. React/Vite, TypeScript, Tailwind v4, Base UI/Radix wrappers and locally supplied fonts are present. Next.js variants and `shadcn-dashboard-uikit.fig` also exist, but are not needed for this architecture.
- Trust actual files, not stale template descriptions: its README describes CRA although `package.json` uses Vite; its AGENTS refers to `src/index.css`, while the entry point imports `src/css/globals.css`.
- `src/main.tsx` starts a mock service worker. Do not copy that entry point or demo data contexts. Feature layouts are adaptation sources, not production domain logic.
- No license file was found in the kit inventory. Jon identifies it as paid. Purchase does not by itself establish source-redistribution permission; verify terms before committing paid source to any shared/public repository or publishing compiled bundles. Plan writing does not require distribution.
- Current deployment's 389 Python and 58 iOS passing tests are baseline evidence, not evidence that this redesign works.

## UX direction

Use a quiet, content-first workspace: neutral surfaces, one restrained accent, clear type hierarchy, compact navigation and visible selection. Avoid decorative KPI cards, commerce widgets, fake avatars/counts and a full template customizer. Support light/dark mode with local fonts and consistent semantic colors; uncertainty must be expressed in text as well as color.

Navigation: Recordings, Review, People, Context; Health/status remains easily reachable from the shell. Recordings opens first. A top toolbar holds date, search and relevant filters. Desktop uses a compact nav rail, recording/event list and readable detail pane. Mobile uses one primary pane at a time, with the template's Sheet/back pattern and reliable return-to-selection behavior.

Playback controls belong to a stable player component, outside frequently refreshed lists. Keep source, time and original/enhanced selection visible. Navigation must not silently restart audio or switch sources. Do not fabricate a waveform or transcript word timing; use only available timing data and normal seek/progress controls.

## Component mapping

All source paths in this table are relative to the paid React kit above. Adapt visual/interaction structure while replacing template data and operations with Life Recorder contracts.

| Life Recorder surface | Actual kit sources | Adaptation |
| --- | --- | --- |
| Navigation and workspace shell | `src/layouts/full/vertical/sidebar/Sidebar.tsx`, `src/components/ui/sidebar.tsx` | Remove sales/profile/demo links; retain collapse and mobile navigation |
| Recordings list/detail | `src/components/apps/email/index.tsx`, `email-list.tsx`, `email-content.tsx` | Replace email fields/actions with real events, clips and transcript; retain responsive master/detail layout |
| Resizable desktop panes | `src/components/ui/resizable.tsx` | Accessible sizing and minimum readable widths; mobile single-pane fallback |
| People catalog and detail | `src/components/apps/contacts/contact-list.tsx`, `src/components/ui/combobox.tsx` | Existing people IDs/readiness/sample provenance; no imported contact deletion semantics |
| Speaker review and identity editor | `src/components/ui/card.tsx`, `dialog.tsx`, `combobox.tsx`, `checkbox.tsx`, `badge.tsx` | Explicit unconfirmed suggestions; human label and sample enrollment remain separate |
| Context, gaps and health | `src/components/ui/card.tsx`, `alert.tsx`, `badge.tsx`, `tabs.tsx` | Evidence age, uncertainty, gaps and backend states; no inferred playback attribution |
| Mobile detail and confirmation flows | `src/components/ui/sheet.tsx`, `alert-dialog.tsx`, `button.tsx` | Focus restoration, useful accessible titles, explicit destructive confirmations |
| Loading/errors/feedback | `src/components/ui/skeleton.tsx`, `empty.tsx`, `sonner.tsx` | Real loading/empty/error/unauthorized states; persistent error content where action is needed |

## Implementation tasks

### 1. Inventory and integration spike

Create `web/` with a small package manifest/lockfile and selected components. Record source provenance and required dependencies in `web/COMPONENTS.md`. Review redistribution terms before licensed code enters a shared repository. Inspect the source dependency closure rather than taking its entire package manifest. Use the real `globals.css` token/font definitions selectively, keeping only the chosen visual variant.

Build a synthetic shell/list/detail fixture first. No real transcripts or audio in screenshots or fixtures. Confirm Base UI, Sheets, tooltips and resizable panels work under the current self-only CSP. The template uses React style props: test the production build in the browser and prefer external CSS/classes. If compatibility requires a policy change, document the exact affected directive and smallest safe change; do not add blanket unsafe script/style allowances, `eval`, or a CDN.

Acceptance: production bundle mounts; local fonts load; no MSW, demo fetch, telemetry, external requests or private content; keyboard and mobile navigation work under the deployed-policy equivalent. Freeze architecture only after this spike passes.

### 2. Same-origin shell, auth and static delivery

Add an isolated human API adapter at `web/src/api/client.ts` and domain types from current response shapes. Preserve local token extraction/clearing from the URL fragment and current session storage behavior; remote mode uses existing Cloudflare Access, not a new login. Never place tokens in URLs, component logs, build output or localStorage. Keep server authorization checks unchanged.

Extend `receiver/viewer.py` to serve only allowlisted `/ui-v2/` assets and a controlled entry document; no arbitrary filesystem fallback, traversal or symlink escape. API/audio paths must never fall through to SPA HTML. Use Vite's `/ui-v2/` base and explicit deep-link behavior compatible with the fragment used for initial auth; do not overload that fragment with router state.

Acceptance: local and remote auth, direct link/refresh, missing/expired auth, GET/HEAD behavior, malformed paths, MIME types, CSP, no-store content and machine-credential exclusions pass focused tests. No dependency on a running Node server.

### 3. Recordings and playback first

Implement `web/src/features/recordings/` and a stable `web/src/components/AudioPlayer.tsx`. Migrate day/search, event groups and flat view, retained/quiet/pending/attention states, selected detail, transcript/brief turns, original/enhanced audio and span playback. Persist selection and player state across non-disruptive UI refreshes. Preserve current grouping/timing rules; do not reimplement them differently in the browser.

Acceptance: flow-by-flow comparison against the legacy viewer, including blank/quiet recordings, missing audio, unavailable enhancement, long transcripts, asynchronous responses and rapid selection changes. No stale response can overwrite the current selection. Playback cannot create new labels or mutate event boundaries.

### 4. Human edits, Review and People

Implement `web/src/features/review/` and `web/src/features/people/`, plus event boundary/title controls. Migrate every current human operation, including provenance, readiness, withdrawal, undo, rejected suggestions and merge/split/name previews where currently supported. Preserve IDs and read revisions; show conflicts and require a reread before retrying.

Suggested identities stay visibly unconfirmed and are never preselected. Labeling and reusable sample enrollment remain separate, with enrollment opt-in defaulting off. No page load, playback or navigation enrolls a voice. Do not inherit the template's generic contact-delete actions.

Acceptance: successful and conflicting mutation fixtures, inaccessible/expired audio, invalid inputs, undo/withdrawal, no accidental enrollment, escaped hostile text and keyboard dialog focus. Real naming operations are not run merely to test the interface.

### 5. Context, places/timeline and receiver health

Implement `web/src/features/context/` and `web/src/features/health/`. Cover current observed context, open/recovered gaps, place/timeline controls and health diagnostics. Preserve the distinctions between uploaded, processed, retrying, quiet, stale and unavailable.

Keep “overlapping context, not a recording source,” observer freshness and sampled-boundary uncertainty visible. Mac output IO must not be relabeled as audible sound or YouTube/X playback. Unknown/stale phone state is not inactivity. Preserve existing visibility-bounded polling and avoid overlapping requests; never recompute voice diagnostics merely by opening a screen.

Acceptance: synthetic active/inactive/unknown/stale/late evidence, open/recovered gaps, unavailable observer, missing audio and degraded-health fixtures. All existing controls are accounted for in the parity matrix.

### 6. Verification and immutable build packaging

Add component/integration tests and rendered browser fixtures. Retain the Python suite; adapt existing UI selectors/probes deliberately rather than dropping tests. Run iOS checks only if implementation changes shared contracts or iOS source.

Extend `scripts/package-receiver-release.py` and `receiver/health.py` together. Today they allow only Python and exact icon/Swift assets; they will reject JS/CSS/fonts. Define bounded, manifest-covered frontend artifact rules with MIME/path validation. Choose artifact/file budgets from the measured minimal build; don't remove bounds. Ensure the build consumes exact source and lockfile revisions, records licensed-source provenance privately as appropriate, and excludes source maps/private test data. Every served deployed asset must hash-match its release manifest.

Acceptance: complete flow parity; desktop and 320/390px phone views, zoom, dark/light mode, reduced motion, keyboard/focus/touch targets, readable contrast, no horizontal transcript overflow and stable audio. Security tests cover local bearer/remote Access, machine denial, HTML escaping, invalid origins, CSP and forbidden asset paths. Tampered assets fail attestation. No external requests in production.

### 7. Side-by-side rollout and rollback

Follow `OPERATIONS.md`: inspect loaded runtime, take a private consistent backup and baseline, package the exact immutable release, restart only the existing receiver job, then verify listener scope, source identity, original audio, search, speaker counts and event IDs. Use `/ui-v2/` while the legacy viewer remains the default. Record user-facing flow acceptance before switching the default entry point; leave a clear legacy return path during the initial rollout.

Rollback selects the prior UI/source release, not an old database. Never overwrite later uploads or speaker decisions with a stale backup. Record the actual deployed revision and checks in `INSTALL-NOTES.md` only after execution.

## Stop conditions and risks

- Unknown license terms block distributing paid source/bundles, not private design planning. Do not assume a public source repository permits vendoring paid files.
- If CSP compatibility or auth routing cannot be preserved in the spike, stop integration and narrow the approach before migrating feature flows.
- Template dependency sprawl is contained by selective import; do not bring sales apps, AI integrations, demo contexts, chart libraries or customizers without a real need.
- A master/detail layout can hide selection or trap keyboard focus. Explicit selection, back navigation and focus restoration are acceptance criteria, not assumed benefits of the template.
- Scope is UI parity and improved usability. Unimplemented recorder capabilities and real-call/browser-metadata verification remain separate work.

## Completion evidence

The authored static frontend, private component closure, human-flow parity,
bounded immutable build packaging, auth/CSP tests and rollback controls are
implemented. See `docs/ui-parity.md` for operations and safety invariants.
Acceptance: 408 Python tests, 53 frontend tests, lint/typecheck and production
build; 58 native tests remain valid because this redesign does not change iOS.
Astra's final review required uncertain proxy/abort save locking, which was
repaired and rechecked. Rendered checks use a separate synthetic Inbox, never
real mutation targets. Deployed revision/default and current readbacks belong
in INSTALL-NOTES.md, not inferred from this plan.
