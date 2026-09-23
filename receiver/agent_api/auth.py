"""Machine identity from a verified Cloudflare Access JWT.

A service-token application token carries the Client ID in common_name, an
empty sub, and no email. Human browser tokens are not that shape. The
allowlist is LIFE_RECORDER_AGENT_CLIENT_IDS. Caller-supplied client headers
are not consulted; Cloudflare has already replaced them with the signed JWT.
"""

from __future__ import annotations

import os

from agent_api.errors import AgentError


def allowlist() -> set[str]:
    raw = os.environ.get("LIFE_RECORDER_AGENT_CLIENT_IDS", "")
    return {part.strip() for part in raw.split(",") if part.strip()}


def service_token_client_id(claims: dict) -> str | None:
    name = claims.get("common_name")
    if not isinstance(name, str) or not name:
        return None
    sub = claims.get("sub", "")
    if sub not in ("", None):
        return None
    if claims.get("email"):
        return None
    return name


def classify_claims(claims: dict, allowed: set[str] | None = None) -> tuple[str, str | None]:
    """Return (human|machine|forbidden, client id)."""
    client_id = service_token_client_id(claims)
    if client_id is None:
        return "human", None
    if client_id in (allowed if allowed is not None else allowlist()):
        return "machine", client_id
    return "forbidden", None


def reject_unknown_fields(body: dict, allowed: set[str]) -> None:
    unknown = sorted(set(body) - allowed)
    if unknown:
        raise AgentError(400, "invalid_input", "Remove unsupported fields: " + ", ".join(unknown) + ".")
