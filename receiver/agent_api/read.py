from __future__ import annotations

import json
import sqlite3

from agent_api import cursors, schemas
from agent_api.errors import AgentError
from agent_api.identity import canonical_event, cursor_key
from agent_api.search import RESPONSE_LIMIT, _people, _show, _stamp, open_read, clip_citation, transcript_revision, recording_revision

def _label(db, chunk_id: str, include_unconfirmed: bool) -> str | None:
    people = _people(db, [chunk_id], include_unconfirmed)
    if not people:
        return None
    text = ", ".join(person["name"] for person in people)
    return text[:128]


def _pieces(db, event_id: str, include_unconfirmed: bool) -> list[dict]:
    rows = list(db.execute(
        """SELECT c.id, c.started, COALESCE(c.transcript, '') AS transcript
           FROM agent_event_members m JOIN chunks c ON c.id = m.chunk_id
           WHERE m.event_id=?""",
        (event_id,),
    ))
    ordered = sorted(rows, key=lambda row: (_stamp(row["started"]) or _stamp("9999-01-01T00:00:00Z"), row["id"]))
    pieces = []
    for row in ordered:
        text = row["transcript"] or ""
        if not text.strip():
            continue
        pieces.append({
            "chunk_id": row["id"],
            "start": _show(_stamp(row["started"])),
            "speaker": _label(db, row["id"], include_unconfirmed),
            "text": text,
        })
    return pieces


def _slice(pieces: list[dict], start_index: int, start_offset: int, budget: int) -> tuple[list[dict], int, int, bool]:
    excerpts = []
    remaining = budget
    index = start_index
    offset = start_offset
    while index < len(pieces) and remaining > 0:
        piece = pieces[index]
        text = piece["text"][offset:]
        take = text[:remaining]
        if not take:
            index += 1
            offset = 0
            continue
        excerpts.append({
            "chunk_id": piece["chunk_id"],
            "start": piece["start"],
            "speaker": piece["speaker"],
            "speaker_scope": "clip_associations",
            "attribution": "unknown",
            "start_offset": offset,
            "end_offset": offset + len(take),
            "offset_unit": "unicode_code_points",
            "text": take,
            "citation": clip_citation(piece["chunk_id"], piece["text"], _stamp(piece["start"]), offset, offset + len(take)),
        })
        remaining -= len(take)
        if len(take) < len(text):
            offset += len(take)
            break
        index += 1
        offset = 0
    finished = index >= len(pieces)
    return excerpts, index, offset, finished


def _dumps(payload: dict) -> bytes:
    return json.dumps(payload).encode()


