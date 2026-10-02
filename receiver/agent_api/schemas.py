from __future__ import annotations

import hashlib
import json
from datetime import datetime

from agent_api.auth import reject_unknown_fields
from agent_api.errors import AgentError

SEARCH_FIELDS = {"query", "from", "to", "person", "include_unconfirmed", "limit", "cursor", "place"}
READ_FIELDS = {"mode", "include_unconfirmed", "max_chars", "cursor", "anchor", "context_before"}
MAX_BODY_BYTES = 16 * 1024


def _bool(value, label: str) -> bool:
    if not isinstance(value, bool):
        raise AgentError(400, "invalid_input", label + " must be true or false.")
    return value


def _text(value, label: str) -> str:
    if not isinstance(value, str):
        raise AgentError(400, "invalid_input", label + " must be a string.")
    return value


def _int(value, label: str, low: int, high: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise AgentError(400, "invalid_input", f"{label} must be an integer from {low} to {high}.")
    return value


def parse_timestamp(value, label: str) -> datetime:
    text = _text(value, label).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        stamp = datetime.fromisoformat(text)
    except ValueError as error:
        raise AgentError(400, "invalid_input", label + " must be an RFC3339 timestamp with an offset.") from error
    if stamp.tzinfo is None:
        raise AgentError(400, "invalid_input", label + " must include a timezone offset.")
    return stamp


def parse_search(body: dict) -> dict:
    if not isinstance(body, dict):
        raise AgentError(400, "invalid_input", "JSON object required.")
    reject_unknown_fields(body, SEARCH_FIELDS)
    parsed = {
        "query": "",
        "start": None,
        "end": None,
        "person": "",
        "place": "",
        "include_unconfirmed": False,
        "limit": 5,
        "cursor": None,
    }
    if "query" in body:
        parsed["query"] = _text(body["query"], "query")
    if "from" in body:
        parsed["start"] = parse_timestamp(body["from"], "from")
    if "to" in body:
        parsed["end"] = parse_timestamp(body["to"], "to")
    if parsed["start"] and parsed["end"] and parsed["end"] <= parsed["start"]:
        raise AgentError(400, "invalid_input", "to must be later than from.")
    if "person" in body:
        parsed["person"] = _text(body["person"], "person").strip()
    if "place" in body:
        parsed["place"] = _text(body["place"], "place").strip()
        if not parsed["place"] or len(parsed["place"]) > 128:
            raise AgentError(400, "invalid_input", "place must be an exact bounded place id or name.")
    if "include_unconfirmed" in body:
        parsed["include_unconfirmed"] = _bool(body["include_unconfirmed"], "include_unconfirmed")
    if "limit" in body:
        parsed["limit"] = _int(body["limit"], "limit", 1, 10)
    if "cursor" in body and body["cursor"] is not None:
        parsed["cursor"] = _text(body["cursor"], "cursor")
    return parsed


def parse_read(body: dict) -> dict:
    if not isinstance(body, dict):
        raise AgentError(400, "invalid_input", "JSON object required.")
    reject_unknown_fields(body, READ_FIELDS)
    if "mode" not in body:
        raise AgentError(400, "invalid_input", "mode is required.")
    mode = _text(body["mode"], "mode")
    if mode not in ("overview", "transcript"):
        raise AgentError(400, "invalid_input", "mode must be overview or transcript.")
    parsed = {
        "mode": mode,
        "include_unconfirmed": False,
        "max_chars": 2000 if mode == "transcript" else None,
        "cursor": None,
        "anchor": None,
        "context_before": 160,
    }
    if "include_unconfirmed" in body:
        parsed["include_unconfirmed"] = _bool(body["include_unconfirmed"], "include_unconfirmed")
    if mode == "overview":
        if any(field in body for field in ("max_chars", "cursor", "anchor", "context_before")):
            raise AgentError(400, "invalid_input", "overview does not accept paging or anchor fields.")
        return parsed
    if "max_chars" in body:
        parsed["max_chars"] = _int(body["max_chars"], "max_chars", 1, 4000)
    if "cursor" in body and body["cursor"] is not None:
        parsed["cursor"] = _text(body["cursor"], "cursor")
    if "anchor" in body:
        anchor = body["anchor"]
        if not isinstance(anchor, dict) or set(anchor) != {"chunk_id", "offset", "revision"}:
            raise AgentError(400, "invalid_input", "anchor requires chunk_id, offset, and revision.")
        parsed["anchor"] = {
            "chunk_id": _text(anchor["chunk_id"], "chunk_id"),
            "offset": _int(anchor["offset"], "offset", 0, 1000000000),
            "revision": _int(anchor["revision"], "revision", 1, 1000000000),
        }
    if "context_before" in body:
        parsed["context_before"] = _int(body["context_before"], "context_before", 0, 1000)
        if not parsed["anchor"]:
            raise AgentError(400, "invalid_input", "context_before requires an anchor.")
    return parsed


def fingerprint(payload: dict) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode()).hexdigest()
