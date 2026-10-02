from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import uuid
from datetime import datetime, timezone
from urllib.parse import quote
from zoneinfo import ZoneInfo

from agent_api import cursors, schemas
from agent_api.errors import AgentError
from agent_api.identity import cursor_key, generation

ZONE = ZoneInfo("America/New_York")
RESPONSE_LIMIT = 16 * 1024
TOKEN = re.compile(r"[^\W_]+", re.UNICODE)


def open_read(path) -> sqlite3.Connection:
    uri = "file:" + quote(str(path), safe="/") + "?mode=ro"
    try:
        db = sqlite3.connect(uri, uri=True, timeout=30)
    except sqlite3.Error as error:
        raise AgentError(503, "unavailable", "Search is unavailable.") from error
    db.row_factory = sqlite3.Row
    return db


def _ready(db) -> None:
    names = {row[0] for row in db.execute("SELECT name FROM sqlite_master")}
    if "agent_api_state" not in names or "agent_transcripts" not in names or "agent_transcript_fts" not in names:
        raise AgentError(503, "unavailable", "Search is unavailable.")


def _stamp(value: str) -> datetime | None:
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


def _show(stamp: datetime | None) -> str | None:
    if stamp is None:
        return None
    return stamp.astimezone(ZONE).isoformat(timespec="seconds")


def _tokens(query: str) -> list[str]:
    if not query.strip():
        return []
    found = TOKEN.findall(query)
    if not found:
        raise AgentError(400, "invalid_input", "query has no searchable words.")
    return found


def _words(text: str, count: int) -> str:
    parts = (text or "").split()
    if count <= 0:
        return ""
    return " ".join(parts[:count])


def _query_in(db, sql: str, values: list, extra: tuple = ()):
    from agent_api.identity import _batches
    rows = []
    for batch in _batches(list(values)):
        placeholders = ",".join("?" for _ in batch)
        rows.extend(db.execute(sql.format(placeholders=placeholders), (*extra, *batch)).fetchall())
    return rows


def _people(db, chunk_ids: list[str], include_unconfirmed: bool) -> list[dict]:
    if not chunk_ids:
        return []
    rows = _query_in(
        db,
        """SELECT p.name, st.label_source FROM speaker_turns st
            JOIN people p ON p.id = st.person_id
            WHERE st.person_id IS NOT NULL AND st.chunk_id IN ({placeholders})
            ORDER BY st.started, st.id""",
        chunk_ids,
    )
    found: dict[str, str] = {}
    for row in rows:
        status = "confirmed" if row["label_source"] == "confirmed" else "unconfirmed"
        if not include_unconfirmed and status != "confirmed":
            continue
        name = row["name"]
        if name in found and found[name] == "confirmed":
            continue
        found[name] = status
    return [{"name": name, "status": status} for name, status in found.items()]


