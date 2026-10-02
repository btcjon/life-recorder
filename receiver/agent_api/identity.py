from __future__ import annotations

import secrets
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone

from agent_api.errors import AgentError

ZONE_NAME = "America/New_York"


SCHEMA_VERSION = 2
# Stay under SQLite's default host parameter cap (commonly 999) and this build's higher cap.
SQL_BATCH = 500


def _batches(values: list, size: int = SQL_BATCH):
    for start in range(0, len(values), size):
        yield values[start:start + size]


def _ensure_fts(db) -> None:
    """Keep chunk text in an ordinary keyed table. FTS5 equality is always a scan."""
    db.execute("""CREATE TABLE IF NOT EXISTS agent_transcripts (
        chunk_id TEXT PRIMARY KEY,
        body TEXT NOT NULL
    )""")
    # table_xinfo also returns FTS5's hidden table/rank columns. Only visible
    # columns describe the index schema; otherwise every ensure drops a valid
    # index and silently leaves existing keyed transcripts unsearchable.
    columns = [row[1] for row in db.execute("PRAGMA table_xinfo(agent_transcript_fts)") if row[6] == 0]
    if columns == ["body"]:
        return
    legacy = None
    if columns:
        legacy = "agent_transcript_fts_legacy"
        db.execute(f"ALTER TABLE agent_transcript_fts RENAME TO {legacy}")
    db.execute("""CREATE VIRTUAL TABLE agent_transcript_fts USING fts5(
        body, tokenize='unicode61'
    )""")
    if legacy and "chunk_id" in columns and "body" in columns:
        for chunk_id, body in db.execute(f"SELECT chunk_id, COALESCE(body, '') FROM {legacy}"):
            if not body.strip():
                continue
            db.execute("INSERT INTO agent_transcripts (chunk_id, body) VALUES (?, ?)", (chunk_id, body))
            rowid = db.execute("SELECT rowid FROM agent_transcripts WHERE chunk_id=?", (chunk_id,)).fetchone()[0]
            db.execute("INSERT INTO agent_transcript_fts(rowid, body) VALUES (?, ?)", (rowid, body))
    if legacy:
        db.execute(f"DROP TABLE {legacy}")


def ensure_schema(db) -> None:
    db.execute("""CREATE TABLE IF NOT EXISTS agent_events (
        id TEXT PRIMARY KEY,
        created_seq INTEGER NOT NULL,
        revision INTEGER NOT NULL,
        tombstoned INTEGER NOT NULL DEFAULT 0,
        start_at TEXT,
        end_at TEXT
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS agent_event_members (
        event_id TEXT NOT NULL,
        chunk_id TEXT NOT NULL,
        PRIMARY KEY (event_id, chunk_id)
    )""")
    db.execute("CREATE INDEX IF NOT EXISTS agent_event_members_chunk ON agent_event_members (chunk_id)")
    db.execute("""CREATE TABLE IF NOT EXISTS agent_event_aliases (
        alias_id TEXT PRIMARY KEY,
        canonical_id TEXT NOT NULL
    )""")
    db.execute("""CREATE TABLE IF NOT EXISTS agent_api_state (
        id INTEGER PRIMARY KEY CHECK (id = 1),
        schema_version INTEGER NOT NULL,
        search_generation INTEGER NOT NULL,
        cursor_key BLOB NOT NULL
    )""")
    if db.execute("SELECT 1 FROM agent_api_state WHERE id=1").fetchone() is None:
        db.execute(
            "INSERT INTO agent_api_state (id, schema_version, search_generation, cursor_key) VALUES (1, ?, 1, ?)",
            (SCHEMA_VERSION, secrets.token_bytes(32)),
        )
    else:
        db.execute("UPDATE agent_api_state SET schema_version=? WHERE id=1 AND schema_version<?", (SCHEMA_VERSION, SCHEMA_VERSION))
    _ensure_fts(db)