def read_event(db_path, event_id: str, body: dict) -> dict:
    if event_id.startswith("rec_"):
        return _read_recording(db_path, event_id, body)
    request = schemas.parse_read(body)
    db = open_read(db_path)
    try:
        db.execute("BEGIN")
        event = canonical_event(db, event_id)
        if event is None:
            raise AgentError(404, "not_found", "That event was not found.")
        pieces = _pieces(db, event["id"], request["include_unconfirmed"])
        turns, turns_truncated = _turns(db, None, request["include_unconfirmed"], event_id=event["id"])
        while turns and len(json.dumps(turns).encode()) > 4096:
            turns.pop()
            turns_truncated = True
        key = cursor_key(db)
        finger = schemas.fingerprint({
            "id": event["id"],
            "mode": request["mode"],
            "include_unconfirmed": request["include_unconfirmed"],
            "max_chars": request["max_chars"],
            "anchor": request["anchor"],
            "context_before": request["context_before"],
        })
        start_index = 0
        start_offset = 0
        if request["anchor"]:
            anchor = request["anchor"]
            if anchor["revision"] != event["revision"]:
                raise AgentError(409, "stale_cursor", "The event changed. Search again.")
            indexes = [i for i, piece in enumerate(pieces) if piece["chunk_id"] == anchor["chunk_id"]]
            if not indexes or anchor["offset"] >= len(pieces[indexes[0]]["text"]):
                raise AgentError(400, "invalid_input", "Anchor is outside this event's transcript.")
            start_index = indexes[0]
            start_offset = max(0, anchor["offset"] - min(request["context_before"], request["max_chars"] - 1))
        if request["cursor"]:
            payload = cursors.decode(key, request["cursor"])
            if payload.get("kind") != "read" or payload.get("fingerprint") != finger or payload.get("event_id") != event["id"]:
                raise AgentError(400, "invalid_input", "The cursor does not match this event. Start again.")
            if payload.get("revision") != event["revision"]:
                raise AgentError(409, "stale_cursor", "The event changed. Open it again.")
            start_index = payload.get("index")
            start_offset = payload.get("offset")
            if not isinstance(start_index, int) or not isinstance(start_offset, int) or start_index < 0 or start_offset < 0:
                raise AgentError(400, "invalid_input", "The cursor is not valid. Start again.")
            if start_index > len(pieces) or (start_index == len(pieces) and start_offset > 0):
                raise AgentError(400, "invalid_input", "The cursor is not valid. Start again.")
            if start_index == len(pieces):
                raise AgentError(400, "invalid_input", "The cursor is already at the end. Start again.")
            if start_index < len(pieces) and start_offset > len(pieces[start_index]["text"]):
                raise AgentError(400, "invalid_input", "The cursor is not valid. Start again.")
        if request["mode"] == "overview":
            budget = 2000
            excerpts, index, offset, finished = _slice(pieces[:3], 0, 0, budget)
            truncated = not finished or len(pieces) > 3 or index < min(3, len(pieces))
            payload = {
                "id": event["id"],
                "requested_id": event_id,
                "mode": "overview",
                "speaker_turns": turns,
                "speaker_turns_truncated": turns_truncated,
                "excerpts": excerpts,
                "truncated": truncated,
                "next_cursor": None,
            }
            if len(_dumps(payload)) > RESPONSE_LIMIT:
                excerpts, _, _, _ = _slice(pieces[:3], 0, 0, 1)
                while excerpts and len(_dumps({**payload, "excerpts": excerpts, "truncated": True})) > RESPONSE_LIMIT:
                    excerpts[-1]["text"] = excerpts[-1]["text"][:-1]
                    excerpts[-1]["end_offset"] -= 1
                    excerpts[-1]["citation"]["end_offset"] -= 1
                    if not excerpts[-1]["text"]:
                        excerpts.pop()
                payload["excerpts"] = excerpts
                payload["truncated"] = True
            return payload
        budget = request["max_chars"]
        excerpts, index, offset, finished = _slice(pieces, start_index, start_offset, budget)
        if not excerpts or not any(item["text"] for item in excerpts):
            raise AgentError(400, "invalid_input", "The cursor is already at the end. Start again.")
        next_cursor = None
        if not finished:
            next_cursor = cursors.encode(key, {
                "kind": "read",
                "event_id": event["id"],
                "revision": event["revision"],
                "fingerprint": finger,
                "index": index,
                "offset": offset,
            })
        payload = {
            "id": event["id"],
            "requested_id": event_id,
            "mode": "transcript",
            "speaker_turns": turns,
            "speaker_turns_truncated": turns_truncated,
            "excerpts": excerpts,
            "truncated": not finished,
            "next_cursor": next_cursor,
        }
        while len(_dumps(payload)) > RESPONSE_LIMIT:
            last = payload["excerpts"][-1]
            if not last["text"]:
                payload["excerpts"].pop()
                if not payload["excerpts"]:
                    raise AgentError(503, "unavailable", "Search is unavailable.")
                continue
            last["text"] = last["text"][:-1]
            last["end_offset"] -= 1
            last["citation"]["end_offset"] -= 1
            payload["truncated"] = True
            # The cursor must resume at the first omitted character.
            consumed = sum(len(item["text"]) for item in payload["excerpts"])
            resume_index = start_index
            resume_offset = start_offset
            left = consumed
            while resume_index < len(pieces) and left > 0:
                available = len(pieces[resume_index]["text"]) - (resume_offset if resume_index == start_index else 0)
                if left < available:
                    resume_offset = (start_offset if resume_index == start_index else 0) + left
                    left = 0
                    break
                left -= available
                resume_index += 1
                resume_offset = 0
            payload["next_cursor"] = cursors.encode(key, {
                "kind": "read",
                "event_id": event["id"],
                "revision": event["revision"],
                "fingerprint": finger,
                "index": resume_index,
                "offset": resume_offset,
            })
        return payload
    except sqlite3.Error as error:
        raise AgentError(503, "unavailable", "Search is unavailable.") from error
    finally:
        db.close()


