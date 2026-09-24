# Life Recorder project guide

Life Recorder is a private iPhone recorder plus a Mac receiver/viewer. Read this file, then [README.md](README.md) for current behavior and [OPERATIONS.md](OPERATIONS.md) before touching the installed receiver. [INSTALL-NOTES.md](INSTALL-NOTES.md) is a dated journal, not the current architecture contract.

## Repository map

- `ios/`: native capture, quiet hours, local queue, and pinned HTTPS upload.
- `receiver/receiver.py`: upload service, private SQLite ledger, processing, and retention.
- `receiver/viewer.py`: loopback viewer, event presentation, and human routes.
- `receiver/event_edits.py`, `speaker_review.py`: revisioned human event boundaries and read-only uncertain-speaker review.
- `receiver/agent_api/`: restricted, read-only machine search and event reads.
- `receiver/asr.py`, `voice_id.py`, `event_summaries.py`: local speech processing, speaker identity, and optional summaries.
- `tests/`: unit and browser probes. `scripts/`: narrowly scoped build/processing aids.

## Working rules

- Preserve existing dirty changes. Inspect status and diffs before editing; never reset or blanket-stage a shared checkout.
- Keep recordings, transcripts, SQLite databases, pairing pages, certificates, tokens, service credentials, device IDs, model files, and generated media outside Git. Do not print their contents in logs or handoffs.
- Do not confuse the three boundaries: the iPhone uploads to authenticated HTTPS port 8766; the viewer binds to `127.0.0.1:8767`; optional remote access reaches only the viewer through Cloudflare Access. Machine credentials may search/read text, not audio or human/mutation routes.
- Speaker labels are evidence-sensitive. Keep unconfirmed matches distinct from human-confirmed names; do not turn automatic matches into enrollment samples.
- A human label does not enroll a reusable voice sample unless `use_sample` is explicitly true; keep that opt-in and the conservative auto-match thresholds intact.
- Event groups, speech-event playback spans, and Markdown capture sessions are different concepts. See README before changing their thresholds.
- The original audio remains the recognition source; enhanced audio is a disposable playback derivative. Remote event summaries may send transcript text to Grok only when explicitly enabled.

## Verify and roll out

Run `/opt/homebrew/bin/python3 -m unittest discover -s tests -p 'test_*.py' -q` and `git diff --check` for receiver changes; run the relevant Xcode build/tests for iOS changes. Verify a rendered viewer state when changing UI. Tests alone do not prove a live phone upload.

Before a receiver restart, follow [OPERATIONS.md](OPERATIONS.md): inspect the loaded launch job, make a private consistent SQLite backup, rehearse any migration on a copy, and record baseline health and identity counts. Restart the existing job rather than rerunning setup. Afterward verify authenticated health, listener scope, schema and search integrity, viewer access, and logs. Never restore an old database over new uploads or while the service runs. A Git commit is not a deployment; record the observed rollout separately in INSTALL-NOTES.
