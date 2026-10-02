"""Bounded operational facts. Never reads transcript, audio, or credentials."""
from pathlib import Path
import hashlib
import json
import os
import shutil
import stat
import time

DELAY_SECONDS = 10 * 60
STORAGE_RESERVE_BYTES = 256 * 1024 * 1024
MAX_UPLOAD_BYTES = 32 * 1024 * 1024


def source_identity(root=None, manifest_pin=None):
    """Attest a readonly launch release before application imports, never HEAD.

    Deployment builds the manifest from an exact commit; launchd separately
    pins its SHA. The process caches this result. No Git or runtime data reads.
    """
    result = {"state": "unavailable", "revision": None, "method": "pinned_readonly_release",
              "reason": "release_manifest_unconfigured"}
    pin = os.environ.get('LIFE_RECORDER_SOURCE_MANIFEST_SHA256') if manifest_pin is None else manifest_pin
    if not isinstance(pin, str) or len(pin) != 64 or any(c not in '0123456789abcdef' for c in pin):
        return result
    try:
        # Keep the lexical launch path until every component is checked. An
        # early resolve() could conceal a symlinked release directory.
        root = Path(__file__).parent if root is None else Path(root)
        root = root.absolute()
        for component in (root, *root.parents):
            if component.is_symlink():
                raise ValueError('release_symlink')
        root = root.resolve(strict=True)
        def readonly(path, directory=False):
            info = path.lstat()
            return (info.st_uid == os.getuid() and not info.st_mode & 0o222
                    and (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)))
        if not readonly(root, True) or not readonly(root.parent, True):
            raise ValueError('release_not_readonly')
        manifest = root / 'source-manifest.json'
        if not readonly(manifest) or manifest.stat().st_size > 32768:
            raise ValueError('release_manifest_invalid')
        raw = manifest.read_bytes()
        if hashlib.sha256(raw).hexdigest() != pin:
            raise ValueError('release_manifest_mismatch')
        metadata = json.loads(raw)
        revision, expected = metadata.get('source_revision'), metadata.get('source_files')
        if (metadata.get('version') != 1 or not isinstance(revision, str) or len(revision) != 40
                or any(c not in '0123456789abcdef' for c in revision)
                or not isinstance(expected, dict) or not 2 <= len(expected) <= 128
                or not {'receiver.py', 'health.py'} <= expected.keys()):
            raise ValueError('release_manifest_invalid')
        actual, total = {}, 0
        for count, path in enumerate(root.rglob('*'), 1):
            if count > 256:
                raise ValueError('release_capacity_exceeded')
            if path.is_symlink():
                raise ValueError('release_symlink')
            if path.is_dir():
                if path.name == '__pycache__' or not readonly(path, True):
                    raise ValueError('release_not_readonly')
                continue
            if path.suffix in ('.pyc', '.pyo'):
                raise ValueError('release_bytecode_cache')
            if path.suffix != '.py':
                if path == manifest:
                    continue
                raise ValueError('release_extra_file')
            if not readonly(path) or path.stat().st_size > 1024 * 1024:
                raise ValueError('release_source_invalid')
            content = path.read_bytes()
            total += len(content)
            if total > 4 * 1024 * 1024:
                raise ValueError('release_capacity_exceeded')
            actual[path.relative_to(root).as_posix()] = hashlib.sha256(content).hexdigest()
        if actual != expected:
            raise ValueError('release_source_changed')
        return {"state": "verified", "revision": revision, "method": "pinned_readonly_release",
                "manifest_sha256": pin, "source_file_count": len(actual)}
    except (OSError, ValueError, TypeError, AttributeError, RecursionError) as error:
        reason = str(error) if isinstance(error, ValueError) and str(error).startswith('release_') else 'release_manifest_unavailable'
        return dict(result, reason=reason)


def source_revision():
    """Compatibility helper; an unmanaged checkout is not loaded-code proof."""
    return source_identity()['revision']


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
        stages['topics'] = {'enabled': inbox.health_stages.get('topics'),
                            'status': getattr(inbox, 'topic_status', {'state': 'not_checked'})}
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
                  runtime={"started_at": inbox.health_started_at, "source_revision": inbox.health_source_revision,
                           "source_identity": dict(inbox.health_source_identity)},
                  state="needs_attention" if queue["needs_attention"] or storage["state"] != "ok"
                  else "delayed" if queue["delayed"] else "ok")
    storage.update(original_audio_bytes=audio_bytes, pinned_count=pinned_count,
                   pinned_audio_bytes=pinned_bytes, audio_usage_complete=unknown == 0)
    return result