def _parse_started(value: str) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError:
        return None
    if stamp.tzinfo is None:
        return None
    return stamp.astimezone(timezone.utc)


def _utc_text(stamp: datetime) -> str:
    return stamp.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _load_chunks(db) -> list[dict]:
    rows = db.execute("SELECT id, started, duration, COALESCE(transcript, '') AS transcript FROM chunks")
    chunks = []
    for row in rows:
        try:
            duration = float(row["duration"] or 0)
        except (TypeError, ValueError):
            duration = 0.0
        chunks.append({
            "id": row["id"],
            "started": row["started"],
            "duration": duration,
            "transcript": row["transcript"] or "",
        })
    return chunks


def _groups(chunks: list[dict], display_blocks) -> list[dict]:
    by_id = {chunk["id"]: chunk for chunk in chunks}
    groups = []
    for block in display_blocks(chunks):
        if block.get("kind") != "event":
            continue
        ids = [str(chunk_id) for chunk_id in block["chunk_ids"]]
        first = _parse_started(by_id[ids[0]]["started"]) if ids else None
        last = by_id[ids[-1]]
        last_start = _parse_started(last["started"])
        end = None
        if last_start is not None:
            end = last_start + timedelta(seconds=float(last["duration"] or 0))
        groups.append({
            "chunk_ids": ids,
            "start": first or datetime.max.replace(tzinfo=timezone.utc),
            "end": end,
            "key": min(ids) if ids else "",
        })
    return groups


def _members(db) -> dict[str, set[str]]:
    found: dict[str, set[str]] = {}
    for row in db.execute("SELECT event_id, chunk_id FROM agent_event_members"):
        found.setdefault(row["event_id"], set()).add(row["chunk_id"])
    return found


def _fts_rows(db, chunk_ids: list[str] | None = None) -> dict[str, str]:
    if chunk_ids is not None and not chunk_ids:
        return {}
    found: dict[str, str] = {}
    if chunk_ids is None:
        rows = db.execute("SELECT chunk_id, body FROM agent_transcripts")
        batches = [rows]
    else:
        batches = []
        for batch in _batches(list(chunk_ids)):
            placeholders = ",".join("?" for _ in batch)
            batches.append(db.execute(
                f"SELECT chunk_id, body FROM agent_transcripts WHERE chunk_id IN ({placeholders})",
                batch,
            ))
    for rows in batches:
        for row in rows:
            found[row["chunk_id"]] = row["body"] or ""
    return found


def _replace_fts(db, chunk_id: str, text: str) -> None:
    row = db.execute("SELECT rowid FROM agent_transcripts WHERE chunk_id=?", (chunk_id,)).fetchone()
    if row is not None:
        db.execute("DELETE FROM agent_transcript_fts WHERE rowid=?", (row["rowid"],))
        db.execute("DELETE FROM agent_transcripts WHERE chunk_id=?", (chunk_id,))
    if text.strip():
        db.execute("INSERT INTO agent_transcripts (chunk_id, body) VALUES (?, ?)", (chunk_id, text))
        rowid = db.execute("SELECT rowid FROM agent_transcripts WHERE chunk_id=?", (chunk_id,)).fetchone()["rowid"]
        db.execute("INSERT INTO agent_transcript_fts(rowid, body) VALUES (?, ?)", (rowid, text))


def _flatten_aliases(db) -> None:
    rows = list(db.execute("SELECT alias_id, canonical_id FROM agent_event_aliases"))
    mapping = {row["alias_id"]: row["canonical_id"] for row in rows}
    for alias, target in rows:
        seen = set()
        current = target
        while current in mapping and current not in seen:
            seen.add(current)
            current = mapping[current]
        if current != target and current != alias:
            db.execute("UPDATE agent_event_aliases SET canonical_id=? WHERE alias_id=?", (current, alias))
        if current == alias:
            db.execute("DELETE FROM agent_event_aliases WHERE alias_id=?", (alias,))


