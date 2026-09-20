"""Deterministic Possible-event detector. No LLM, no speaker identity."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from meetings import (
    DISPLAY_ZONE,
    SESSION_GAP,
    capture_gaps,
    chunk_span,
    clip_possible,
    format_utc,
    parse_utc,
)

ALGORITHM_VERSION = "possible-event-v1"
WINDOW = timedelta(minutes=10)
MIN_COVERAGE = timedelta(minutes=8)
MIN_SPEECH_CHUNKS = 6
MIN_SPEECH_WORDS = 5
MIN_TOTAL_WORDS = 300
MIN_CATEGORIES = 2
SPARSE_WORDS = 30
MAX_EVENT = timedelta(hours=2)

CUE_CATEGORIES = {
    "meeting_agenda": (
        "meeting agenda",
        "this meeting",
        "today's agenda",
        "on the agenda",
        "kick off the meeting",
        "start the meeting",
        "weekly standup",
        "stand-up meeting",
        "all-hands meeting",
        "sync meeting",
        "agenda item",
        "called this meeting",
    ),
    "decision_planning": (
        "let's decide",
        "we should decide",
        "we decided",
        "we agreed",
        "let's plan",
        "we need to plan",
        "make a decision",
        "planning session",
        "agreed that",
        "decision is",
    ),
    "action_close": (
        "action item",
        "action items",
        "next steps",
        "next step is",
        "follow up with",
        "wrap up the meeting",
        "meeting adjourned",
        "send the notes",
        "takeaway is",
        "assign an owner",
    ),
}

_CUE_PATTERNS = {
    name: [re.compile(r"\b" + re.escape(phrase) + r"\b", re.IGNORECASE) for phrase in phrases]
    for name, phrases in CUE_CATEGORIES.items()
}


def cleaned_words(text: str) -> list[str]:
    return [part for part in (text or "").split() if part]


def chunk_categories(text: str) -> set[str]:
    found = set()
    body = text or ""
    for name, patterns in _CUE_PATTERNS.items():
        if any(pattern.search(body) for pattern in patterns):
            found.add(name)
    return found


def _row_words(row) -> int:
    if row["word_count"] is not None:
        return int(row["word_count"])
    return len(cleaned_words(row["transcript"] or ""))


def _is_speech(row) -> bool:
    return _row_words(row) >= MIN_SPEECH_WORDS


def _quiet_hours_end(started: datetime) -> datetime:
    local = started.astimezone(DISPLAY_ZONE)
    boundary = local.replace(hour=22, minute=0, second=0, microsecond=0)
    if local >= boundary:
        boundary = (local + timedelta(days=1)).replace(hour=22, minute=0, second=0, microsecond=0)
    return boundary.astimezone(timezone.utc)


def _session_groups(rows) -> list[list]:
    groups = []
    current = []
    previous_end = None
    for row in rows:
        start, end = chunk_span(row)
        if previous_end is not None and start - previous_end > SESSION_GAP:
            if current:
                groups.append(current)
            current = []
        current.append(row)
        previous_end = end if previous_end is None else max(previous_end, end)
    if current:
        groups.append(current)
    return groups


def _window(chunks: list, start_index: int) -> list:
    origin, _ = chunk_span(chunks[start_index])
    selected = []
    for row in chunks[start_index:]:
        start, _end = chunk_span(row)
        if start - origin > WINDOW:
            break
        selected.append(row)
    return selected


def _coverage(chunks: list) -> timedelta:
    total = 0.0
    for row in chunks:
        total += max(float(row["duration"] or 0), 0.0)
    return timedelta(seconds=total)


def _qualifies(window_rows: list) -> tuple[bool, list[str]]:
    reasons = []
    coverage = _coverage(window_rows)
    if coverage >= MIN_COVERAGE:
        reasons.append("coverage")
    speech = [row for row in window_rows if _is_speech(row)]
    if len(speech) >= MIN_SPEECH_CHUNKS:
        reasons.append("speech_chunks")
    words = sum(_row_words(row) for row in window_rows)
    if words >= MIN_TOTAL_WORDS:
        reasons.append("word_count")
    categories = set()
    category_chunks = set()
    for row in window_rows:
        found = chunk_categories(row["transcript"] or "")
        if found:
            categories.update(found)
            category_chunks.add(row["id"])
    if len(categories) >= MIN_CATEGORIES and len(category_chunks) >= 2:
        reasons.append("cue_categories")
    ok = (
        coverage >= MIN_COVERAGE
        and len(speech) >= MIN_SPEECH_CHUNKS
        and words >= MIN_TOTAL_WORDS
        and len(categories) >= MIN_CATEGORIES
        and len(category_chunks) >= 2
    )
    return ok, reasons


def _first_speech(rows: list):
    for row in rows:
        if _is_speech(row):
            return row
    return rows[0]


def _last_speech(rows: list):
    speech = [row for row in rows if _is_speech(row)]
    return speech[-1] if speech else rows[-1]


def _sparse_end(event_rows: list, rest: list):
    if not rest:
        return None
    for index, _row in enumerate(rest):
        window = _window(rest, index)
        if _coverage(window) >= WINDOW and sum(_row_words(item) for item in window) < SPARSE_WORDS:
            return _last_speech(event_rows)
    return None


def detect_possible_events(chunks, now: datetime | None = None) -> list[dict]:
    current = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    by_device: dict[str, list] = {}
    for row in chunks:
        by_device.setdefault(row["device"], []).append(row)
    found = []
    for device_id, rows in by_device.items():
        ordered = sorted(rows, key=lambda item: (item["started"], item["id"]))
        for session in _session_groups(ordered):
            confirmed = False
            start_index = None
            reasons = []
            for index in range(len(session)):
                window_rows = _window(session, index)
                ok, window_reasons = _qualifies(window_rows)
                if ok:
                    confirmed = True
                    start_index = index
                    reasons = window_reasons
                    break
            if start_index is None:
                # Keep a tentative candidate only when some meeting-like cues exist
                # but coverage/data is still incomplete.
                cue_rows = [row for row in session if chunk_categories(row["transcript"] or "")]
                if not cue_rows:
                    continue
                start_row = _first_speech(session)
                end_row = session[-1]
                start, _ = chunk_span(start_row)
                _end_start, end = chunk_span(end_row)
                hard = min(start + MAX_EVENT, _quiet_hours_end(start))
                status = "tentative"
                closed = 0
                ended_at = None
                closure = None
                if current >= hard:
                    status = "closed"
                    closed = 1
                    ended_at = format_utc(min(end, hard))
                    closure = "deadline" if hard == start + MAX_EVENT else "quiet_hours"
                found.append({
                    "id": f"possible:{device_id}:{start_row['id']}",
                    "device_id": device_id,
                    "source": "possible",
                    "meeting_id": None,
                    "label": "Possible event",
                    "started_at": format_utc(start),
                    "ended_at": ended_at,
                    "closed": closed,
                    "closure_reason": closure,
                    "status": status,
                    "deadline": format_utc(hard),
                    "algorithm_version": ALGORITHM_VERSION,
                    "reasons": ["unconfirmed"],
                    "chunk_ids": [row["id"] for row in session],
                })
                continue
            start_row = _first_speech(_window(session, start_index))
            event_rows = []
            start, _ = chunk_span(start_row)
            hard = min(start + MAX_EVENT, _quiet_hours_end(start))
            collecting = False
            for row in session:
                row_start, row_end = chunk_span(row)
                if row["id"] == start_row["id"]:
                    collecting = True
                if not collecting:
                    continue
                if row_start >= hard:
                    break
                event_rows.append(row)
                if row_end >= hard:
                    break
            rest = [row for row in session if chunk_span(row)[0] > chunk_span(event_rows[-1])[0]]
            sparse = _sparse_end(event_rows, rest)
            end_row = sparse or _last_speech(event_rows)
            _end_start, end = chunk_span(end_row)
            gaps = capture_gaps(session)
            closure = None
            closed = 1
            status = "closed"
            ended = end
            for gap_start, _gap_end in gaps:
                if start < gap_start <= end:
                    ended = min(ended, gap_start)
                    closure = "capture_gap"
                    break
            if ended >= hard:
                ended = hard
                closure = "deadline" if hard == start + MAX_EVENT else "quiet_hours"
            elif sparse is not None and closure is None:
                closure = "sparse_speech"
            elif closure is None:
                if current < hard and chunk_span(session[-1])[1] < hard and not gaps:
                    # Still receiving data; keep open unless a hard boundary already applied.
                    last_end = chunk_span(event_rows[-1])[1]
                    if last_end < current and (current - last_end) <= SESSION_GAP:
                        closed = 0
                        status = "open"
                        ended = None
                    else:
                        closure = "session_end"
                else:
                    closure = "session_end"
            found.append({
                "id": f"possible:{device_id}:{start_row['id']}",
                "device_id": device_id,
                "source": "possible",
                "meeting_id": None,
                "label": "Possible event",
                "started_at": format_utc(start),
                "ended_at": format_utc(ended) if ended is not None else None,
                "closed": closed,
                "closure_reason": closure,
                "status": status,
                "deadline": format_utc(hard),
                "algorithm_version": ALGORITHM_VERSION,
                "reasons": reasons,
                "chunk_ids": [row["id"] for row in event_rows],
            })
    return found


def apply_manual_precedence(possible: list[dict], manuals: list[dict]) -> list[dict]:
    clipped = []
    for item in possible:
        clipped.extend(clip_possible(item, manuals))
    return clipped