def _chunk_people(db, include_unconfirmed: bool, chunk_ids: list[str] | None = None) -> dict[str, set[str]]:
    if chunk_ids is not None and not chunk_ids:
        return {}
    if chunk_ids is None:
        rows = db.execute(
            """SELECT st.chunk_id, p.name, st.label_source FROM speaker_turns st
               JOIN people p ON p.id = st.person_id
               WHERE st.person_id IS NOT NULL"""
        ).fetchall()
    else:
        rows = _query_in(
            db,
            """SELECT st.chunk_id, p.name, st.label_source FROM speaker_turns st
               JOIN people p ON p.id = st.person_id
               WHERE st.person_id IS NOT NULL AND st.chunk_id IN ({placeholders})""",
            chunk_ids,
        )
    found: dict[str, set[str]] = {}
    for row in rows:
        if row["label_source"] != "confirmed" and not include_unconfirmed:
            continue
        found.setdefault(row["chunk_id"], set()).add((row["name"] or "").casefold())
    return found


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def _evidence(item: dict, width: int = 480) -> dict:
    text = item["preview"]
    hit = item["match_offset"]
    start = max(0, hit - width // 3)
    end = min(len(text), start + width)
    evidence = {
        "chunk_id": item["chunk_id"], "start": _show(item["chunk_start"]),
        "text": text[start:end], "start_offset": start, "end_offset": end,
        "offset_unit": "unicode_code_points", "attribution": "unknown",
        "clip_read_path": "/v1/clips/" + item["chunk_id"] + "/read",
        "anchor": {"chunk_id": item["chunk_id"], "offset": hit,
                   "revision": item["revision"]},
    }
    evidence["citation"] = clip_citation(item["chunk_id"], text, item["chunk_start"], start, end)
    return evidence


def transcript_revision(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def recording_revision(text: str) -> int:
    """Legacy event anchors need a bounded integer even for singleton records."""
    return int(transcript_revision(text)[:15], 16) % 999999999 + 1


def clip_citation(chunk_id, text, stamp, start, end):
    return {"chunk_id": chunk_id, "transcript_revision": transcript_revision(text),
            "start_offset": start, "end_offset": end,
            "captured_at": _show(stamp), "offset_unit": "unicode_code_points"}


def _preview(evidence: dict) -> str:
    words = list(re.finditer(r"\S+", evidence["text"]))
    if not words:
        return ""
    hit = evidence["anchor"]["offset"] - evidence["start_offset"]
    index = next((i for i, word in enumerate(words) if word.end() > hit), 0)
    start = max(0, index - 20)
    return " ".join(word.group() for word in words[start:start + 60])


def search_events(db_path, body: dict, *, experimental_lexical: bool = False) -> dict:
    request = schemas.parse_search(body)
    tokens = _tokens(request["query"])
    if experimental_lexical:
        # Offline evaluator only: no HTTP field enables this candidate.
        stop = {"when", "did", "we", "discuss", "what", "was", "the", "about", "please", "find", "said", "where"}
        aliases = {"authorization": "approval", "spending": "budget", "hiring": "recruitment"}
        tokens = [aliases.get(token.casefold(), token) for token in tokens if token.casefold() not in stop]
        if request["query"].strip() and not tokens:
            return {"events": [], "next_cursor": None}
    db = open_read(db_path)
    try:
        db.execute("BEGIN")
        _ready(db)
        current = generation(db)
        key = cursor_key(db)
        finger = schemas.fingerprint({
            "query": request["query"].strip(),
            "start": request["start"].isoformat() if request["start"] else None,
            "end": request["end"].isoformat() if request["end"] else None,
            "person": request["person"].casefold(),
            "place": request["place"].casefold(),
            "include_unconfirmed": request["include_unconfirmed"],
            "limit": request["limit"],
            "experimental_lexical": experimental_lexical,
        })
        offset_key = None
        if request["cursor"]:
            payload = cursors.decode(key, request["cursor"])
            if payload.get("kind") != "search" or payload.get("fingerprint") != finger:
                raise AgentError(400, "invalid_input", "The cursor does not match this search. Start again.")
            if payload.get("generation") != current:
                raise AgentError(409, "stale_cursor", "The results changed. Start the search again.")
            offset_key = payload.get("key")
        scored = _candidates(db, tokens, request)
        ordered = sorted(scored, key=lambda item: item["sort"])
        start = 0
        if offset_key is not None:
            for index, item in enumerate(ordered):
                if item["sort"] == tuple(offset_key):
                    start = index + 1
                    break
            else:
                raise AgentError(400, "invalid_input", "The cursor does not match this search. Start again.")
        page = ordered[start:start + request["limit"]]
        more = ordered[start + request["limit"]:start + request["limit"] + 1]
        events = []
        for item in page:
            evidence = _evidence(item)
            events.append({
                "id": item["id"],
                "kind": item["kind"],
                "start": _show(item["start"]),
                "end": _show(item["end"]),
                "preview": _preview(evidence),
                "match": evidence,
                "people_scope": "clip_associations" if item["kind"] == "recording" else "event_associations",
                "people": item["people"],
            })
            if request["place"]:
                from place_context import clip_context
                events[-1]["location"] = clip_context(db, item["chunk_id"])
        next_cursor = None
        if more:
            next_cursor = cursors.encode(key, {
                "kind": "search",
                "generation": current,
                "fingerprint": finger,
                "key": list(page[-1]["sort"]),
            })
        payload = {"events": events, "next_cursor": next_cursor}
        width = 480
        while len(_dumps(payload)) > RESPONSE_LIMIT and width > 1:
            width = max(1, width // 2)
            for event, item in zip(payload["events"], page):
                event["match"] = _evidence(item, width)
                event["preview"] = _preview(event["match"])
            payload["next_cursor"] = next_cursor
        if len(_dumps(payload)) > RESPONSE_LIMIT:
            raise AgentError(503, "unavailable", "Search is unavailable.")
        return payload
    except sqlite3.Error as error:
        raise AgentError(503, "unavailable", "Search is unavailable.") from error
    finally:
        db.close()


def _matched_chunks(db, tokens: list[str]) -> dict[str, tuple[float, str, str, int]]:
    """Return chunk id -> (bm25, started, transcript, match offset)."""
    match = " AND ".join('"' + token.replace('"', '""') + '"' for token in tokens)
    marker = "lr-hit-" + uuid.uuid4().hex
    try:
        rows = db.execute(
            """SELECT t.chunk_id, bm25(agent_transcript_fts) AS score,
                      c.started, COALESCE(c.transcript, '') AS transcript,
                      t.body AS indexed_text, highlight(agent_transcript_fts, 0, ?, '') AS highlighted
               FROM agent_transcript_fts
               JOIN agent_transcripts t ON t.rowid = agent_transcript_fts.rowid
               JOIN chunks c ON c.id = t.chunk_id
               WHERE agent_transcript_fts MATCH ?""",
            (marker, match),
        ).fetchall()
    except sqlite3.Error as error:
        raise AgentError(503, "unavailable", "Search is unavailable.") from error
    found: dict[str, tuple[float, str, str, int]] = {}
    for row in rows:
        # Offsets must refer to the exact indexed source, never a stale transcript.
        if row["indexed_text"] != row["transcript"] or marker in row["transcript"]:
            raise AgentError(503, "unavailable", "Search evidence is updating. Try again.")
        score = float(row["score"])
        previous = found.get(row["chunk_id"])
        if previous is None or score < previous[0]:
            found[row["chunk_id"]] = (score, row["started"], row["transcript"] or "",
                                      max(0, row["highlighted"].find(marker)))
    return found


def _live_members(db, chunk_ids: list[str] | None = None) -> dict[str, str]:
    if chunk_ids is not None and not chunk_ids:
        return {}
    if chunk_ids is None:
        rows = db.execute(
            """SELECT m.event_id, m.chunk_id FROM agent_event_members m
               JOIN agent_events e ON e.id = m.event_id AND e.tombstoned = 0"""
        )
    else:
        rows = _query_in(
            db,
            """SELECT m.event_id, m.chunk_id FROM agent_event_members m
                JOIN agent_events e ON e.id = m.event_id AND e.tombstoned = 0
                WHERE m.chunk_id IN ({placeholders})""",
            chunk_ids,
        )
    return {row["chunk_id"]: row["event_id"] for row in rows}


def _candidates(db, tokens: list[str], request: dict) -> list[dict]:
    place_id = None
    located = set()
    if request["place"]:
        from place_context import place_filter, tagged_clips
        place_id = place_filter(db, request["place"])
        located = {row[0] for row in db.execute("SELECT chunk_id FROM clip_place_context WHERE place_id=? AND resolution='known'", (place_id,))}
        located.update(tagged_clips(db, place_id))
    matched = _matched_chunks(db, tokens) if tokens else {}
    if tokens and not matched:
        return []
    if tokens:
        membership = _live_members(db, list(matched))
        chunk_rows = [
            {"id": chunk_id, "started": item[1], "transcript": item[2]}
            for chunk_id, item in matched.items()
        ]
    else:
        membership = _live_members(db)
        chunk_rows = db.execute("SELECT id, started, COALESCE(transcript, '') AS transcript FROM chunks WHERE TRIM(COALESCE(transcript, '')) != ''").fetchall()
    # Sidebar grouping is presentation, not a condition for corpus coverage.
    for row in chunk_rows:
        if (row["transcript"] or "").strip():
            membership.setdefault(row["id"], "rec_" + row["id"])
    chunks = {row["id"]: row for row in chunk_rows}
    scoped = list(chunks)
    names = _chunk_people(db, request["include_unconfirmed"], scoped)
    wanted = request["person"].casefold()
    grouped: dict[str, dict] = {}
    for chunk_id, event_id in membership.items():
        if place_id and chunk_id not in located:
            continue
        row = chunks.get(chunk_id)
        if row is None:
            continue
        stamp = _stamp(row["started"])
        if request["start"] or request["end"]:
            if stamp is None:
                continue
            if request["start"] and stamp < request["start"].astimezone(timezone.utc):
                continue
            if request["end"] and stamp >= request["end"].astimezone(timezone.utc):
                continue
        if wanted and wanted not in names.get(chunk_id, set()):
            continue
        if not tokens and not (row["transcript"] or "").strip() and not request["start"] and not request["end"] and not wanted:
            continue
        bucket = grouped.setdefault(event_id, {"chunks": []})
        bucket["chunks"].append({
            "id": chunk_id,
            "start": stamp or datetime.max.replace(tzinfo=timezone.utc),
            "transcript": row["transcript"] or "",
            "score": matched.get(chunk_id, (0.0,))[0],
            "match_offset": matched[chunk_id][3] if tokens else 0,
        })
    event_ids = list(grouped)
    if not event_ids:
        return []
    events = {
        row["id"]: row for row in _query_in(
            db,
            "SELECT id, start_at, end_at, revision FROM agent_events WHERE tombstoned=0 AND id IN ({placeholders})",
            event_ids,
        )
    }
    member_ids: dict[str, list[str]] = {}
    for row in _query_in(
        db,
        "SELECT event_id, chunk_id FROM agent_event_members WHERE event_id IN ({placeholders})",
        event_ids,
    ):
        member_ids.setdefault(row["event_id"], []).append(row["chunk_id"])
    results = []
    for event_id, bucket in grouped.items():
        event = events.get(event_id)
        if not bucket["chunks"]:
            continue
        recording = event is None
        if recording:
            event = {"start_at": None, "end_at": None,
                     "revision": recording_revision(bucket["chunks"][0]["transcript"])}
        best = min(item["score"] for item in bucket["chunks"])
        earliest = min(bucket["chunks"], key=lambda item: (item["start"], item["id"]))
        relevant = min(bucket["chunks"], key=lambda item: (item["score"], item["start"], item["id"]))
        start = _stamp(event["start_at"]) or earliest["start"]
        end = _stamp(event["end_at"])
        if tokens:
            sort = (round(best, 6), -(start.timestamp() if start else 0), event_id)
        else:
            sort = (-(start.timestamp() if start else 0), event_id)
        results.append({
            "id": event_id,
            "kind": "recording" if recording else "event",
            "start": start,
            "end": end,
            "preview": relevant["transcript"],
            "chunk_id": relevant["id"], "chunk_start": relevant["start"],
            "match_offset": relevant["match_offset"], "revision": event["revision"],
            "people": _people(db, member_ids.get(event_id, [relevant["id"]]), request["include_unconfirmed"]),
            "sort": sort,
        })
    return results
