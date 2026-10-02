"""Opt-in phone location context. Local matching only; no inferred travel history.

Call ensure_schema from the receiver's migration/reconciliation transaction.
Coordinates are private inputs, never included in the public response helpers.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
import time
import uuid

from agent_api.errors import AgentError

RAW_TTL_SECONDS = 24 * 60 * 60
CLIP_FRESH_SECONDS = 5 * 60
OBSERVATION_LIMIT = 4096


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS named_places (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, latitude REAL,
        longitude REAL, radius_m REAL NOT NULL, revision INTEGER NOT NULL,
        deleted INTEGER NOT NULL DEFAULT 0)""")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS named_places_active_name ON named_places(LOWER(name)) WHERE deleted=0")
    db.execute("""CREATE TABLE IF NOT EXISTS location_observations (
        id TEXT PRIMARY KEY, device TEXT NOT NULL, captured_at REAL NOT NULL,
        received_at REAL NOT NULL, latitude REAL, longitude REAL, accuracy_m REAL,
        source TEXT NOT NULL, activity TEXT, status TEXT NOT NULL,
        place_id TEXT, place_name TEXT, resolution TEXT NOT NULL,
        payload_hash TEXT NOT NULL)""")
    db.execute("CREATE INDEX IF NOT EXISTS location_observations_time ON location_observations(captured_at DESC)")
    db.execute("""CREATE TABLE IF NOT EXISTS clip_place_context (
        chunk_id TEXT PRIMARY KEY, observation_id TEXT NOT NULL,
        place_id TEXT, place_name TEXT, resolution TEXT NOT NULL DEFAULT 'pending')""")
    db.execute("CREATE INDEX IF NOT EXISTS clip_place_context_place ON clip_place_context(place_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS event_place_tags (
        event_id TEXT PRIMARY KEY, place_id TEXT, revision INTEGER NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS location_delete_cutoffs (
        device TEXT PRIMARY KEY, cutoff REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS location_delete_requests (
        id TEXT PRIMARY KEY, device TEXT NOT NULL, cutoff REAL NOT NULL)""")
    db.execute("""CREATE TABLE IF NOT EXISTS location_observation_receipts (
        id TEXT PRIMARY KEY, device TEXT NOT NULL, payload_hash TEXT NOT NULL,
        disposition TEXT NOT NULL)""")


def _number(value, label, low, high):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or not low <= value <= high:
        raise AgentError(400, "invalid_input", f"{label} is outside its valid range.")
    return float(value)


def _text(value, label, maximum=128):
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise AgentError(400, "invalid_input", f"{label} must be a nonempty bounded string.")
    return value.strip()


def _revision(value):
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise AgentError(400, "invalid_input", "revision must be a positive integer.")
    return value


def _stamp(value):
    if not isinstance(value, str):
        raise AgentError(400, "invalid_input", "captured_at must be an RFC3339 timestamp.")
    try:
        stamp = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
        return stamp.timestamp()
    except (ValueError, OverflowError) as error:
        raise AgentError(400, "invalid_input", "captured_at must include a valid timezone.") from error


def _iso(stamp):
    return datetime.fromtimestamp(stamp, timezone.utc).isoformat(timespec="seconds")


def list_places(db):
    return [dict(row) for row in db.execute("SELECT id,name,latitude,longitude,radius_m,revision FROM named_places WHERE deleted=0 ORDER BY name,id")]


def save_place(db, body, place_id=None):
    required = {"name", "latitude", "longitude", "radius_m"}
    if not isinstance(body, dict) or set(body) != required | ({"revision"} if place_id else set()):
        raise AgentError(400, "invalid_input", "Place requires name, latitude, longitude, radius_m and an update revision.")
    values = (_text(body["name"], "name", 80), _number(body["latitude"], "latitude", -90, 90),
              _number(body["longitude"], "longitude", -180, 180), _number(body["radius_m"], "radius_m", 10, 100000))
    existing = db.execute("SELECT id FROM named_places WHERE deleted=0 AND LOWER(name)=LOWER(?) AND id != ?", (values[0], place_id or "")).fetchone()
    if existing:
        raise AgentError(409, "conflict", "An active place already has that name.")
    if place_id:
        row = db.execute("SELECT revision FROM named_places WHERE id=? AND deleted=0", (place_id,)).fetchone()
        if row is None:
            raise AgentError(404, "not_found", "Place was not found.")
        if row["revision"] != _revision(body["revision"]):
            raise AgentError(409, "stale_revision", "The place changed. Reload it.")
        updated = db.execute("UPDATE named_places SET name=?,latitude=?,longitude=?,radius_m=?,revision=revision+1 WHERE id=? AND revision=? AND deleted=0", (*values, place_id, body["revision"]))
        if updated.rowcount != 1:
            raise AgentError(409, "stale_revision", "The place changed. Reload it.")
    else:
        place_id = "place_" + uuid.uuid4().hex
        db.execute("INSERT INTO named_places(id,name,latitude,longitude,radius_m,revision) VALUES(?,?,?,?,?,1)", (place_id, *values))
    _generation(db)
    return dict(db.execute("SELECT id,name,latitude,longitude,radius_m,revision FROM named_places WHERE id=?", (place_id,)).fetchone())


def _generation(db):
    # Optional agent index may not exist during initial setup.
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='agent_api_state'").fetchone():
        db.execute("UPDATE agent_api_state SET search_generation=search_generation+1 WHERE id=1")


def delete_place(db, place_id, revision):
    row = db.execute("SELECT revision FROM named_places WHERE id=? AND deleted=0", (place_id,)).fetchone()
    if row is None:
        raise AgentError(404, "not_found", "Place was not found.")
    if row["revision"] != _revision(revision):
        raise AgentError(409, "stale_revision", "The place changed. Reload it.")
    deleted = db.execute("UPDATE named_places SET deleted=1,latitude=NULL,longitude=NULL,revision=revision+1 WHERE id=? AND revision=? AND deleted=0", (place_id, revision))
    if deleted.rowcount != 1:
        raise AgentError(409, "stale_revision", "The place changed. Reload it.")
    db.execute("UPDATE location_observations SET place_id=NULL,place_name=NULL,resolution='place_deleted' WHERE place_id=?", (place_id,))
    db.execute("UPDATE clip_place_context SET place_id=NULL,place_name=NULL,resolution='place_deleted' WHERE place_id=?", (place_id,))
    db.execute("UPDATE event_place_tags SET place_id=NULL,revision=revision+1 WHERE place_id=?", (place_id,))
    _generation(db)
    return {"deleted": True, "id": place_id}


def parse_observation(body, device, now=None):
    now = time.time() if now is None else now
    if not isinstance(body, dict) or set(body) != {"observation"} or len(json.dumps(body).encode()) > OBSERVATION_LIMIT:
        raise AgentError(400, "invalid_input", "Send one observation in a JSON envelope of at most 4096 bytes.")
    item = body["observation"]
    if not isinstance(item, dict) or set(item) - {"id", "captured_at", "status", "latitude", "longitude", "accuracy_m", "source", "activity"}:
        raise AgentError(400, "invalid_input", "Unsupported observation fields.")
    if not {"id", "captured_at", "source"} <= set(item):
        raise AgentError(400, "invalid_input", "Observation requires id, captured_at and phone source.")
    status = item.get("status", "observed")
    if status not in ("observed", "denied", "unavailable", "revoked") or item["source"] not in ("foreground", "background"):
        raise AgentError(400, "invalid_input", "Observation requires a supported phone status and collection source.")
    captured = _stamp(item["captured_at"])
    if captured > now + 5:
        raise AgentError(400, "invalid_input", "Observation capture time is in the future.")
    activity = item.get("activity")
    if activity is not None and (not isinstance(activity, dict) or set(activity) != {"state", "confidence"}
        or activity["state"] not in ("stationary", "walking", "vehicle", "unknown")
        or activity["confidence"] not in ("low", "medium", "high", "unknown")):
        raise AgentError(400, "invalid_input", "Unsupported phone activity state or confidence.")
    coords = (None, None, None)
    if status == "observed":
        if not {"latitude", "longitude", "accuracy_m"} <= set(item):
            raise AgentError(400, "invalid_input", "Available observations require coordinates and accuracy.")
        coords = (_number(item["latitude"], "latitude", -90, 90), _number(item["longitude"], "longitude", -180, 180),
                  _number(item["accuracy_m"], "accuracy_m", 0, 100000))
    elif any(k in item for k in ("latitude", "longitude", "accuracy_m")):
        raise AgentError(400, "invalid_input", "Unavailable observations cannot carry coordinates.")
    parsed = {"id": _text(item["id"], "id"), "device": _text(device, "device"), "captured_at": captured,
              "latitude": coords[0], "longitude": coords[1], "accuracy_m": coords[2], "source": item["source"], "status": status, "activity": activity}
    parsed["payload_hash"] = hashlib.sha256(json.dumps(parsed, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
    return parsed


def _distance(lat1, lon1, lat2, lon2):
    a, b = math.radians(lat1), math.radians(lat2)
    dlat, dlon = b - a, math.radians(lon2 - lon1)
    h = math.sin(dlat/2)**2 + math.cos(a)*math.cos(b)*math.sin(dlon/2)**2
    return 6371000 * 2 * math.asin(min(1, math.sqrt(h)))


def _match(db, observation):
    if observation["status"] != "observed":
        return None, None, observation["status"]
    fits = [p for p in db.execute("SELECT id,name,latitude,longitude,radius_m FROM named_places WHERE deleted=0")
            if _distance(observation["latitude"], observation["longitude"], p["latitude"], p["longitude"]) + observation["accuracy_m"] <= p["radius_m"]]
    if len(fits) == 1:
        return fits[0]["id"], fits[0]["name"], "known"
    return None, None, "ambiguous" if len(fits) > 1 else "unknown"


def _resolve_clip(db, chunk_id, observation_id):
    clip = db.execute("SELECT device,started FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    obs = db.execute("SELECT * FROM location_observations WHERE id=?", (observation_id,)).fetchone()
    place_id, name, state = None, None, "pending"
    receipt = db.execute("SELECT device,disposition FROM location_observation_receipts WHERE id=?", (observation_id,)).fetchone()
    if clip is not None and receipt is not None:
        state = receipt["disposition"] if receipt["device"] == clip["device"] else "device_mismatch"
    if clip is not None and obs is not None:
        age = _stamp(clip["started"]) - obs["captured_at"]
        if clip["device"] != obs["device"]:
            state = "device_mismatch"
        elif age < 0:
            state = "future_observation"
        elif age > CLIP_FRESH_SECONDS:
            state = "stale"
        else:
            place_id, name, state = obs["place_id"], obs["place_name"], obs["resolution"]
    db.execute("UPDATE clip_place_context SET place_id=?,place_name=?,resolution=? WHERE chunk_id=?", (place_id, name, state, chunk_id))
    return {"chunk_id": chunk_id, "observation_id": observation_id, "place": {"id": place_id, "name": name} if place_id else None, "status": state}


def bind_clip(db, chunk_id, observation_id):
    observation_id = _text(observation_id, "observation_id")
    clip = db.execute("SELECT device,started FROM chunks WHERE id=?", (chunk_id,)).fetchone()
    if clip is None:
        raise AgentError(404, "not_found", "Clip was not found.")
    cutoff = db.execute("SELECT cutoff FROM location_delete_cutoffs WHERE device=?", (clip["device"],)).fetchone()
    if cutoff and _stamp(clip["started"]) <= cutoff["cutoff"]:
        return {"chunk_id": chunk_id, "observation_id": observation_id, "place": None, "status": "history_deleted"}
    previous = db.execute("SELECT observation_id FROM clip_place_context WHERE chunk_id=?", (chunk_id,)).fetchone()
    if previous and previous["observation_id"] != observation_id:
        raise AgentError(409, "conflict", "This clip already has an explicit observation pointer.")
    db.execute("INSERT OR IGNORE INTO clip_place_context(chunk_id,observation_id) VALUES(?,?)", (chunk_id, observation_id))
    result = _resolve_clip(db, chunk_id, observation_id)
    if previous is None:
        _generation(db)
    return result


def ingest_observation(db, device, body, now=None):
    now = time.time() if now is None else now
    item = parse_observation(body, device, now)
    cleanup(db, now)
    receipt = db.execute("SELECT device,payload_hash,disposition FROM location_observation_receipts WHERE id=?", (item["id"],)).fetchone()
    if receipt:
        if receipt["device"] != device or receipt["payload_hash"] != item["payload_hash"]:
            raise AgentError(409, "conflict", "Observation id already has different contents.")
        return {"id": item["id"], "stored": True, "duplicate": True, "ignored": receipt["disposition"]}
    previous = db.execute("SELECT payload_hash FROM location_observations WHERE id=?", (item["id"],)).fetchone()
    if previous:
        if previous["payload_hash"] != item["payload_hash"]:
            raise AgentError(409, "conflict", "Observation id already has different contents.")
        return {"id": item["id"], "stored": True, "duplicate": True}
    cutoff = db.execute("SELECT cutoff FROM location_delete_cutoffs WHERE device=?", (device,)).fetchone()
    ignored = "history_deleted" if cutoff and item["captured_at"] <= cutoff["cutoff"] else "expired" if item["captured_at"] <= now - RAW_TTL_SECONDS else None
    if ignored:
        db.execute("INSERT INTO location_observation_receipts(id,device,payload_hash,disposition) VALUES(?,?,?,?)", (item["id"], device, item["payload_hash"], ignored))
        for row in db.execute("SELECT chunk_id FROM clip_place_context WHERE observation_id=?", (item["id"],)).fetchall():
            _resolve_clip(db, row["chunk_id"], item["id"])
        return {"id": item["id"], "stored": True, "duplicate": False, "ignored": ignored}
    place_id, name, state = _match(db, item)
    lat, lon = item["latitude"], item["longitude"]
    db.execute("""INSERT INTO location_observations(id,device,captured_at,received_at,latitude,longitude,accuracy_m,source,activity,status,place_id,place_name,resolution,payload_hash)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""", (item["id"], device, item["captured_at"], now, lat, lon, item["accuracy_m"], item["source"], json.dumps(item["activity"]) if item["activity"] else None, item["status"], place_id, name, state, item["payload_hash"]))
    changed = False
    for row in db.execute("SELECT chunk_id FROM clip_place_context WHERE observation_id=?", (item["id"],)).fetchall():
        _resolve_clip(db, row["chunk_id"], item["id"])
        changed = True
    if changed:
        _generation(db)
    return {"id": item["id"], "stored": True, "duplicate": False}


def cleanup(db, now=None):
    now = time.time() if now is None else now
    cursor = db.execute("UPDATE location_observations SET latitude=NULL,longitude=NULL WHERE captured_at<=? AND (latitude IS NOT NULL OR longitude IS NOT NULL)", (now-RAW_TTL_SECONDS,))
    # Orphan context cannot retain labels after an explicit recording deletion.
    db.execute("DELETE FROM clip_place_context WHERE chunk_id NOT IN (SELECT id FROM chunks)")
    return {"raw_coordinates_expired": cursor.rowcount}


def last_known(db, now=None):
    now = time.time() if now is None else now
    row = db.execute("SELECT * FROM location_observations WHERE captured_at<=? ORDER BY captured_at DESC,received_at ASC,id ASC LIMIT 1", (now,)).fetchone()
    if row is None or now - row['captured_at'] >= RAW_TTL_SECONDS:
        return {"status": "unknown", "place": None, "observation_id": None, "captured_at": None, "received_at": None, "age_seconds": None, "accuracy_m": None, "source": None, "subject": "phone", "activity": None}
    stale = now - row["captured_at"] >= RAW_TTL_SECONDS
    return {"status": "stale" if stale else row["resolution"], "place": {"id": row["place_id"], "name": row["place_name"]} if row["place_id"] and not stale else None, "observation_id": row["id"],
            "captured_at": _iso(row["captured_at"]), "received_at": _iso(row["received_at"]), "age_seconds": max(0, round(now-row["captured_at"])),
            "accuracy_m": row["accuracy_m"], "activity": json.loads(row["activity"]) if row["activity"] else None, "source": row["source"], "subject": "phone"}


def place_filter(db, value):
    if not isinstance(value, str) or not value.strip() or len(value) > 128:
        raise AgentError(400, "invalid_input", "place must be an exact bounded place id or name.")
    rows = db.execute("SELECT id FROM named_places WHERE deleted=0 AND (id=? OR name=? COLLATE NOCASE)", (value.strip(), value.strip())).fetchall()
    if len(rows) != 1:
        raise AgentError(400, "invalid_input", "Use one known exact place id or name.")
    return rows[0]["id"]


def clip_context(db, chunk_id):
    row = db.execute("SELECT place_id,place_name,resolution FROM clip_place_context WHERE chunk_id=?", (chunk_id,)).fetchone()
    return {"status": row["resolution"], "place": {"id": row["place_id"], "name": row["place_name"]} if row["place_id"] else None} if row else {"status": "unknown", "place": None}


def delete_observation(db, observation_id):
    """Explicit human deletion also removes its persisted derived clip labels."""
    db.execute("DELETE FROM clip_place_context WHERE observation_id=?", (observation_id,))
    db.execute("""INSERT OR IGNORE INTO location_observation_receipts(id,device,payload_hash,disposition)
        SELECT id,device,payload_hash,'history_deleted' FROM location_observations WHERE id=?""", (observation_id,))
    deleted = db.execute("DELETE FROM location_observations WHERE id=?", (observation_id,)).rowcount
    _generation(db)
    return {"id": observation_id, "deleted": bool(deleted)}


def clear_history(db, device_id, request, now=None):
    """Explicit phone action, monotonic capture cutoff, idempotent durable ACK."""
    now = time.time() if now is None else now
    if not isinstance(request, dict) or set(request) != {"id", "occurred_at"}:
        raise AgentError(400, "invalid_input", "Delete history requires id and occurred_at.")
    request_id = _text(request["id"], "id")
    device = _text(device_id, "device")
    cutoff = _stamp(request["occurred_at"])
    if cutoff > now + 5:
        raise AgentError(400, "invalid_input", "Deletion time is in the future.")
    previous = db.execute("SELECT device,cutoff FROM location_delete_requests WHERE id=?", (request_id,)).fetchone()
    if previous:
        if previous["device"] != device or previous["cutoff"] != cutoff:
            raise AgentError(409, "conflict", "Delete request id already has different contents.")
        return {"id": request_id, "deleted": True, "duplicate": True}
    db.execute("INSERT INTO location_delete_requests(id,device,cutoff) VALUES(?,?,?)", (request_id, device, cutoff))
    db.execute("INSERT INTO location_delete_cutoffs(device,cutoff) VALUES(?,?) ON CONFLICT(device) DO UPDATE SET cutoff=MAX(cutoff,excluded.cutoff)", (device, cutoff))
    # Retain only receipt fingerprints so delayed retries cannot recreate data.
    db.execute("""INSERT OR IGNORE INTO location_observation_receipts(id,device,payload_hash,disposition)
        SELECT id,device,payload_hash,'history_deleted' FROM location_observations WHERE device=? AND captured_at<=?""", (device, cutoff))
    db.execute("""DELETE FROM clip_place_context WHERE observation_id IN
        (SELECT id FROM location_observations WHERE device=? AND captured_at<=?)
        OR chunk_id IN (SELECT id FROM chunks WHERE device=? AND julianday(started)<=julianday(?))""", (device, cutoff, device, datetime.fromtimestamp(cutoff, timezone.utc).isoformat(timespec="microseconds")))
    db.execute("DELETE FROM location_observations WHERE device=? AND captured_at<=?", (device, cutoff))
    _generation(db)
    return {"id": request_id, "deleted": True, "duplicate": False}


def event_place(db, event_id):
    row = db.execute("SELECT place_id,revision FROM event_place_tags WHERE event_id=?", (event_id,)).fetchone()
    return {"event_id": event_id, "place_id": row["place_id"] if row else None, "revision": row["revision"] if row else 0, "source": "manual_event_tag"}


def set_event_place(db, event_id, place_id, revision):
    if (db.execute("SELECT 1 FROM agent_events WHERE id=? AND tombstoned=0", (event_id,)).fetchone() is None
            and db.execute('SELECT 1 FROM event_edits WHERE id=?', (event_id,)).fetchone() is None):
        raise AgentError(404, "not_found", "Event was not found.")
    if place_id is not None:
        place_id = place_filter(db, place_id)
    row = db.execute("SELECT revision FROM event_place_tags WHERE event_id=?", (event_id,)).fetchone()
    expected = row["revision"] if row else 0
    if isinstance(revision, bool) or not isinstance(revision, int) or revision != expected:
        raise AgentError(409, "stale_revision", "The manual event tag changed. Reload it.")
    db.execute("INSERT INTO event_place_tags(event_id,place_id,revision) VALUES(?,?,1) ON CONFLICT(event_id) DO UPDATE SET place_id=excluded.place_id,revision=event_place_tags.revision+1", (event_id, place_id))
    _generation(db)
    return {"event_id": event_id, "place_id": place_id, "revision": expected+1, "source": "manual_event_tag"}


def tagged_clips(db, place_id):
    """Manual event context is separate from observed phone context."""
    import event_edits
    ids = set()
    rows = db.execute('SELECT id,started,duration,transcript FROM chunks').fetchall()
    chunks = event_edits._chunks(rows)
    for tag in db.execute('SELECT event_id FROM event_place_tags WHERE place_id=?', (place_id,)):
        edit = db.execute('SELECT start_chunk_id,end_chunk_id FROM event_edits WHERE id=?', (tag['event_id'],)).fetchone()
        if edit:
            span, error = event_edits._locate(chunks, edit['start_chunk_id'], edit['end_chunk_id'])
            if error is None:
                ids.update(item['id'] for item in span)
        else:
            ids.update(row[0] for row in db.execute('SELECT m.chunk_id FROM agent_event_members m JOIN agent_events e ON e.id=m.event_id WHERE e.id=? AND e.tombstoned=0', (tag['event_id'],)))
    return ids