def _turns(db, chunk_id, include_unconfirmed, event_id=None):
    """Diarization time spans are not word attribution. Never return voice vectors."""
    clause = "st.chunk_id=?" if event_id is None else "st.chunk_id IN (SELECT chunk_id FROM agent_event_members WHERE event_id=?)"
    rows = db.execute("""SELECT st.id, st.chunk_id, st.started, st.ended, st.label_source, p.name
        FROM speaker_turns st LEFT JOIN people p ON p.id=st.person_id
        WHERE """ + clause + " ORDER BY st.chunk_id, st.started, st.ended, st.id LIMIT 33", (chunk_id if event_id is None else event_id,)).fetchall()
    turns = []
    for row in rows[:32]:
        status = "confirmed" if row["name"] and row["label_source"] == "confirmed" else "unconfirmed" if row["name"] else "unknown"
        turns.append({"id": row["id"], "chunk_id": row["chunk_id"], "start_seconds": row["started"], "end_seconds": row["ended"],
                      "status": status, "name": (row["name"] or "")[:128] if status == "confirmed" or include_unconfirmed else None,
                      "attribution": "unknown", "time_unit": "seconds_from_clip_start"})
    return turns, len(rows) > 32


def _read_recording(db_path, event_id, body):
    """Keep the event read route usable for new standalone search records."""
    request = schemas.parse_read(body)
    chunk_id = event_id[4:]
    converted = dict(body)
    if request["anchor"]:
        db = open_read(db_path)
        try:
            row = db.execute("SELECT started,transcript FROM chunks WHERE id=?", (chunk_id,)).fetchone()
            if row is None or not (row["transcript"] or "").strip():
                raise AgentError(404, "not_found", "That recording was not found.")
            anchor = request["anchor"]
            if anchor["chunk_id"] != chunk_id or anchor["offset"] >= len(row["transcript"]):
                raise AgentError(400, "invalid_input", "Anchor is outside this recording.")
            if anchor["revision"] != recording_revision(row["transcript"]):
                raise AgentError(409, "stale_cursor", "The recording changed. Search again.")
            converted.pop("anchor")
            converted["citation"] = clip_citation(chunk_id, row["transcript"], _stamp(row["started"]), anchor["offset"], anchor["offset"] + 1)
        finally:
            db.close()
    page = read_clip(db_path, chunk_id, converted)
    page.update(id=event_id, requested_id=event_id, kind="recording")
    return page


