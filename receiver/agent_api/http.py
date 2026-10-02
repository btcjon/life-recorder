from __future__ import annotations

import json
from urllib.parse import urlparse

from access_auth import AccessAuthError, VerificationUnavailable

from agent_api.auth import allowlist, classify_claims
from agent_api.errors import AgentError
from agent_api.rate_limit import Limiter
from agent_api.read import read_event, read_clip
from agent_api.schemas import MAX_BODY_BYTES
from agent_api.search import search_events

_EVENT_READ = "/read"


def access_outcome(handler):
    """Validate a remote JWT once per request and keep the result for the human path."""
    cached = getattr(handler, "_access_outcome", None)
    if cached is not None:
        return cached
    if not handler._is_remote_host():
        outcome = ("local", None)
    else:
        verifier = getattr(handler.server, "access_verifier", None)
        if verifier is None:
            outcome = ("missing", None)
        else:
            token = handler.headers.get("Cf-Access-Jwt-Assertion", "")
            try:
                claims = verifier.validate(token)
            except VerificationUnavailable as error:
                outcome = ("unavailable", error)
            except AccessAuthError:
                outcome = ("rejected", None)
            else:
                kind, client_id = classify_claims(claims, allowlist())
                outcome = (kind, client_id)
    handler._access_outcome = outcome
    return outcome


def _decision(handler):
    kind, client_id = access_outcome(handler)
    if kind in ("local", "missing", "rejected", "human"):
        return "skip", None
    if kind == "unavailable":
        return "unavailable", None
    return kind, client_id


def _error(handler, status: int, code: str, message: str, retry_after: int | None = None):
    extra = {"Retry-After": str(retry_after)} if retry_after else None
    handler._json(status, {"error": {"code": code, "message": message}}, extra)


def _route(path: str):
    if path == "/v1/search":
        return "search", None
    marker = "/v1/events/"
    clip_marker = "/v1/clips/"
    if path.startswith(clip_marker) and path.endswith(_EVENT_READ):
        chunk_id = path[len(clip_marker):-len(_EVENT_READ)]
        return ("clip", chunk_id) if chunk_id and "/" not in chunk_id else ("invalid", None)
    if path.startswith(marker) and path.endswith(_EVENT_READ):
        event_id = path[len(marker):-len(_EVENT_READ)]
        if "/" in event_id or not event_id:
            return "invalid", None
        return "read", event_id
    return None, None


def _body(handler) -> dict:
    if handler.headers.get("Transfer-Encoding"):
        raise AgentError(400, "invalid_input", "Send a JSON body.")
    if handler.headers.get_content_type() != "application/json":
        raise AgentError(400, "invalid_input", "Content-Type must be application/json.")
    try:
        length = int(handler.headers.get("Content-Length", ""))
    except (TypeError, ValueError):
        raise AgentError(400, "invalid_input", "Content-Length is required.")
    if length < 0 or length > MAX_BODY_BYTES:
        raise AgentError(400, "invalid_input", "JSON body must be 16 KiB or smaller.")
    raw = handler.rfile.read(length)
    if len(raw) != length:
        raise AgentError(400, "invalid_input", "Incomplete body.")
    try:
        body = json.loads(raw)
    except (ValueError, json.JSONDecodeError) as error:
        raise AgentError(400, "invalid_input", "JSON object required.") from error
    if not isinstance(body, dict):
        raise AgentError(400, "invalid_input", "JSON object required.")
    return body


def intercept(handler, method: str) -> bool:
    """Handle a machine credential. Return False to continue the human/local path."""
    kind, client_id = _decision(handler)
    if kind == "skip":
        return False
    if kind == "unavailable":
        # The token could not be classified. Agent routes keep the nested shape;
        # every other route uses the human viewer's flat error.
        parsed = urlparse(handler.path)
        route, _event_id = _route(parsed.path)
        if method == "POST" and route is not None:
            _error(handler, 503, "unavailable", "Access verification is unavailable.")
            return True
        raise handler._access_outcome[1]
    if kind == "forbidden":
        _error(handler, 403, "forbidden", "This credential cannot use that route.")
        return True
    parsed = urlparse(handler.path)
    if method != "POST":
        _error(handler, 403, "forbidden", "This credential cannot use that route.")
        return True
    route, event_id = _route(parsed.path)
    if route is None:
        _error(handler, 403, "forbidden", "This credential cannot use that route.")
        return True
    limiter = getattr(handler.server, "agent_limiter", None)
    if limiter is None:
        limiter = Limiter()
        handler.server.agent_limiter = limiter
    allowed, retry = limiter.allow(client_id)
    if not allowed:
        _error(handler, 503, "rate_limited", "Too many requests. Try again later.", retry)
        return True
    if parsed.query:
        _error(handler, 400, "invalid_input", "Send the search text in the JSON body.")
        return True
    try:
        body = _body(handler)
        inbox = handler._inbox()
        if route == "search":
            payload = search_events(inbox.db, body)
        elif route == "invalid":
            raise AgentError(400, "invalid_input", "Event id is not valid.")
        elif route == "clip":
            payload = read_clip(inbox.db, event_id, body)
        else:
            payload = read_event(inbox.db, event_id, body)
    except AgentError as error:
        _error(handler, error.status, error.code, error.message, error.retry_after)
        return True
    handler._json(200, payload)
    return True
