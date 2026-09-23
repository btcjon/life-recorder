from __future__ import annotations

import base64
import hashlib
import hmac
import json

from agent_api.errors import AgentError


def _pad(text: str) -> str:
    return text + "=" * (-len(text) % 4)


def encode(key: bytes, payload: dict) -> str:
    body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    mac = hmac.new(key, body, hashlib.sha256).digest()
    packed = json.dumps({
        "payload": payload,
        "mac": base64.urlsafe_b64encode(mac).decode().rstrip("="),
    }, separators=(",", ":")).encode()
    return base64.urlsafe_b64encode(packed).decode().rstrip("=")


def decode(key: bytes, cursor: str) -> dict:
    try:
        packed = json.loads(base64.urlsafe_b64decode(_pad(cursor)))
        payload = packed["payload"]
        mac = base64.urlsafe_b64decode(_pad(packed["mac"]))
        body = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    except (KeyError, ValueError, TypeError, json.JSONDecodeError) as error:
        raise AgentError(400, "invalid_input", "The cursor is not valid. Start the request again.") from error
    if not hmac.compare_digest(hmac.new(key, body, hashlib.sha256).digest(), mac):
        raise AgentError(400, "invalid_input", "The cursor is not valid. Start the request again.")
    if not isinstance(payload, dict):
        raise AgentError(400, "invalid_input", "The cursor is not valid. Start the request again.")
    return payload
