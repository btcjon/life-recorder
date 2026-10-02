# Receiver operations

This is the current maintenance runbook for the Mac installation. [README.md](README.md) describes behavior; [INSTALL-NOTES.md](INSTALL-NOTES.md) records dated observations. Keep runtime state outside this repository and never paste tokens, audio, transcripts, JWTs, or private keys into a command log.

## Installed boundaries

The launch job is `com.browseruse.life-recorder.receiver` in the current user's GUI domain. The private data directory is `~/Library/Application Support/LifeRecorder`; the active job must be inspected rather than inferred from this example. HTTPS upload listens on port 8766 and requires the private receiver bearer token. The viewer listens on `127.0.0.1:8767`; remote access, if configured, is Cloudflare Access to that viewer only. `agent_api_state.schema_version` is the agent-index schema; it is not SQLite's `PRAGMA user_version`.

After the authorized October 2 rebuild, the active runtime is `~/Library/Application Support/LifeRecorder-Rebuilt-20261002`. The original `LifeRecorder` directory remains untouched; `LifeRecorder-Recovery-20261002` holds preservation copies and five recovered WAVs outside retention. The old incomplete runtime is **not a working rollback target**. On failure, stop the rebuilt job and preserve its database and every newly accepted upload; never replace them with the old incomplete database. Inspect the loaded job before using any path. Remote summaries remain disabled. Transcript-only topic analysis was separately enabled through the existing xAI route during the later completion rollout; missing/expired credentials fail closed. Selectable local noise enhancement was restored during the October 2 roadmap rollout; original playback remains the default. See INSTALL-NOTES.md for observed state.

Topic credentials are read-only to this receiver. Renew expired OAuth through Pi's
existing account flow; do not add a separate refresh writer to its shared auth
file. Inspect sanitized topic health and per-job model/source/code fingerprints.
Disabling `LIFE_RECORDER_TOPIC_ANALYSIS` stops future requests without deleting
source clips or silently accepting existing suggestions. Plist environment changes
need reload of this same job; preserve the loaded configuration first.

## Before a restart

### Verified source release

Commit reviewed receiver changes first. Build a private source-only release:

```sh
python3 scripts/package-receiver-release.py \
  --output-root /absolute/private/runtime/source-releases
```

The output gives an exact `receiver_script`, committed source revision and
`manifest_sha256`. After the safety checks below, point the existing job's script
argument at that returned path and set `LIFE_RECORDER_SOURCE_MANIFEST_SHA256` to
the returned digest. Preserve every other argument/environment value. Reload the
same job when changing its plist. Never edit or overwrite a deployed readonly
release; create a new committed release. The four exact viewer PNG assets are
also hash-checked; no recording/database/token is packaged.
This is a source deployment artifact, not a runtime-backup service.

The receiver verifies its complete Python tree before application imports. A
missing or mismatched pin, mutable source, added/deleted file, symlink or bytecode
cache produces explicit unavailable provenance, not a guessed Git HEAD. Health
caches the verified launch revision; it never follows later checkout changes.
An already running unverified process cannot be retroactively attested. Local
development checkout jobs therefore report source identity unavailable.

Speaker maintenance diagnostics are read-only and aggregate-only:

```sh
/opt/homebrew/bin/python3 scripts/voice-diagnostics.py --db '/absolute/private/runtime/inbox.sqlite3'
```

The report contains profile readiness/sample counts, label counts, and queue age/attempt/error-class counts, never transcripts or voice vectors. Background recovery leaves unchanged enrolled samples untouched rather than repeatedly requeuing all unknown recordings. A current human speaker label records the explicit enrollment decision for that recording: legacy recovery will not enroll other stretches in it automatically; each may still be enrolled through an explicit human opt-in. Automatic identity thresholds remain unchanged.

1. Inspect `git status`, the intended commit, and the loaded launch job. Confirm its Python executable, `receiver.py` source path, data directory, port, environment *key names* and listener addresses. Do not dump environment values or pairing files.
2. Check authenticated and unauthenticated upload `/health` (200 with `viewer: ok`, and 401 respectively). Record the PID, start time, queue/status counts, agent-index schema and generation, event-ID count, keyed/FTS transcript counts, a count-only representative FTS query, and SQLite integrity. Record only a fingerprint of the cursor key, not the key.
3. Create a dated, mode-0700 backup directory outside Git. Use SQLite's backup API for a consistent `inbox.sqlite3` copy while the service is running; verify `PRAGMA integrity_check` on that copy. Privately copy the currently loaded launch plist and source used by the process. Do not copy or publish credentials into the repository.
4. Rehearse any schema migration against a *second copy* of the backup, twice. Confirm search hits, event IDs, cursor key, generation, and integrity remain stable on the no-op pass. Stop if any check fails.
5. Run the full Python suite and `git diff --check`; review the exact staged paths for secrets and unrelated files. Commit the source/docs before restarting. Do not push unless separately requested.

## Restart and verify

Use the existing loaded job: `launchctl kickstart -k gui/$(id -u)/com.browseruse.life-recorder.receiver`. Do **not** rerun `setup.py --install-agent` merely to restart: it can rewrite launch configuration. Confirm a new PID and start time, then confirm `*:8766` for upload and `127.0.0.1:8767` for the viewer. Recheck authenticated health (200, `viewer: ok`) and unauthenticated health (401).

Check the live SQLite database with read-only access: `agent_api_state.schema_version`, event-ID continuity, cursor-key fingerprint, keyed and FTS counts, a count-only MATCH query, and `PRAGMA integrity_check`. A normal subsequent reconciliation must not empty the FTS index. Check that the local viewer loads a day/detail and can serve an existing retained audio item where one exists. Check that an unauthenticated remote request is blocked and that machine credentials cannot use human/audio/mutation routes. If no machine credential is available for a safe live probe, report that limit; the test suite is not a substitute for a live authorized request. Review only new, sanitized error-log entries and observe the service for a bounded interval. A fresh iPhone upload requires a separate phone-side check; do not infer one from `/health`.

For a processing-recovery rollout, also compare count-only `pending`/`needs_attention`/`complete` totals against the baseline. One completed processing job may have an empty transcript; do not equate `complete` with spoken words. Never bulk-retry attention clips or discard their original audio. The viewer's **Retry processing** acts on one selected clip and is unavailable when the original source is missing.

## If verification fails

Stop further rollout. Retain the failed database, new logs, and every post-backup upload. Restore only a validated source/configuration/database combination, with the job stopped; never overwrite the live database with an older backup while it is running or after accepting newer clips without first reconciling them. Prefer a code-only rollback when the database is healthy. Recheck health, listeners, and data integrity after recovery. Record actual results and remaining uncertainty in INSTALL-NOTES.