def reconcile(db, display_blocks, touched_chunk_ids=None) -> None:
    """Rebuild durable event ids from the current grouping. Search does not call this."""
    ensure_schema(db)
    chunks = _load_chunks(db)
    by_id = {chunk["id"]: chunk for chunk in chunks}
    indexed = _fts_rows(db, [chunk["id"] for chunk in chunks])
    changed_text = set()
    for chunk in chunks:
        if indexed.get(chunk["id"], "") != (chunk["transcript"] or ""):
            changed_text.add(chunk["id"])
    groups = _groups(chunks, display_blocks)
    active = list(db.execute(
        "SELECT id, created_seq, revision, start_at, end_at FROM agent_events WHERE tombstoned=0 ORDER BY created_seq, id"
    ))
    members = _members(db)
    group_by_chunk: dict[str, int] = {}
    for index, group in enumerate(groups):
        for chunk_id in group["chunk_ids"]:
            group_by_chunk[chunk_id] = index
    claims: dict[int, list] = {}
    claimed_events = set()
    for event in active:
        owned = members.get(event["id"], set())
        overlap: dict[int, int] = {}
        for chunk_id in owned:
            index = group_by_chunk.get(chunk_id)
            if index is None:
                continue
            overlap[index] = overlap.get(index, 0) + 1
        if not overlap:
            continue
        best_index = min(overlap, key=lambda index: (-overlap[index], groups[index]["start"], groups[index]["key"]))
        claims.setdefault(best_index, []).append(event)
        claimed_events.add(event["id"])
    dirty = False
    next_seq = db.execute("SELECT COALESCE(MAX(created_seq), 0) FROM agent_events").fetchone()[0]
    kept_ids = set()
    for index, group in enumerate(groups):
        contenders = sorted(claims.get(index, []), key=lambda item: (item["created_seq"], item["id"]))
        start_at = _utc_text(group["start"]) if group["start"] != datetime.max.replace(tzinfo=timezone.utc) else None
        end_at = _utc_text(group["end"]) if group["end"] is not None else None
        member_ids = list(group["chunk_ids"])
        if not contenders:
            next_seq += 1
            event_id = "evt_" + uuid.uuid4().hex
            db.execute(
                "INSERT INTO agent_events (id, created_seq, revision, tombstoned, start_at, end_at) VALUES (?, ?, 1, 0, ?, ?)",
                (event_id, next_seq, start_at, end_at),
            )
            for chunk_id in member_ids:
                db.execute("INSERT INTO agent_event_members (event_id, chunk_id) VALUES (?, ?)", (event_id, chunk_id))
            dirty = True
            kept_ids.add(event_id)
            continue
        keeper = contenders[0]
        kept_ids.add(keeper["id"])
        previous = members.get(keeper["id"], set())
        current = set(member_ids)
        moved = previous != current or keeper["start_at"] != start_at or keeper["end_at"] != end_at
        content_changed = bool(current & changed_text)
        if moved or content_changed:
            db.execute(
                "UPDATE agent_events SET revision=revision+1, start_at=?, end_at=?, tombstoned=0 WHERE id=?",
                (start_at, end_at, keeper["id"]),
            )
            dirty = True
        else:
            db.execute("UPDATE agent_events SET start_at=?, end_at=? WHERE id=?", (start_at, end_at, keeper["id"]))
        db.execute("DELETE FROM agent_event_members WHERE event_id=?", (keeper["id"],))
        for chunk_id in member_ids:
            db.execute("INSERT INTO agent_event_members (event_id, chunk_id) VALUES (?, ?)", (keeper["id"], chunk_id))
        for retired in contenders[1:]:
            db.execute("UPDATE agent_events SET tombstoned=1, revision=revision+1 WHERE id=?", (retired["id"],))
            db.execute("DELETE FROM agent_event_members WHERE event_id=?", (retired["id"],))
            db.execute(
                "INSERT INTO agent_event_aliases (alias_id, canonical_id) VALUES (?, ?) ON CONFLICT(alias_id) DO UPDATE SET canonical_id=excluded.canonical_id",
                (retired["id"], keeper["id"]),
            )
            db.execute("DELETE FROM agent_event_aliases WHERE alias_id=?", (keeper["id"],))
            dirty = True
    for event in active:
        if event["id"] in claimed_events or event["id"] in kept_ids:
            continue
        db.execute("UPDATE agent_events SET tombstoned=1, revision=revision+1 WHERE id=?", (event["id"],))
        db.execute("DELETE FROM agent_event_members WHERE event_id=?", (event["id"],))
        dirty = True
    _flatten_aliases(db)
    for chunk in chunks:
        current = chunk["transcript"] or ""
        stored = indexed.get(chunk["id"])
        if stored is None:
            if current.strip():
                _replace_fts(db, chunk["id"], current)
                dirty = True
        elif stored != current:
            _replace_fts(db, chunk["id"], current)
            dirty = True
    stale = [chunk_id for chunk_id in _fts_rows(db) if chunk_id not in by_id]
    for chunk_id in stale:
        _replace_fts(db, chunk_id, "")
        dirty = True
    if dirty:
        db.execute("UPDATE agent_api_state SET search_generation=search_generation+1 WHERE id=1")