def read_clip(db_path, chunk_id, body):
    if not isinstance(body, dict):
        raise AgentError(400, "invalid_input", "JSON object required.")
    citation = body.get("citation")
    request = schemas.parse_read({k: v for k, v in body.items() if k != "citation" and not (k == "context_before" and citation is not None)})
    if citation is not None and "context_before" in body:
        request["context_before"] = schemas._int(body["context_before"], "context_before", 0, 1000)
    if request["anchor"]:
        raise AgentError(400, "invalid_input", "Use a clip citation for this route.")
    if citation is not None:
        required = {"chunk_id", "transcript_revision", "start_offset", "end_offset", "captured_at", "offset_unit"}
        if not isinstance(citation, dict) or set(citation) != required or citation["chunk_id"] != chunk_id or citation["offset_unit"] != "unicode_code_points":
            raise AgentError(400, "invalid_input", "The clip citation is not valid.")
        if request["mode"] != "transcript":
            raise AgentError(400, "invalid_input", "Clip citations require transcript mode.")
        for field in ("start_offset", "end_offset"):
            schemas._int(citation[field], field, 0, 1000000000)
        if not isinstance(citation["transcript_revision"], str):
            raise AgentError(400, "invalid_input", "The transcript revision is not valid.")
    db = open_read(db_path)
    try:
        db.execute("BEGIN")
        row = db.execute("SELECT started, COALESCE(transcript, '') AS transcript FROM chunks WHERE id=?", (chunk_id,)).fetchone()
        if row is None or not row["transcript"].strip():
            raise AgentError(404, "not_found", "That clip transcript is unavailable.")
        text = row["transcript"]
        revision = transcript_revision(text)
        captured = _show(_stamp(row["started"]))
        if citation is not None:
            if citation["transcript_revision"] != revision or citation["captured_at"] != captured:
                raise AgentError(409, "stale_cursor", "The clip changed. Search again.")
            if not 0 <= citation["start_offset"] < citation["end_offset"] <= len(text):
                raise AgentError(400, "invalid_input", "Citation offsets are outside the clip transcript.")
        key = cursor_key(db)
        finger = schemas.fingerprint({"chunk_id": chunk_id, "request": {k: v for k, v in request.items() if k != "cursor"}, "citation": citation})
        start = max(0, citation["start_offset"] - min(request["context_before"], request["max_chars"] - 1)) if citation else 0
        if request["cursor"]:
            cursor = cursors.decode(key, request["cursor"])
            if cursor.get("kind") != "clip_read" or cursor.get("fingerprint") != finger or cursor.get("chunk_id") != chunk_id:
                raise AgentError(400, "invalid_input", "The cursor does not match this clip.")
            if cursor.get("revision") != revision or cursor.get("captured_at") != captured:
                raise AgentError(409, "stale_cursor", "The clip changed. Open it again.")
            start = cursor.get("offset")
            if isinstance(start, bool) or not isinstance(start, int) or not 0 <= start < len(text):
                raise AgentError(400, "invalid_input", "The cursor is not valid.")
        budget = request["max_chars"] if request["mode"] == "transcript" else 2000
        turns, more_turns = _turns(db, chunk_id, request["include_unconfirmed"])
        # Recompute the signed continuation after reducing either text or metadata.
        while True:
            end = min(len(text), start + budget)
            excerpt = {"chunk_id": chunk_id, "start": captured, "text": text[start:end],
                       "start_offset": start, "end_offset": end, "offset_unit": "unicode_code_points",
                       "attribution": "unknown", "citation": clip_citation(chunk_id, text, _stamp(row["started"]), start, end)}
            next_cursor = cursors.encode(key, {"kind": "clip_read", "fingerprint": finger, "chunk_id": chunk_id,
                "revision": revision, "captured_at": captured, "offset": end}) if end < len(text) and request["mode"] == "transcript" else None
            payload = {"id": chunk_id, "kind": "recording", "mode": request["mode"], "excerpts": [excerpt],
                       "speaker_turns": turns, "speaker_turns_truncated": more_turns,
                       "truncated": end < len(text), "next_cursor": next_cursor}
            if len(_dumps(payload)) <= RESPONSE_LIMIT - 512:
                return payload
            if turns:
                turns.pop()
                more_turns = True
            elif budget > 1:
                budget //= 2
            else:
                raise AgentError(503, "unavailable", "The clip read is unavailable.")
    except sqlite3.Error as error:
        raise AgentError(503, "unavailable", "The clip read is unavailable.") from error
    finally:
        db.close()
