"""Manual meeting events, derived intervals, and conservative automatic closure."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from uuid import UUID
from zoneinfo import ZoneInfo

DISPLAY_ZONE = ZoneInfo("America/New_York")
SESSION_GAP = timedelta(minutes=15)
MAX_MEETING = timedelta(hours=4)
MAX_BODY = 4 * 1024
MAX_FUTURE = timedelta(minutes=5)
SCHEMA_VERSION = 2


def parse_utc(value: str) -> datetime:
    stamp = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if stamp.tzinfo is None:
        raise ValueError("Timestamp must have a timezone")
    return stamp.astimezone(timezone.utc)


def format_utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def valid_uuid(value: str) -> str:
    parsed = UUID(str(value))
    if str(parsed) != str(value).lower():
        raise ValueError("Invalid UUID")
    return str(parsed)


def next_quiet_boundary(started: datetime) -> datetime:
    local = started.astimezone(DISPLAY_ZONE)
    boundary = local.replace(hour=22, minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary = (local + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    return boundary.astimezone(timezone.utc)


def manual_deadline(started: datetime) -> datetime:
    started = started.astimezone(timezone.utc)
    return min(started + MAX_MEETING, next_quiet_boundary(started))


def eastern_dates(start: datetime, end: datetime | None) -> list[str]:
    begin = start.astimezone(DISPLAY_ZONE).date()
    finish = (end or start).astimezone(DISPLAY_ZONE).date()
    if finish < begin:
        finish = begin
    days = []
    cursor = begin
    while cursor <= finish:
        days.append(cursor.isoformat())
        cursor += timedelta(days=1)
    return days


def migrate_schema(db) -> None:
    version = db.execute("PRAGMA user_version").fetchone()[0]
    cols = {row[1] for row in db.execute("PRAGMA table_info(chunks)")}
    if "engine" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN engine TEXT")
    if "asr_model" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN asr_model TEXT")
    if "asr_summary" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN asr_summary TEXT")
    if "word_count" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN word_count INTEGER")
    if "speech_density" not in cols:
        db.execute("ALTER TABLE chunks ADD COLUMN speech_density REAL")
    additions = {
        "completed_at": "REAL", "audio_state": "TEXT NOT NULL DEFAULT 'present'",
        "audio_bytes": "INTEGER", "audio_expires_at": "REAL",
        "audio_pinned": "INTEGER NOT NULL DEFAULT 0", "words_json": "TEXT",
        "diarization_status": "TEXT NOT NULL DEFAULT 'pending'",
        "diarization_error": "TEXT", "diarization_attempts": "INTEGER NOT NULL DEFAULT 0",
        "diarization_retry_at": "REAL NOT NULL DEFAULT 0",
    }
    for name, declaration in additions.items():
        if name not in cols:
            db.execute(f"ALTER TABLE chunks ADD COLUMN {name} {declaration}")
    db.execute(
        """CREATE TABLE IF NOT EXISTS meeting_events (
            event_id TEXT PRIMARY KEY,
            meeting_id TEXT NOT NULL,
            device_id TEXT NOT NULL,
            kind TEXT NOT NULL,
            occurred_at TEXT NOT NULL,
            received_at REAL NOT NULL,
            payload TEXT NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS meeting_events_device ON meeting_events(device_id, meeting_id, occurred_at)"
    )
    db.execute("""CREATE TABLE IF NOT EXISTS people (
        id TEXT PRIMARY KEY, name TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_runs (
        id TEXT PRIMARY KEY, chunk_id TEXT NOT NULL, engine TEXT NOT NULL,
        status TEXT NOT NULL, speaker_count INTEGER, processing_seconds REAL,
        error TEXT, created_at REAL NOT NULL,
        FOREIGN KEY(chunk_id) REFERENCES chunks(id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS speaker_turns (
        id TEXT PRIMARY KEY, run_id TEXT NOT NULL, chunk_id TEXT NOT NULL,
        speaker_key TEXT NOT NULL, started REAL NOT NULL, ended REAL NOT NULL,
        quality REAL, embedding_json TEXT, person_id TEXT, label_source TEXT,
        FOREIGN KEY(person_id) REFERENCES people(id)
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS voice_samples (
        id TEXT PRIMARY KEY, person_id TEXT NOT NULL, turn_id TEXT NOT NULL UNIQUE,
        embedding_json TEXT NOT NULL, duration REAL NOT NULL, confirmed_at REAL NOT NULL,
        FOREIGN KEY(person_id) REFERENCES people(id)
    )""")
    db.execute(
        """CREATE TABLE IF NOT EXISTS intervals (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            source TEXT NOT NULL,
            meeting_id TEXT,
            label TEXT NOT NULL,
            started_at TEXT NOT NULL,
            ended_at TEXT,
            deadline TEXT,
            closed INTEGER NOT NULL DEFAULT 0,
            closure_reason TEXT,
            status TEXT NOT NULL,
            algorithm_version TEXT,
            reasons TEXT,
            chunk_ids TEXT
        )"""
    )
    extra = {row[1] for row in db.execute("PRAGMA table_info(intervals)")}
    if extra and "deadline" not in extra:
        db.execute("ALTER TABLE intervals ADD COLUMN deadline TEXT")
    if extra and "label" not in extra:
        db.execute("ALTER TABLE intervals ADD COLUMN label TEXT")
    if version < SCHEMA_VERSION:
        db.execute(f"PRAGMA user_version={SCHEMA_VERSION}")