def note_speaker_change(db, chunk_ids) -> None:
    """A label or name change alters search hits without changing transcript text."""
    ids = sorted({chunk_id for chunk_id in chunk_ids if chunk_id})
    if not ids:
        return
    ensure_schema(db)
    live = None
    for batch in _batches(ids):
        placeholders = ",".join("?" for _ in batch)
        live = db.execute(
            f"""SELECT 1 FROM agent_event_members m
                JOIN agent_events e ON e.id = m.event_id AND e.tombstoned = 0
                WHERE m.chunk_id IN ({placeholders}) LIMIT 1""",
            batch,
        ).fetchone()
        if live is not None:
            break
    if live is None:
        # A singleton transcript is searchable even without event membership.
        # Its person filter changes must invalidate search continuation too.
        for batch in _batches(ids):
            placeholders = ",".join("?" for _ in batch)
            live = db.execute(f"SELECT 1 FROM agent_transcripts WHERE chunk_id IN ({placeholders}) LIMIT 1", batch).fetchone()
            if live is not None:
                break
        if live is None:
            return
    for batch in _batches(ids):
        placeholders = ",".join("?" for _ in batch)
        db.execute(
            """UPDATE agent_events SET revision = revision + 1
               WHERE tombstoned = 0 AND id IN (
                 SELECT event_id FROM agent_event_members WHERE chunk_id IN (""" + placeholders + """)
               )""",
            batch,
        )
    db.execute("UPDATE agent_api_state SET search_generation = search_generation + 1 WHERE id = 1")


def generation(db) -> int:
    row = db.execute("SELECT search_generation FROM agent_api_state WHERE id=1").fetchone()
    if row is None:
        raise AgentError(503, "unavailable", "Search is unavailable.")
    return int(row["search_generation"])


def cursor_key(db) -> bytes:
    row = db.execute("SELECT cursor_key FROM agent_api_state WHERE id=1").fetchone()
    if row is None or not row["cursor_key"]:
        raise AgentError(503, "unavailable", "Search is unavailable.")
    return bytes(row["cursor_key"])


def canonical_event(db, event_id: str) -> sqlite3.Row | None:
    seen = set()
    current = event_id
    while current not in seen:
        seen.add(current)
        alias = db.execute("SELECT canonical_id FROM agent_event_aliases WHERE alias_id=?", (current,)).fetchone()
        if alias is None:
            break
        current = alias["canonical_id"]
    row = db.execute("SELECT * FROM agent_events WHERE id=?", (current,)).fetchone()
    if row is None or row["tombstoned"]:
        return None
    return row
