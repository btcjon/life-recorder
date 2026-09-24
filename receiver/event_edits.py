"""Revisioned human titles and clip boundaries for sidebar events.

Automatic grouping stays a suggestion. A saved edit is keyed by a stable id
and by the start and end clip anchors. Recomputing groups does not rewrite it.
"""
from __future__ import annotations

import re
import time
import uuid
from datetime import timedelta

MAX_TITLE = 80
_ID = re.compile(r"edt_[0-9a-f]{32}")
_CHUNK = re.compile(r"[A-Za-z0-9_-]{1,80}")
_FIELDS = {"id", "title", "start_chunk_id", "end_chunk_id", "expected_revision"}


class EditError(Exception):
    def __init__(self, status: int, error: str, **extra):
        self.status = status
        self.payload = {"error": error, **extra}
        super().__init__(error)


def ensure_schema(db) -> None:
    db.execute(
        """CREATE TABLE IF NOT EXISTS event_edits (
            id TEXT PRIMARY KEY,
            revision INTEGER NOT NULL,
            title TEXT NOT NULL,
            start_chunk_id TEXT NOT NULL,
            end_chunk_id TEXT NOT NULL,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL
        )"""
    )
    db.execute(
        "CREATE INDEX IF NOT EXISTS event_edits_anchors ON event_edits(start_chunk_id, end_chunk_id)"
    )


def load(db) -> list[dict]:
    rows = db.execute(
        """SELECT id, revision, title, start_chunk_id, end_chunk_id, created_at
           FROM event_edits ORDER BY created_at, id"""
    ).fetchall()
    return [
        {
            "id": row["id"],
            "revision": int(row["revision"]),
            "title": row["title"],
            "start_chunk_id": row["start_chunk_id"],
            "end_chunk_id": row["end_chunk_id"],
        }
        for row in rows
    ]


def _viewer():
    import viewer as viewer_mod
    return viewer_mod


def _chunks(rows) -> list[dict]:
    found = []
    for row in rows:
        transcript = row["transcript"] if row["transcript"] is not None else ""
        found.append({
            "id": row["id"],
            "started": row["started"],
            "duration": row["duration"],
            "transcript": transcript,
        })
    return found


def _ordered(chunks: list[dict]) -> list[dict]:
    viewer = _viewer()
    ordered = sorted(
        enumerate(chunks),
        key=lambda item: (
            (0, viewer._chunk_start(item[1]).timestamp(), str(item[1].get("id") or ""))
            if viewer._chunk_start(item[1]) is not None
            else (1, item[0], str(item[1].get("id") or ""))
        ),
    )
    return [chunk for _, chunk in ordered]


def _locate(chunks: list[dict], start_id: str, end_id: str):
    """Return the inclusive anchor span, or an EditError for the caller to raise."""
    viewer = _viewer()
    known = {chunk.get("id") for chunk in chunks}
    if start_id not in known or end_id not in known:
        return None, EditError(400, "Unknown recording")
    ordered = [chunk for chunk in _ordered(chunks) if viewer._chunk_start(chunk) is not None]
    positions = {chunk["id"]: index for index, chunk in enumerate(ordered)}
    if start_id not in positions or end_id not in positions:
        return None, EditError(400, "Recording has no timestamp")
    start_i = positions[start_id]
    end_i = positions[end_id]
    if start_i > end_i:
        return None, EditError(400, "Boundaries are out of order")
    span = ordered[start_i:end_i + 1]
    days = {viewer._local_date(viewer._chunk_start(chunk)) for chunk in span}
    if len(days) != 1:
        return None, EditError(400, "Events must stay on one day")
    if len(span) < 2:
        return None, EditError(400, "An event needs two recordings")
    return span, None


def _title(value) -> str:
    if not isinstance(value, str):
        raise EditError(400, "Title is required")
    title = " ".join(value.split())
    if not title:
        raise EditError(400, "Title is required")
    if len(title) > MAX_TITLE:
        raise EditError(400, "Title is too long")
    return title


def _chunk_id(value) -> str:
    if not isinstance(value, str) or not _CHUNK.fullmatch(value):
        raise EditError(400, "Unknown recording")
    return value


