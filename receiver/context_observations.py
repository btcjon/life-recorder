"""Immutable context observations and a deterministic interval reducer.

Observations are evidence of overlapping context. They are not a recording
source and never change audio, transcripts, voice samples, or event ids.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import hashlib
import json
import re
import uuid
from zoneinfo import ZoneInfo

MAX_BODY = 4096
ACTIVE_BOUND_SECONDS = 30
OBSERVER_FRESH_SECONDS = 30
SKEW_LIMIT_SECONDS = 300
SCAN_LIMIT = 2000
CAPTURE_SCAN_LIMIT = 500
PRESENT_LIMIT = 200
DISPLAY_ZONE = ZoneInfo("America/New_York")
STATEMENT = "Overlapping context. This is not a recording source."
BROWSER_ADAPTER = {
    "available": False,
    "reason": "not_configured",
    "allowlisted_hosts": ["youtube.com", "x.com"],
    "note": (
        "YouTube and X playback metadata has not been configured or verified. "
        "Process output-IO evidence does not use browser metadata."
    ),
}
SIGNALS = {"mac_output", "browser_media", "phone_audio", "phone_call", "capture"}
STATES = {"active", "inactive", "unknown", "paused", "ended"}
APP_SIGNALS = {"mac_output", "browser_media"}
METADATA_KEYS = {"app", "title", "host", "reason", "output_route", "input_route", "call_state"}
CALL_STATES = {"ringing", "incoming", "outgoing", "connected", "held", "ended", "missed", "unknown"}
OBSERVATION_KEYS = {
    "version", "id", "producer_id", "session_id", "sequence", "observed_at",
    "signal", "state", "provenance", "metadata",
}
ISO_RE = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$")
URL_RE = re.compile(r"(?i)(?:https?://|www\.|[a-z][a-z0-9+.-]*://)")
PHONE_RE = re.compile(r"(?:\+\d[\d\s().-]{7,}\d|\b\d{3}[-.\s)]\d{3}[-.\s]\d{4}\b|\d{10,})")
HOST_RE = re.compile(r"^(?=.{1,200}$)(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?)(?:\.(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?))*$")


class Conflict(ValueError):
    pass


def ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS context_observations (
            id TEXT PRIMARY KEY,
            device_id TEXT NOT NULL,
            producer_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            sequence INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            signal TEXT NOT NULL,
            state TEXT NOT NULL,
            provenance TEXT NOT NULL,
            app TEXT NOT NULL DEFAULT '',
            metadata_json TEXT NOT NULL,
            logical_hash TEXT NOT NULL,
            received_at REAL NOT NULL,
            clock_skew_seconds REAL NOT NULL,
            precise_association INTEGER NOT NULL
        )"""
    )
    db.execute(
        """CREATE INDEX IF NOT EXISTS context_observations_order
           ON context_observations(signal, observed_at, sequence, id)"""
    )


def valid_uuid(value) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid UUID")
    parsed = uuid.UUID(value)
    if str(parsed) != value.lower():
        raise ValueError("Invalid UUID")
    return str(parsed)


