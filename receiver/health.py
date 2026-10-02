"""Bounded operational facts. Never reads transcript, audio, or credentials."""
from pathlib import Path
import shutil
import subprocess
import time

DELAY_SECONDS = 10 * 60
STORAGE_RESERVE_BYTES = 256 * 1024 * 1024
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def source_revision():
    """Capture once at process startup; an on-disk update is not deployment."""
    try:
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"], cwd=Path(__file__).resolve().parent,
            capture_output=True, text=True, timeout=2, check=True)
        revision = result.stdout.strip()
        return revision if len(revision) == 40 and all(c in "0123456789abcdef" for c in revision) else None
    except (OSError, subprocess.SubprocessError):
        return None


def processing(db, now, device_id=None):
    where = " WHERE device=?" if device_id is not None else ""
    args = (device_id,) if device_id is not None else ()
    row = db.execute("""SELECT count(*) AS received,
        sum(status='complete') AS complete, sum(status='pending') AS pending,
        sum(status='needs_attention') AS needs_attention,
        sum(status='pending' AND attempts>0) AS retrying,
        min(CASE WHEN status='pending' THEN received END) AS oldest_pending_at,
        max(received) AS last_received_at, max(completed_at) AS last_completed_at
        FROM chunks""" + where, args).fetchone()
    data = {key: int(row[key] or 0) for key in ("received", "complete", "pending", "needs_attention", "retrying")}
    data.update({key: row[key] for key in ("oldest_pending_at", "last_received_at", "last_completed_at")})
    age = max(0, now - row["oldest_pending_at"]) if row["oldest_pending_at"] is not None else None
    data["oldest_pending_age_seconds"] = age
    data["delayed"] = age is not None and age >= DELAY_SECONDS
    data["state"] = ("needs_attention" if data["needs_attention"] else "delayed" if data["delayed"]
                     else "pending" if data["pending"] else "idle")
    data["receipt_means"] = "audio_stored"
    data["complete_means"] = "processed_may_be_quiet"
    return data


def snapshot(inbox, device_id=None, now=None):
    now = time.time() if now is None else now
    with inbox.connect() as db:
        queue = processing(db, now, device_id)
        result = {"version": 1, "checked_at": now, "processing": queue}
        # The phone receives only its own queue facts, never receiver-wide totals.
        if device_id is not None:
            return result
        stages = {}
        for name, column in (("asr", "status"), ("diarization", "diarization_status"), ("vad", "vad_status")):
            counts = {r[0]: r[1] for r in db.execute(
                f"SELECT {column},count(*) FROM chunks GROUP BY {column}")}
            stages[name] = {"enabled": inbox.health_stages.get(name), "counts": counts}
        stages["enhancement"] = {"enabled": inbox.health_stages.get("enhancement"), "counts": {
            r[0]: r[1] for r in db.execute("SELECT enhancement_status,count(*) FROM speech_events GROUP BY enhancement_status")
            if r[0] is not None}}
        names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        stages["summaries"] = {"enabled": inbox.health_stages.get("summaries"), "counts": {
            r[0]: r[1] for r in db.execute("SELECT status,count(*) FROM event_summaries GROUP BY status")
        } if "event_summaries" in names else {}}
        index = db.execute("SELECT schema_version,search_generation FROM agent_api_state WHERE id=1").fetchone()
        result["agent_index"] = {"schema_version": index[0], "generation": index[1],
                                 "last_reconciled_at": inbox.last_index_reconciled_at}
        audio = db.execute("""SELECT coalesce(sum(audio_bytes),0),
            coalesce(sum(audio_pinned!=0),0), coalesce(sum(CASE WHEN audio_pinned!=0 THEN audio_bytes ELSE 0 END),0),
            coalesce(sum(audio_bytes IS NULL),0) FROM chunks WHERE audio_state='present'""").fetchone()
        audio_bytes, pinned_count, pinned_bytes, unknown = audio
        # Older rows may lack cached sizes. Bound metadata probes and never follow
        # a ledger path outside the managed original-audio directory.
        for row in db.execute("""SELECT path,audio_pinned FROM chunks
            WHERE audio_state='present' AND audio_bytes IS NULL LIMIT 100"""):
            try:
                path = Path(row[0]).resolve()
                path.relative_to(inbox.audio.resolve())
                size = path.stat().st_size
            except (OSError, ValueError):
                continue
            audio_bytes += size
            pinned_bytes += size if row[1] else 0
            unknown -= 1
    try:
        free = shutil.disk_usage(inbox.root).free
        storage = {"available_bytes": free, "reserve_bytes": STORAGE_RESERVE_BYTES,
                   "low_space": free < STORAGE_RESERVE_BYTES + MAX_UPLOAD_BYTES,
                   "state": "low" if free < STORAGE_RESERVE_BYTES + MAX_UPLOAD_BYTES else "ok"}
    except OSError:
        storage = {"available_bytes": None, "reserve_bytes": STORAGE_RESERVE_BYTES, "low_space": None, "state": "unavailable"}
    result.update(storage=storage, stages=stages,
                  runtime={"started_at": inbox.health_started_at, "source_revision": inbox.health_source_revision},
                  state="needs_attention" if queue["needs_attention"] or storage["state"] != "ok"
                  else "delayed" if queue["delayed"] else "ok")
    storage.update(original_audio_bytes=audio_bytes, pinned_count=pinned_count,
                   pinned_audio_bytes=pinned_bytes, audio_usage_complete=unknown == 0)
    return result
