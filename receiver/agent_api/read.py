from __future__ import annotations

import json
import sqlite3

from agent_api import cursors, schemas
from agent_api.errors import AgentError
from agent_api.identity import canonical_event, cursor_key
from agent_api.search import RESPONSE_LIMIT, _people, _show, _stamp, open_read

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
            "text": take,
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
    request = schemas.parse_read(body)
    db = open_read(db_path)
    try:
        event = canonical_event(db, event_id)
        if event is None:
            raise AgentError(404, "not_found", "That event was not found.")
        pieces = _pieces(db, event["id"], request["include_unconfirmed"])
        key = cursor_key(db)
        finger = schemas.fingerprint({
            "id": event["id"],
            "mode": request["mode"],
            "include_unconfirmed": request["include_unconfirmed"],
            "max_chars": request["max_chars"],
        })
        start_index = 0
        start_offset = 0
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
                "excerpts": excerpts,
                "truncated": truncated,
                "next_cursor": None,
            }
            if len(_dumps(payload)) > RESPONSE_LIMIT:
                excerpts, _, _, _ = _slice(pieces[:3], 0, 0, 1)
                while excerpts and len(_dumps({**payload, "excerpts": excerpts, "truncated": True})) > RESPONSE_LIMIT:
                    excerpts[-1]["text"] = excerpts[-1]["text"][:-1]
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