def validate_event(payload: dict, now: datetime | None = None) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("Invalid event")
    if payload.get("version") != 1:
        raise ValueError("Unsupported event version")
    kind = payload.get("kind")
    if kind not in ("start", "end"):
        raise ValueError("Invalid event kind")
    event = {
        "version": 1,
        "event_id": valid_uuid(payload.get("event_id")),
        "meeting_id": valid_uuid(payload.get("meeting_id")),
        "device_id": valid_uuid(payload.get("device_id")),
        "kind": kind,
        "occurred_at": format_utc(parse_utc(payload.get("occurred_at"))),
    }
    extra = set(payload) - {"version", "event_id", "meeting_id", "device_id", "kind", "occurred_at"}
    if extra:
        raise ValueError("Unexpected event field")
    occurred = parse_utc(event["occurred_at"])
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if occurred > current + MAX_FUTURE:
        raise ValueError("Event timestamp is too far in the future")
    return event


def event_conflict(existing, event: dict) -> bool:
    return (
        existing["meeting_id"] != event["meeting_id"]
        or existing["device_id"] != event["device_id"]
        or existing["kind"] != event["kind"]
        or format_utc(parse_utc(existing["occurred_at"])) != event["occurred_at"]
    )


def chunk_span(row) -> tuple[datetime, datetime]:
    start = parse_utc(row["started"])
    duration = float(row["duration"] or 0)
    return start, start + timedelta(seconds=max(duration, 0))


def capture_gaps(rows) -> list[tuple[datetime, datetime]]:
    gaps = []
    previous_end = None
    for row in rows:
        start, end = chunk_span(row)
        if previous_end is not None and start - previous_end > SESSION_GAP:
            gaps.append((previous_end, start))
        previous_end = end if previous_end is None else max(previous_end, end)
    return gaps


def _close(interval: dict, at: datetime, reason: str) -> None:
    started = parse_utc(interval["started_at"])
    deadline = parse_utc(interval["deadline"])
    ended = min(max(at, started), deadline)
    interval["ended_at"] = format_utc(ended)
    interval["closed"] = 1
    interval["status"] = "closed"
    if reason == "explicit" and at <= deadline:
        interval["closure_reason"] = "explicit"
        return
    if reason == "later_start" and at < deadline:
        interval["closure_reason"] = "later_start"
        return
    if reason == "capture_gap" and at < deadline:
        interval["closure_reason"] = "capture_gap"
        return
    interval["ended_at"] = format_utc(deadline)
    if deadline == started + MAX_MEETING:
        interval["closure_reason"] = "deadline"
    else:
        interval["closure_reason"] = "quiet_hours"


