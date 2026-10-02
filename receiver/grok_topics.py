"""Bounded, tool-free xAI Responses adapter using existing host-local Pi auth.

No agent session, file tools, web tools, provider fallback or transcript logging.
Provider response metadata is checked outside model-generated content. Credentials
are read-only: Pi owns renewal. Expired credentials fail closed rather than racing
Pi's writers or rotating a shared refresh token.
"""
import hashlib
import http.client
import json
import math
import os
from pathlib import Path
import socket
import ssl
import stat
import threading
import time
import urllib.parse
import uuid

ENDPOINT = "https://api.x.ai/v1/responses"
MAX_INPUT_BYTES = 32 * 1024
MAX_RESPONSE_BYTES = 128 * 1024
MAX_TEXT_BYTES = 16 * 1024
TIMEOUT_SECONDS = 120
ADAPTER_VERSION = "xai-pi-oauth-topics-v1"
ADAPTER_SHA256 = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()


class RouteError(Exception):
    def __init__(self, code, *, http_status=None):
        self.code = code
        self.http_status = http_status
        super().__init__(code)


def _request(url, body, headers, *, timeout):
    """Trusted fixed-host HTTPS only, no redirects/retries, bounded capture."""
    if url != ENDPOINT:
        raise RouteError("untrusted_endpoint")
    parsed = urllib.parse.urlsplit(url)
    connection = http.client.HTTPSConnection(parsed.hostname, timeout=timeout,
                                              context=ssl.create_default_context())
    active_socket = None
    # A socket inactivity timeout alone can be extended indefinitely by a
    # trickling response. Interrupt the socket at the overall job deadline.
    def interrupt():
        # getresponse() may clear connection.sock for Connection: close while
        # its response file still owns the socket. Keep the original handle.
        active = active_socket
        if active is not None:
            try:
                active.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
    deadline = threading.Timer(timeout, interrupt)
    deadline.daemon = True
    deadline.start()
    try:
        connection.request("POST", parsed.path, body=body, headers=headers)
        active_socket = connection.sock
        response = connection.getresponse()
        # read(n) bounds memory even if the sender omits/forges Content-Length.
        raw = response.read(MAX_RESPONSE_BYTES + 1)
        if len(raw) > MAX_RESPONSE_BYTES:
            raise RouteError("output_limit")
        if response.status != 200:
            if response.status in (401, 403):
                raise RouteError("authentication_unavailable", http_status=response.status)
            if response.status == 429:
                raise RouteError("provider_rate_limited", http_status=response.status)
            raise RouteError("provider_unavailable", http_status=response.status)
        try:
            payload = json.loads(raw)
        except (ValueError, UnicodeError):
            raise RouteError("invalid_provider_output") from None
        if not isinstance(payload, dict):
            raise RouteError("invalid_provider_output")
        return payload, response.getheader("x-request-id")
    except (OSError, TimeoutError, http.client.HTTPException):
        raise RouteError("transport_unavailable") from None
    finally:
        deadline.cancel()
        connection.close()


def _read_auth(path):
    try:
        path = Path(path)
        if not path.is_absolute():
            raise RouteError("unsafe_auth_file")
        # Inspect and read the same descriptor; reject symlinks even if the
        # credential owner replaces the path between lookup and read.
        with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as file:
            info = os.fstat(file.fileno())
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid()
                    or info.st_mode & 0o077 or info.st_size > 128 * 1024):
                raise RouteError("unsafe_auth_file")
            raw = file.read(128 * 1024 + 1)
            if len(raw) > 128 * 1024:
                raise RouteError("unsafe_auth_file")
        data = json.loads(raw)
        credential = data.get("xai")
        if not isinstance(credential, dict):
            raise RouteError("authentication_unavailable")
        if credential.get("type") == "oauth":
            expiry = credential.get("expires")
            if (not isinstance(credential.get("access"), str) or not credential["access"]
                    or not isinstance(credential.get("refresh"), str) or not credential["refresh"]
                    or isinstance(expiry, bool) or not isinstance(expiry, (float, int))
                    or not math.isfinite(expiry)):
                raise RouteError("authentication_unavailable")
        elif credential.get("type") == "api_key":
            if not isinstance(credential.get("key"), str) or not credential["key"]:
                raise RouteError("authentication_unavailable")
        else:
            raise RouteError("authentication_unavailable")
        return data, credential
    except (OSError, ValueError, TypeError, AttributeError):
        raise RouteError("authentication_unavailable") from None


