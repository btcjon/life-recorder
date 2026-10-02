# Physical-device and listening gates

Keep experiment manifests, recordings and outputs in a private runtime directory.
These protocols collect evidence; they do not activate features automatically.

## Enhanced playback

Prepare at least twelve consented original/enhanced excerpt pairs representing
speech and noise. Match listening level, check waveform alignment and duration,
inspect clipping and boundaries, and listen to both versions. A private JSON list
contains original/enhanced paths and consented, level_matched, alignment_checked,
no_clipping, speech_boundaries_preserved, user_approved booleans. Run
`scripts/evaluate-playback-pairs.py --manifest /private/pairs.json`.
The original remains selectable and remains the recognition/enrollment source.

## Quiet upload

Collect seven representative days in existing shadow mode. Review every proposed
hold and include soft_speech, distant_speech, music, noise and incomplete cases.
Private reviews contain chunk_id, consented, speech_present and condition. Run
`scripts/evaluate-quiet-upload.py --db /private/inbox.sqlite3 --reviews /private/reviews.json`.
Zero observed misses is required, together with measured battery, storage and
network effects. Until that evidence exists, every clip uploads normally.

## Background place trial

Run matched four-hour recording sessions with place context off and with the
experimental background switch on. Record initial/final battery percentage,
location coverage and observation age, upload receipts, queue recovery and audio
continuity. Exercise stationary/walking/vehicle movement; vehicle does not imply
driving. Pass requires no lost audio or upload regression and at most three
additional battery percentage points. A failing trial remains opt-in/disabled.

## Cellular delivery

On the physical phone disable Wi-Fi and confirm the intended private network is
reachable. Record a fresh clip, verify its durable receipt, completed processing,
clip search/citation and original playback. Repeat with temporary connectivity
loss; queued audio must survive and upload exactly once after reconnection.
Record count/status evidence only. Phone settings and subjective listening require
human participation; server health or simulator builds cannot prove these tests.