def _new_manual(event: dict) -> dict:
    started = parse_utc(event["occurred_at"])
    deadline = manual_deadline(started)
    return {
        "id": f"manual:{event['device_id']}:{event['meeting_id']}",
        "device_id": event["device_id"],
        "source": "manual",
        "meeting_id": event["meeting_id"],
        "label": "Manual meeting",
        "started_at": format_utc(started),
        "ended_at": None,
        "closed": 0,
        "closure_reason": None,
        "status": "open",
        "deadline": format_utc(deadline),
        "algorithm_version": None,
        "reasons": ["manual"],
        "chunk_ids": [],
    }


def rebuild_manual_intervals(events, chunks, now: datetime | None = None) -> list[dict]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    by_device: dict[str, list] = {}
    for event in events:
        by_device.setdefault(event["device_id"], []).append(event)
    chunk_by_device: dict[str, list] = {}
    for row in chunks:
        chunk_by_device.setdefault(row["device"], []).append(row)
    intervals = []
    for device_id, device_events in by_device.items():
        ordered = sorted(device_events, key=lambda item: (item["occurred_at"], item["event_id"]))
        active = None
        pending_ends: dict[str, dict] = {}
        device_intervals = []
        for event in ordered:
            if event["kind"] == "end":
                if active and active["meeting_id"] == event["meeting_id"]:
                    _close(active, parse_utc(event["occurred_at"]), "explicit")
                    device_intervals.append(active)
                    active = None
                else:
                    pending_ends[event["meeting_id"]] = event
                continue
            if active:
                _close(active, parse_utc(event["occurred_at"]), "later_start")
                device_intervals.append(active)
            active = _new_manual(event)
            pending = pending_ends.pop(event["meeting_id"], None)
            if pending:
                _close(active, parse_utc(pending["occurred_at"]), "explicit")
                device_intervals.append(active)
                active = None
        if active:
            deadline = parse_utc(active["deadline"])
            if current >= deadline:
                _close(active, deadline, "deadline")
            device_intervals.append(active)
        gaps = capture_gaps(sorted(chunk_by_device.get(device_id, []), key=lambda row: (row["started"], row["id"])))
        for interval in device_intervals:
            started = parse_utc(interval["started_at"])
            existing_end = parse_utc(interval["ended_at"]) if interval["ended_at"] else None
            for gap_start, _gap_end in gaps:
                if gap_start < started:
                    continue
                if existing_end and gap_start >= existing_end:
                    continue
                _close(interval, gap_start, "capture_gap")
                existing_end = parse_utc(interval["ended_at"])
                break
        intervals.extend(device_intervals)
    return intervals


def overlap(start_a: datetime, end_a: datetime | None, start_b: datetime, end_b: datetime | None) -> bool:
    if end_a is None and end_b is None:
        return True
    if end_a is None:
        return start_a < (end_b or start_b)
    if end_b is None:
        return start_b < end_a
    return start_a < end_b and start_b < end_a


def clip_possible(possible: dict, manuals: list[dict]) -> list[dict]:
    start = parse_utc(possible["started_at"])
    end = parse_utc(possible["ended_at"]) if possible["ended_at"] else None
    segments = [(start, end)]
    for manual in manuals:
        if possible.get("device_id") and manual.get("device_id") and manual["device_id"] != possible["device_id"]:
            continue
        m_start = parse_utc(manual["started_at"])
        m_end = parse_utc(manual["ended_at"]) if manual["ended_at"] else None
        next_segments = []
        for seg_start, seg_end in segments:
            if not overlap(seg_start, seg_end, m_start, m_end):
                next_segments.append((seg_start, seg_end))
                continue
            if seg_start < m_start:
                next_segments.append((seg_start, min(m_start, seg_end or m_start)))
            if m_end is not None and (seg_end is None or m_end < seg_end):
                next_segments.append((m_end, seg_end))
        segments = [(a, b) for a, b in next_segments if b is None or b > a]
    clipped = []
    for index, (seg_start, seg_end) in enumerate(segments):
        item = dict(possible)
        item["id"] = possible["id"] if index == 0 else f"{possible['id']}:part{index}"
        item["started_at"] = format_utc(seg_start)
        item["ended_at"] = format_utc(seg_end) if seg_end else None
        if seg_end is not None:
            item["closed"] = 1
            if item.get("status") == "open":
                item["status"] = "closed"
        clipped.append(item)
    return clipped