def preflight(auth_file, model):
    if (not isinstance(model, str) or not model or len(model) > 120
            or not all(c.isalnum() or c in "-._/" for c in model)):
        raise RouteError("model_unconfigured")
    _access_token(auth_file)
    config_digest = hashlib.sha256(json.dumps({"model": model, "endpoint": ENDPOINT,
        "adapter": ADAPTER_VERSION, "tools": [], "store": False}, sort_keys=True).encode()).hexdigest()
    return {"provider": "xai", "requested_model": model, "endpoint": ENDPOINT,
            "adapter_version": ADAPTER_VERSION,
            "adapter_sha256": ADAPTER_SHA256,
            "configuration_sha256": config_digest,
            "tools": False, "web_search": False, "store": False}


def _access_token(auth_file, *, request=_request, now=None):
    """Read-only auth. Never refresh or replace Pi's shared credential file."""
    now = time.time() if now is None else now
    _, credential = _read_auth(auth_file)
    if credential["type"] == "api_key":
        return credential["key"]
    if credential["expires"] > (now + 150) * 1000:
        return credential["access"]
    raise RouteError("authentication_expired_renew_in_pi")


def run(auth_file, model, prompt, *, request=_request, now=None):
    provenance = preflight(auth_file, model)
    if not isinstance(prompt, str) or len(prompt.encode()) > MAX_INPUT_BYTES:
        raise RouteError("input_limit")
    token = _access_token(auth_file, request=request, now=now)
    client_request = str(uuid.uuid4())
    # xAI rejects tool_choice when tools is empty. No tools are registered or
    # executed; all tool output types are rejected below, including web calls.
    body = {"model": model, "input": [{"role": "user", "content": prompt}],
            "tools": [],
            "store": False, "stream": False, "max_output_tokens": 4096,
            "reasoning": {"effort": "low"}}
    payload, server_request = request(ENDPOINT, json.dumps(body).encode(),
        {"Content-Type": "application/json", "Accept": "application/json",
         "Authorization": "Bearer " + token, "X-Client-Request-Id": client_request}, timeout=TIMEOUT_SECONDS)
    # A model's prose cannot self-attest routing. Only provider envelope metadata.
    if payload.get("model") != model:
        raise RouteError("model_mismatch")
    response_id = payload.get("id")
    if not isinstance(response_id, str) or not response_id or len(response_id) > 200:
        raise RouteError("response_unverified")
    if payload.get("status") != "completed":
        raise RouteError("response_incomplete")
    if payload.get("store") is not False:
        raise RouteError("storage_policy_mismatch")
    if payload.get("tools", []) != []:
        raise RouteError("unexpected_tool_output")
    output = payload.get("output")
    if not isinstance(output, list) or not output or len(output) > 16:
        raise RouteError("invalid_provider_output")
    text = []
    for item in output:
        if not isinstance(item, dict):
            raise RouteError("invalid_provider_output")
        if item.get("type") == "reasoning":
            continue
        if item.get("type") != "message" or item.get("role") != "assistant":
            raise RouteError("unexpected_tool_output")
        contents = item.get("content")
        if not isinstance(contents, list):
            raise RouteError("invalid_provider_output")
        for content in contents:
            if (not isinstance(content, dict) or content.get("type") != "output_text"
                    or not isinstance(content.get("text"), str)):
                raise RouteError("invalid_provider_output")
            text.append(content["text"])
    result = "".join(text)
    if not result or len(result.encode()) > MAX_TEXT_BYTES:
        raise RouteError("output_limit")
    provenance.update({"effective_model": payload["model"], "response_id": response_id,
                       "client_request_id": client_request, "provider_request_id": server_request,
                       "verified_at": time.time(), "input_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    return result, provenance