def format_utc(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


def parse_observed_at(value) -> datetime:
    if not isinstance(value, str) or not ISO_RE.fullmatch(value):
        raise ValueError("Invalid observed_at")
    text = value[:-1] + "+00:00" if value.endswith("Z") else value
    stamp = datetime.fromisoformat(text)
    if stamp.tzinfo is None:
        raise ValueError("observed_at must have a timezone")
    return stamp.astimezone(timezone.utc)


def _text(value, limit: int, *, host: bool = False, call_state: bool = False) -> str:
    if not isinstance(value, str) or not value or value != value.strip() or len(value) > limit:
        raise ValueError("Invalid text")
    if any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
        raise ValueError("Invalid text")
    if URL_RE.search(value) or "@" in value or PHONE_RE.search(value):
        raise ValueError("Raw URL or contact handle")
    if host and not HOST_RE.fullmatch(value):
        raise ValueError("Invalid host")
    if call_state and value not in CALL_STATES:
        raise ValueError("Invalid call state")
    return value


def _metadata(value) -> dict:
    if not isinstance(value, dict) or len(value) > 7 or set(value) - METADATA_KEYS:
        raise ValueError("Invalid metadata")
    clean = {}
    for key in sorted(value):
        if key == "host":
            clean[key] = _text(value[key], 200, host=True)
        elif key == "call_state":
            clean[key] = _text(value[key], 200, call_state=True)
        else:
            clean[key] = _text(value[key], 200)
    return clean


def validate(device_id: str, body: dict, now: float) -> dict:
    header = valid_uuid(device_id)
    if not isinstance(body, dict) or set(body) != {"device_id", "observation"}:
        raise ValueError("Invalid envelope")
    if valid_uuid(body["device_id"]) != header:
        raise ValueError("Device mismatch")
    observation = body["observation"]
    if not isinstance(observation, dict) or set(observation) != OBSERVATION_KEYS:
        raise ValueError("Invalid observation")
    if type(observation["version"]) is not int or observation["version"] != 1:
        raise ValueError("Invalid version")
    identifier = valid_uuid(observation["id"])
    producer = valid_uuid(observation["producer_id"])
    session = valid_uuid(observation["session_id"])
    if producer != header:
        raise ValueError("Producer mismatch")
    sequence = observation["sequence"]
    if isinstance(sequence, bool) or not isinstance(sequence, int) or not 0 <= sequence <= 2**63 - 1:
        raise ValueError("Invalid sequence")
    observed = parse_observed_at(observation["observed_at"])
    signal = observation["signal"]
    state = observation["state"]
    if signal not in SIGNALS or state not in STATES:
        raise ValueError("Invalid signal or state")
    provenance = _text(observation["provenance"], 80)
    metadata = _metadata(observation["metadata"])
    skew = float(now) - observed.timestamp()
    normalized = {
        "version": 1,
        "id": identifier,
        "device_id": header,
        "producer_id": producer,
        "session_id": session,
        "sequence": sequence,
        "observed_at": format_utc(observed),
        "signal": signal,
        "state": state,
        "provenance": provenance,
        "metadata": metadata,
        "app": metadata.get("app", ""),
        "received_at": float(now),
        "clock_skew_seconds": skew,
        "precise_association": 0 <= skew <= SKEW_LIMIT_SECONDS,
    }
    normalized["logical_hash"] = hashlib.sha256(
        json.dumps(
            {key: normalized[key] for key in (
                "version", "id", "producer_id", "session_id", "sequence", "observed_at",
                "signal", "state", "provenance", "metadata")},
            sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        ).encode()
    ).hexdigest()
    return normalized


def ingest(db, device_id: str, body: dict, now: float) -> dict:
    """Store one observation. An identical logical payload returns the same receipt."""
    item = validate(device_id, body, now)
    existing = db.execute(
        "SELECT logical_hash FROM context_observations WHERE id=?", (item["id"],)
    ).fetchone()
    if existing:
        if existing["logical_hash"] != item["logical_hash"]:
            raise Conflict("Context observation conflict")
        return {"durable": True, "observation_id": item["id"], "duplicate": True}
    db.execute(
        """INSERT INTO context_observations (
            id, device_id, producer_id, session_id, sequence, observed_at, signal, state,
            provenance, app, metadata_json, logical_hash, received_at, clock_skew_seconds,
            precise_association
        ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (item["id"], item["device_id"], item["producer_id"], item["session_id"], item["sequence"],
         item["observed_at"], item["signal"], item["state"], item["provenance"], item["app"],
         json.dumps(item["metadata"], sort_keys=True, separators=(",", ":"), ensure_ascii=False),
         item["logical_hash"], item["received_at"], item["clock_skew_seconds"],
         int(item["precise_association"])),
    )
    return {"durable": True, "observation_id": item["id"], "duplicate": False}


def _row(row) -> dict:
    metadata = json.loads(row["metadata_json"])
    return {
        "id": row["id"],
        "device_id": row["device_id"],
        "producer_id": row["producer_id"],
        "session_id": row["session_id"],
        "sequence": int(row["sequence"]),
        "observed_at": row["observed_at"],
        "signal": row["signal"],
        "state": row["state"],
        "provenance": row["provenance"],
        "app": row["app"] or "",
        "metadata": metadata,
        "reason": metadata.get("reason"),
        "received_at": float(row["received_at"]),
        "clock_skew_seconds": float(row["clock_skew_seconds"]),
        "precise_association": bool(row["precise_association"]),
    }


def _sort_key(row: dict):
    return (row["observed_at"], row["sequence"], row["id"])


def _load(db, signal_sql: str, limit: int, params=()) -> tuple[list[dict], bool]:
    rows = db.execute(
        f"""SELECT * FROM context_observations WHERE {signal_sql}
            ORDER BY observed_at DESC, sequence DESC, id DESC LIMIT ?""",
        (*params, limit + 1),
    ).fetchall()
    truncated = len(rows) > limit
    return [_row(row) for row in rows[:limit]], truncated


def _group_key(row: dict):
    app = row["app"] if row["signal"] in APP_SIGNALS else ""
    return (row["producer_id"], row["session_id"], row["signal"], app)


def _derived_id(*parts) -> str:
    raw = "\n".join(parts).encode()
    return hashlib.sha256(raw).hexdigest()[:16]


def _uncertainties(precise: bool, boundary_exact: bool, extra=()) -> list[str]:
    values = []
    if not precise:
        values.append("clock_skew")
    if not boundary_exact:
        values.append("unknown_termination_boundary")
    for item in extra:
        if item not in values:
            values.append(item)
    return values


def reduce_intervals(rows: list[dict], now: float) -> list[dict]:
    grouped: dict[tuple, list[dict]] = {}
    for row in sorted(rows, key=_sort_key):
        grouped.setdefault(_group_key(row), []).append(row)
    intervals = []
    for key, items in grouped.items():
        current = None
        for obs in items:
            expired = (current is not None and current["state"] == "active"
                       and parse_observed_at(obs["observed_at"]).timestamp()
                       - parse_observed_at(current["last_observed_at"]).timestamp() > ACTIVE_BOUND_SECONDS)
            if current is None or obs["state"] != current["state"] or expired:
                if current is not None:
                    _close_interval(current, obs)
                    _finish_interval(current, now)
                    intervals.append(current)
                current = _open_interval(key, obs)
            else:
                current["last_observed_at"] = obs["observed_at"]
                current["last_sequence"] = obs["sequence"]
                current["last_id"] = obs["id"]
                current["last_received_at"] = obs["received_at"]
                current["observation_count"] += 1
                current["precise_transcript_association"] = (
                    current["precise_transcript_association"] and obs["precise_association"]
                )
                current["clock_skew_seconds"] = max(
                    current["clock_skew_seconds"], abs(obs["clock_skew_seconds"])
                )
                current["metadata"] = obs["metadata"]
                current["reason"] = obs["reason"]
        if current is not None:
            _finish_interval(current, now)
            intervals.append(current)
    intervals.sort(key=lambda item: (item["started_at"], item["sequence"], item["id"]))
    return intervals


def _open_interval(key, obs: dict) -> dict:
    producer, session, signal, app = key
    return {
        "id": _derived_id(producer, session, signal, app, obs["id"]),
        "producer_id": producer,
        "session_id": session,
        "signal": signal,
        "app": app or None,
        "state": obs["state"],
        "started_at": obs["observed_at"],
        "ended_at": None,
        "effective_until": None,
        "sequence": obs["sequence"],
        "first_observation_id": obs["id"],
        "last_id": obs["id"],
        "last_observed_at": obs["observed_at"],
        "last_sequence": obs["sequence"],
        "last_received_at": obs["received_at"],
        "observation_count": 1,
        "closed": False,
        "boundary_exact": _exact_capture_boundary(obs),
        "fresh": False,
        "stale": False,
        "precise_transcript_association": obs["precise_association"],
        "clock_skew_seconds": abs(obs["clock_skew_seconds"]),
        "provenance": obs["provenance"],
        "reason": obs["reason"],
        "metadata": obs["metadata"],
        "relationship": "overlapping_context",
        "reason_assigns_cause": False,
    }


def _close_interval(current: dict, nxt: dict) -> None:
    current["closed"] = True
    current["ended_at"] = nxt["observed_at"]
    current["fresh"] = False
    current["stale"] = False
    current["precise_transcript_association"] &= nxt["precise_association"]
    exact = current["boundary_exact"] and _exact_capture_boundary(nxt) and current["precise_transcript_association"]
    if current["state"] == "active":
        bound = parse_observed_at(current["last_observed_at"]) + timedelta(seconds=ACTIVE_BOUND_SECONDS)
        if parse_observed_at(nxt["observed_at"]) > bound:
            current["ended_at"] = format_utc(bound)
            exact = False
    current["boundary_exact"] = exact
    current["effective_until"] = current["ended_at"]


def _finish_interval(current: dict, now: float) -> None:
    if current["closed"]:
        current["uncertain"] = not current["boundary_exact"] or not current["precise_transcript_association"]
        current["uncertainties"] = _uncertainties(
            current["precise_transcript_association"], current["boundary_exact"]
        )
        return
    current["ended_at"] = None
    if current["state"] == "unknown":
        current["boundary_exact"] = False
    if current["state"] == "active":
        last = parse_observed_at(current["last_observed_at"])
        effective = last + timedelta(seconds=ACTIVE_BOUND_SECONDS)
        current["effective_until"] = format_utc(effective)
        if current["precise_transcript_association"]:
            current["fresh"] = last.timestamp() <= now <= effective.timestamp()
        else:
            current["fresh"] = False
        current["stale"] = not current["fresh"]
        current["boundary_exact"] = False
    else:
        current["fresh"] = False
        current["stale"] = False
        current["effective_until"] = None
    current["uncertain"] = (
        not current["boundary_exact"] or not current["precise_transcript_association"] or current["stale"]
    )
    current["uncertainties"] = _uncertainties(
        current["precise_transcript_association"], current["boundary_exact"],
        ("stale_observer",) if current["stale"] else (),
    )


def _gap_flags(gap: dict) -> None:
    gap["uncertain"] = bool(gap["uncertainties"]) or not gap["boundary_exact"]
    gap["uncertainty"] = gap["uncertainties"][-1] if gap["uncertainties"] else None


def _exact_capture_boundary(obs: dict) -> bool:
    return (obs['signal'] == 'capture' and obs['state'] in ('active', 'inactive')
            and obs['provenance'] == 'ios-phone-capture' and obs['precise_association'])


def capture_gaps(rows: list[dict], fresh_until: dict[str, float], now: float) -> list[dict]:
    by_producer: dict[str, list[dict]] = {}
    for row in rows:
        if row["signal"] == "capture":
            by_producer.setdefault(row["producer_id"], []).append(row)
    gaps = []
    for producer, items in by_producer.items():
        gap = None
        for obs in sorted(items, key=_sort_key):
            if obs["state"] == "inactive" or (obs["state"] == "unknown" and obs["reason"] in ("unknown_termination", "startup_failure")):
                if gap is None or not gap["open"]:
                    gap = {
                        "id": _derived_id("gap", producer, obs["id"]),
                        "producer_id": producer,
                        "session_id": obs["session_id"],
                        "started_at": obs["observed_at"],
                        "start_observation_id": obs["id"],
                        "ended_at": None,
                        "open": True,
                        "boundary_exact": _exact_capture_boundary(obs),
                        "uncertainties": _uncertainties(obs["precise_association"], _exact_capture_boundary(obs)),
                        "reason": obs["reason"],
                        "reason_assigns_cause": False,
                        "relationship": "overlapping_context",
                    }
                    _gap_flags(gap)
            elif obs["state"] == "active" and gap is not None and gap["open"]:
                gap["open"] = False
                gap["ended_at"] = obs["observed_at"]
                gap["closed_by_session_id"] = obs["session_id"]
                gap["end_exact"] = bool(obs["precise_association"])
                if not obs["precise_association"] and "clock_skew" not in gap["uncertainties"]:
                    gap["uncertainties"].append("clock_skew")
                _gap_flags(gap)
                gaps.append(gap)
                gap = None
            elif obs["state"] == "unknown" and gap is not None and gap["open"]:
                gap["boundary_exact"] = False
                if "unknown_termination_boundary" not in gap["uncertainties"]:
                    gap["uncertainties"].append("unknown_termination_boundary")
                _gap_flags(gap)
        if gap is not None and gap["open"]:
            latest = fresh_until.get(producer)
            if latest is None or now - latest > OBSERVER_FRESH_SECONDS:
                gap["uncertain"] = True
                if "no_fresh_observer" not in gap["uncertainties"]:
                    gap["uncertainties"].append("no_fresh_observer")
                gap["uncertainty"] = "no_fresh_observer"
            else:
                _gap_flags(gap)
            gaps.append(gap)
    gaps.sort(key=lambda item: (item["started_at"], item["id"]))
    return gaps


def _overlaps(start: str, end: str | None, day_start: datetime, day_end: datetime) -> bool:
    started = parse_observed_at(start)
    if started >= day_end:
        return False
    if end is not None and parse_observed_at(end) <= day_start:
        return False
    return True


def _public_interval(item: dict) -> dict:
    metadata = item["metadata"]
    public = {
        "id": item["id"],
        "producer_id": item["producer_id"],
        "session_id": item["session_id"],
        "signal": item["signal"],
        "app": item["app"],
        "state": item["state"],
        "started_at": item["started_at"],
        "ended_at": item["ended_at"],
        "effective_until": item["effective_until"],
        "fresh": item["fresh"],
        "stale": item["stale"],
        "closed": item["closed"],
        "boundary_exact": item["boundary_exact"],
        "uncertain": item["uncertain"],
        "uncertainties": item["uncertainties"],
        "precise_transcript_association": item["precise_transcript_association"],
        "clock_skew_seconds": round(item["clock_skew_seconds"], 3),
        "provenance": item["provenance"],
        "reason": item["reason"],
        "reason_assigns_cause": False,
        "relationship": "overlapping_context",
        "observation_count": item["observation_count"],
        "open_recording_gap": False,
    }
    for key in ("title", "host", "output_route", "input_route", "call_state"):
        if key in metadata:
            public[key] = metadata[key]
    return public


def present(db, day: str | None = None, now: float | None = None,
            observer_status: dict | None = None, scan_limit: int = SCAN_LIMIT,
            capture_limit: int = CAPTURE_SCAN_LIMIT, present_limit: int = PRESENT_LIMIT) -> dict:
    now = float(now if now is not None else datetime.now(timezone.utc).timestamp())
    day_start = day_end = None
    if day is not None:
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", day):
            raise ValueError("Invalid day")
        day_start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=DISPLAY_ZONE)
        day_end = day_start + timedelta(days=1)
        others, other_truncated = _load(db,
            "signal!='capture' AND observed_at>=? AND observed_at<?", scan_limit,
            (format_utc(day_start - timedelta(seconds=ACTIVE_BOUND_SECONDS)), format_utc(day_end)))
    else:
        others, other_truncated = _load(db, "signal!='capture'", scan_limit)
    captures, capture_truncated = _load(db, "signal='capture'", capture_limit)
    if day_end is not None:
        older, older_truncated = _load(db, "signal='capture' AND observed_at<?", capture_limit,
                                      (format_utc(day_end),))
        captures = list({row['id']: row for row in captures + older}.values())
        capture_truncated |= older_truncated
    rows = others + captures
    freshness_rows = db.execute(
        "SELECT producer_id, max(received_at) AS received_at FROM context_observations GROUP BY producer_id LIMIT 100"
    ).fetchall()
    fresh_until = {row["producer_id"]: float(row["received_at"]) for row in freshness_rows}
    intervals = [_public_interval(item) for item in reduce_intervals(rows, now)]
    gaps = capture_gaps(rows, fresh_until, now)
    if day_start is not None:
        intervals = [item for item in intervals if _overlaps(item["started_at"], item["ended_at"] or item["effective_until"], day_start, day_end)]
        gaps = [item for item in gaps if _overlaps(item["started_at"], item["ended_at"], day_start, day_end)]
    for gap in gaps:
        until = now if gap['open'] else parse_observed_at(gap['ended_at']).timestamp()
        gap['duration_seconds'] = max(0.0, until - parse_observed_at(gap['started_at']).timestamp())
        gap['duration_is_final'] = not gap['open']
    truncated = other_truncated or capture_truncated or len(intervals) > present_limit or len(gaps) > present_limit
    intervals = intervals[-present_limit:]
    gaps = gaps[-present_limit:]
    open_starts = {(gap["producer_id"], gap["started_at"]) for gap in gaps if gap["open"]}
    for item in intervals:
        if item["signal"] == "capture" and item["state"] == "inactive":
            item["open_recording_gap"] = (item["producer_id"], item["started_at"]) in open_starts
    latest = max(fresh_until.values(), default=None)
    age = None if latest is None else max(0.0, now - latest)
    helper = (observer_status or {}).get("helper")
    if helper == "unavailable":
        label = "helper unavailable"
    elif latest is None:
        label = "no observations"
    elif age is not None and age <= OBSERVER_FRESH_SECONDS:
        label = "fresh"
    else:
        label = "stale"
    return {
        "version": 1,
        "statement": STATEMENT,
        "relationship": "overlapping_context",
        "browser_adapter": BROWSER_ADAPTER,
        "freshness": {
            "label": label,
            "age_seconds": None if age is None else round(age, 3),
            "latest_received_at": latest,
            "detail": "Freshness uses the receiver receipt clock and a 30 second bound.",
        },
        "observer": observer_status or {"running": False, "helper": "not_started", "browser_adapter": BROWSER_ADAPTER},
        "intervals": intervals,
        "open_capture_gaps": [gap for gap in gaps if gap["open"]],
        "capture_gaps": gaps,
        "truncated": truncated,
        "bounds": {"active_seconds": ACTIVE_BOUND_SECONDS, "skew_seconds": SKEW_LIMIT_SECONDS},
    }
