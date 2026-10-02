"""Persisted timeline reads and explicit revision-checked human topic decisions.

Call ensure_schema during receiver reconciliation, never during timeline reads.
Mutations use savepoints so a rejected merge/split cannot partially remove edits.
"""
from contextlib import contextmanager
from datetime import datetime, timedelta
import hashlib
import json
import time
import uuid

import event_edits
import meetings


def ensure_schema(db):
    db.execute("""CREATE TABLE IF NOT EXISTS topic_suggestions (
        id TEXT PRIMARY KEY, revision INTEGER NOT NULL, fingerprint TEXT NOT NULL,
        source_fingerprint TEXT NOT NULL, source_ids TEXT NOT NULL,
        title TEXT NOT NULL, start_chunk_id TEXT NOT NULL, end_chunk_id TEXT NOT NULL,
        state TEXT NOT NULL, model TEXT NOT NULL, prompt_version TEXT NOT NULL,
        created_at REAL NOT NULL, updated_at REAL NOT NULL, accepted_edit_id TEXT)""")
    db.execute("""CREATE TABLE IF NOT EXISTS timeline_lineage (
        edit_id TEXT PRIMARY KEY, source_ids TEXT NOT NULL, human_override INTEGER NOT NULL DEFAULT 1)""")


def source_fingerprint(rows):
    values = [[r["id"], r["started"], r["duration"], r["status"], r["transcript"] or ""] for r in rows]
    return hashlib.sha256(json.dumps(values, ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def current_source(db, identifiers):
    """Detect changed/deleted clips and new clips inserted between source anchors."""
    if not identifiers:
        return []
    first = db.execute("SELECT device,started,id FROM chunks WHERE id=?", (identifiers[0],)).fetchone()
    last = db.execute("SELECT device,started,id FROM chunks WHERE id=?", (identifiers[-1],)).fetchone()
    if first is None or last is None or first["device"] != last["device"]:
        return []
    return db.execute("""SELECT id,started,duration,status,transcript FROM chunks
        WHERE device=? AND (started,id)>=(?,?) AND (started,id)<=(?,?) ORDER BY started,id""",
        (first["device"], first["started"], first["id"], last["started"], last["id"])).fetchall()


def _names(db):
    return {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _rows(db):
    return db.execute("SELECT id,started,duration,'' AS transcript FROM chunks ORDER BY started,id").fetchall()


def _public_suggestion(row):
    return {key: row[key] for key in ("id", "revision", "title", "start_chunk_id", "end_chunk_id", "state", "model", "prompt_version", "accepted_edit_id")} | {
        "source": "topic_suggestion", "source_ids": json.loads(row["source_ids"]), "human_override": False}


def list_timeline(db, day=None, limit=200):
    """Read persisted metadata only; no model, derived rebuild, or schema writes."""
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 500:
        raise event_edits.EditError(400, "Invalid timeline limit")
    bounds = None
    if day is not None:
        try:
            start = datetime.strptime(day, "%Y-%m-%d").replace(tzinfo=meetings.DISPLAY_ZONE)
            if start.strftime("%Y-%m-%d") != day:
                raise ValueError()
            bounds = (meetings.format_utc(start), meetings.format_utc(start + timedelta(days=1)))
        except (TypeError, ValueError):
            raise event_edits.EditError(400, "Invalid timeline day") from None
    def included(start, end):
        return bounds is None or (start < bounds[1] and (end or start) >= bounds[0])
    entries = []
    names = _names(db)
    if "intervals" in names:
        where = " WHERE started_at<? AND coalesce(ended_at,deadline,started_at)>=?" if bounds else ""
        args = (bounds[1], bounds[0], limit + 1) if bounds else (limit + 1,)
        for row in db.execute("SELECT * FROM intervals" + where + " ORDER BY started_at DESC,id LIMIT ?", args):
            entries.append({key: row[key] for key in ("id", "source", "label", "started_at", "ended_at", "closed", "closure_reason", "status")} | {
                "revision": None, "source_ids": json.loads(row["chunk_ids"] or "[]"),
                "human_override": row["source"] == "manual", "reasons": json.loads(row["reasons"] or "[]")})
    rows = _rows(db)
    chunks = {r["id"]: r for r in rows}
    if "event_edits" in names:
        for edit in event_edits.load(db):
            first, last = chunks.get(edit["start_chunk_id"]), chunks.get(edit["end_chunk_id"])
            if first is None or last is None or not included(first["started"], last["started"]):
                continue
            span, error = event_edits._locate(event_edits._chunks(rows), first["id"], last["id"])
            if error:
                continue
            lineage = db.execute("SELECT source_ids FROM timeline_lineage WHERE edit_id=?", (edit["id"],)).fetchone() if "timeline_lineage" in names else None
            entries.append(edit | {"source": "human_topic", "human_override": True,
                                  "source_ids": json.loads(lineage[0]) if lineage else [edit["id"]],
                                  "chunk_ids": [r["id"] for r in span], "started_at": first["started"],
                                  "ended_at": meetings.format_utc(meetings.chunk_span(last)[1])})
    suggestions = []
    if "topic_suggestions" in names:
        query = """SELECT s.* FROM topic_suggestions s JOIN chunks c ON c.id=s.start_chunk_id
            JOIN chunks e ON e.id=s.end_chunk_id WHERE s.state='proposed'"""
        args = ()
        if bounds:
            query += " AND c.started<? AND e.started>=?"
            args = (bounds[1], bounds[0])
        for row in db.execute(query + " ORDER BY s.created_at DESC,s.id LIMIT ?", args + (limit + 1,)):
            suggestions.append(_public_suggestion(row))
    entries.sort(key=lambda r: (r["started_at"], r["id"]), reverse=True)
    return {"items": entries[:limit], "suggestions": suggestions[:limit],
            "truncated": len(entries) > limit or len(suggestions) > limit}


@contextmanager
def _atomic(db):
    name = "timeline_" + uuid.uuid4().hex
    db.execute("SAVEPOINT " + name)
    try:
        yield
        db.execute("RELEASE " + name)
    except Exception:
        db.execute("ROLLBACK TO " + name)
        db.execute("RELEASE " + name)
        raise


def publish_suggestions(db, *, fingerprint, rows, segments, model, prompt_version, now=None):
    """Store validated model proposals; saved human boundaries take precedence."""
    now = time.time() if now is None else now
    source_ids = [r["id"] for r in rows]
    source_hash = source_fingerprint(rows)
    for old in db.execute("SELECT id,source_ids FROM topic_suggestions WHERE state='proposed' AND fingerprint!=?", (fingerprint,)).fetchall():
        if set(json.loads(old["source_ids"])).intersection(source_ids):
            db.execute("UPDATE topic_suggestions SET state='superseded',revision=revision+1,updated_at=? WHERE id=?", (now, old["id"]))
    positions = {identifier: i for i, identifier in enumerate(source_ids)}
    manual = set()
    all_rows = _rows(db)
    for edit in event_edits.load(db):
        span, error = event_edits._locate(event_edits._chunks(all_rows), edit["start_chunk_id"], edit["end_chunk_id"])
        if not error:
            manual.update(r["id"] for r in span)
    published = []
    for index, segment in enumerate(segments):
        span_ids = source_ids[positions[segment["start_clip_id"]]:positions[segment["end_clip_id"]] + 1]
        if manual.intersection(span_ids):
            continue
        identifier = "topic_" + hashlib.sha256((fingerprint + ":" + str(index)).encode()).hexdigest()[:32]
        db.execute("""INSERT OR IGNORE INTO topic_suggestions
            (id,revision,fingerprint,source_fingerprint,source_ids,title,start_chunk_id,end_chunk_id,
             state,model,prompt_version,created_at,updated_at) VALUES (?,1,?,?,?,?,?,?,'proposed',?,?,?,?)""",
            (identifier, fingerprint, source_hash, json.dumps(source_ids), segment["title"],
             segment["start_clip_id"], segment["end_clip_id"], model, prompt_version, now, now))
        published.append(identifier)
    return published


def _check(row, expected_revision):
    if row is None:
        raise event_edits.EditError(404, "Unknown timeline item")
    expected = event_edits._revision(expected_revision)
    if row["revision"] != expected:
        raise event_edits.EditError(409, "Revision conflict", revision=row["revision"])


def decide_suggestion(db, identifier, expected_revision, accept, title=None):
    with _atomic(db):
        row = db.execute("SELECT * FROM topic_suggestions WHERE id=?", (identifier,)).fetchone()
        _check(row, expected_revision)
        if row["state"] != "proposed":
            raise event_edits.EditError(409, "Suggestion already decided")
        edit = None
        if accept:
            identifiers = json.loads(row["source_ids"])
            current = current_source(db, identifiers)
            if source_fingerprint(current) != row["source_fingerprint"]:
                raise event_edits.EditError(409, "Suggestion source changed")
            edit = event_edits.save(db, {"title": row["title"] if title is None else title,
                "start_chunk_id": row["start_chunk_id"], "end_chunk_id": row["end_chunk_id"], "expected_revision": 0}, _rows(db))
            _lineage(db, edit["id"], [identifier])
        db.execute("""UPDATE topic_suggestions SET revision=revision+1,state=?,updated_at=?,accepted_edit_id=? WHERE id=? AND revision=?""",
                   ("accepted" if accept else "rejected", time.time(), edit["id"] if edit else None, identifier, expected_revision))
        return {"suggestion": _public_suggestion(db.execute("SELECT * FROM topic_suggestions WHERE id=?", (identifier,)).fetchone()), "edit": edit}


def _lineage(db, edit_id, sources):
    db.execute("INSERT OR REPLACE INTO timeline_lineage(edit_id,source_ids,human_override) VALUES (?,?,1)",
               (edit_id, json.dumps(list(dict.fromkeys(sources)))))


def _ancestors(db, identifier):
    row = db.execute("SELECT source_ids FROM timeline_lineage WHERE edit_id=?", (identifier,)).fetchone()
    return json.loads(row[0]) if row else [identifier]


def split_edit(db, identifier, expected_revision, before_clip_id, titles=None):
    with _atomic(db):
        current = db.execute("SELECT * FROM event_edits WHERE id=?", (identifier,)).fetchone()
        _check(current, expected_revision)
        rows = _rows(db)
        span, error = event_edits._locate(event_edits._chunks(rows), current["start_chunk_id"], current["end_chunk_id"])
        if error:
            raise error
        ids = [r["id"] for r in span]
        if before_clip_id not in ids[1:]:
            raise event_edits.EditError(400, "Split must be inside the event")
        at = ids.index(before_clip_id)
        if titles is None:
            titles = [current["title"], current["title"]]
        if not isinstance(titles, list) or len(titles) != 2:
            raise event_edits.EditError(400, "Two titles required")
        sources = _ancestors(db, identifier)
        left = event_edits.save(db, {"id": identifier, "expected_revision": expected_revision,
            "title": titles[0], "start_chunk_id": ids[0], "end_chunk_id": ids[at - 1]}, rows)
        right = event_edits.save(db, {"expected_revision": 0, "title": titles[1],
            "start_chunk_id": ids[at], "end_chunk_id": ids[-1]}, rows)
        for edit in (left, right):
            _lineage(db, edit["id"], sources)
        return {"edits": [left, right], "source_ids": sources, "human_override": True}


def merge_edits(db, identifiers, expected_revisions, title):
    if not isinstance(identifiers, list) or len(identifiers) != 2 or len(set(identifiers)) != 2 or not isinstance(expected_revisions, list) or len(expected_revisions) != 2:
        raise event_edits.EditError(400, "Two distinct events and revisions required")
    with _atomic(db):
        edits = []
        for identifier, revision in zip(identifiers, expected_revisions):
            row = db.execute("SELECT * FROM event_edits WHERE id=?", (identifier,)).fetchone()
            _check(row, revision)
            edits.append(dict(row))
        rows = _rows(db)
        chunks = event_edits._ordered(event_edits._chunks(rows))
        positions = {r["id"]: i for i, r in enumerate(chunks)}
        edits.sort(key=lambda r: positions[r["start_chunk_id"]])
        left, right = edits
        if positions[left["end_chunk_id"]] + 1 != positions[right["start_chunk_id"]]:
            raise event_edits.EditError(400, "Events must be adjacent")
        sources = _ancestors(db, left["id"]) + _ancestors(db, right["id"])
        db.execute("DELETE FROM event_edits WHERE id=? AND revision=?", (right["id"], right["revision"]))
        edit = event_edits.save(db, {"id": left["id"], "expected_revision": left["revision"], "title": title,
            "start_chunk_id": left["start_chunk_id"], "end_chunk_id": right["end_chunk_id"]}, rows)
        _lineage(db, edit["id"], sources)
        db.execute("UPDATE topic_suggestions SET accepted_edit_id=? WHERE accepted_edit_id=?", (edit["id"], right["id"]))
        return {"edit": edit, "source_ids": list(dict.fromkeys(sources)), "human_override": True}