def _revision(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise EditError(400, "Revision is required")
    if value < 0 or value > 1_000_000:
        raise EditError(409, "Revision conflict")
    return value


def _public(row) -> dict:
    return {
        "id": row["id"],
        "revision": int(row["revision"]),
        "title": row["title"],
        "start_chunk_id": row["start_chunk_id"],
        "end_chunk_id": row["end_chunk_id"],
    }


def _suggestion(span: list[dict]) -> dict:
    viewer = _viewer()
    parts = []
    for block in viewer.display_blocks(span):
        if block.get("kind") == "event":
            parts.append({
                "kind": "event",
                "started_local": block.get("started_local") or "",
                "ended_local": block.get("ended_local") or "",
                "clip_count": block.get("clip_count") or 0,
            })
            continue
        chunk = next((item for item in span if item.get("id") == block.get("chunk_id")), None)
        stamp = viewer._chunk_start(chunk) if chunk else None
        parts.append({
            "kind": "recording",
            "started_local": viewer._local_text(stamp) if stamp else "",
        })
    return {"parts": parts}


def _manual_block(edit: dict, span: list[dict]) -> dict:
    viewer = _viewer()
    start = viewer._chunk_start(span[0])
    last = span[-1]
    end = viewer._chunk_start(last) + timedelta(seconds=viewer._duration(last))
    preview = next(
        (str(item.get("transcript") or "").strip() for item in span if str(item.get("transcript") or "").strip()),
        "",
    )
    return {
        "kind": "event",
        "id": edit["id"],
        "chunk_ids": [item.get("id") for item in span],
        "started_local": viewer._local_text(start),
        "ended_local": viewer._local_text(end),
        "clip_count": len(span),
        "duration": sum(viewer._duration(item) for item in span),
        "preview": preview,
        "source": "manual",
        "title": edit["title"],
        "revision": int(edit["revision"]),
        "start_chunk_id": edit["start_chunk_id"],
        "end_chunk_id": edit["end_chunk_id"],
        "suggestion": _suggestion(span),
    }


def _annotate(block: dict) -> dict:
    item = dict(block)
    if item.get("kind") != "event":
        return item
    ids = list(item.get("chunk_ids") or [])
    item["source"] = "suggestion"
    item["title"] = ""
    item["revision"] = None
    item["start_chunk_id"] = ids[0] if ids else ""
    item["end_chunk_id"] = ids[-1] if ids else ""
    return item


def overlay(chunks: list[dict], edits: list[dict]) -> list[dict]:
    """Apply saved boundaries over automatic groups. Each clip appears once."""
    viewer = _viewer()
    ordered = _ordered(chunks)
    claimed: set[str] = set()
    manuals: dict[str, dict] = {}
    for edit in edits:
        span, error = _locate(chunks, edit["start_chunk_id"], edit["end_chunk_id"])
        if error is not None or span is None:
            continue
        ids = [item["id"] for item in span]
        if claimed.intersection(ids):
            continue
        claimed.update(ids)
        manuals[ids[0]] = _manual_block(edit, span)
    if not manuals:
        return [_annotate(block) for block in viewer.display_blocks(ordered)]
    combined: list[dict] = []
    uncovered: list[dict] = []

    def flush() -> None:
        combined.extend(_annotate(block) for block in viewer.display_blocks(uncovered))
        uncovered.clear()

    for chunk in ordered:
        chunk_id = chunk.get("id")
        if chunk_id in manuals:
            flush()
            combined.append(manuals[chunk_id])
        if chunk_id not in claimed:
            uncovered.append(chunk)
    flush()
    return combined


def confirmed_speakers(blocks: list[dict]) -> list[dict]:
    """Sidebar names are confirmed assignments. Transcript text is not a source."""
    shown = []
    for block in blocks:
        item = dict(block)
        if item.get("kind") == "event":
            people = []
            for person in item.get("speakers") or []:
                if not isinstance(person, dict) or person.get("confirmed") is not True:
                    continue
                name = str(person.get("name") or "").strip()
                if not name:
                    continue
                people.append({"id": person.get("id"), "name": name, "confirmed": True})
            item["speakers"] = people
        shown.append(item)
    return shown


def save(db, body: dict, rows) -> dict:
    if not isinstance(body, dict):
        raise EditError(400, "JSON object required")
    if set(body) - _FIELDS:
        raise EditError(400, "Unexpected field")
    ensure_schema(db)
    title = _title(body.get("title"))
    start_id = _chunk_id(body.get("start_chunk_id"))
    end_id = _chunk_id(body.get("end_chunk_id"))
    expected = _revision(body.get("expected_revision"))
    chunks = _chunks(rows)
    span, error = _locate(chunks, start_id, end_id)
    if error is not None:
        raise error
    span_ids = {item["id"] for item in span}
    existing_id = body.get("id")
    if existing_id is not None:
        if not isinstance(existing_id, str) or not _ID.fullmatch(existing_id):
            raise EditError(404, "Unknown event")
        current = db.execute(
            "SELECT id, revision, title, start_chunk_id, end_chunk_id FROM event_edits WHERE id=?",
            (existing_id,),
        ).fetchone()
        if current is None:
            raise EditError(404, "Unknown event")
        if int(current["revision"]) != expected:
            raise EditError(409, "Revision conflict", revision=int(current["revision"]))
    elif expected != 0:
        raise EditError(409, "Revision conflict")
    for other in load(db):
        if existing_id is not None and other["id"] == existing_id:
            continue
        other_span, other_error = _locate(chunks, other["start_chunk_id"], other["end_chunk_id"])
        if other_error is not None or other_span is None:
            continue
        if span_ids.intersection(item["id"] for item in other_span):
            raise EditError(400, "Boundaries overlap another edit")
    now = time.time()
    if existing_id is None:
        edit_id = "edt_" + uuid.uuid4().hex
        db.execute(
            """INSERT INTO event_edits
               (id, revision, title, start_chunk_id, end_chunk_id, created_at, updated_at)
               VALUES (?, 1, ?, ?, ?, ?, ?)""",
            (edit_id, title, start_id, end_id, now, now),
        )
    else:
        updated = db.execute(
            """UPDATE event_edits
               SET revision=revision+1, title=?, start_chunk_id=?, end_chunk_id=?, updated_at=?
               WHERE id=? AND revision=?""",
            (title, start_id, end_id, now, existing_id, expected),
        )
        if updated.rowcount != 1:
            current = db.execute(
                "SELECT revision FROM event_edits WHERE id=?",
                (existing_id,),
            ).fetchone()
            revision = int(current["revision"]) if current else expected
            raise EditError(409, "Revision conflict", revision=revision)
        edit_id = existing_id
    saved = db.execute(
        "SELECT id, revision, title, start_chunk_id, end_chunk_id FROM event_edits WHERE id=?",
        (edit_id,),
    ).fetchone()
    return _public(saved)
