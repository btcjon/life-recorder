"""Cloudflare Access JWT checks for the optional remote viewer."""
from __future__ import annotations

import time
from dataclasses import dataclass
from urllib.parse import urlparse

JWKS_TIMEOUT = 3
JWKS_CACHE_SECONDS = 300
ALLOWED_ALGORITHMS = ("RS256",)


class AccessAuthError(Exception):
    pass


class VerificationUnavailable(AccessAuthError):
    """JWKS or key lookup failed. Callers should fail closed with 503."""


@dataclass(frozen=True)
class RemoteAccessConfig:
    host: str
    team_domain: str
    audience: str
    issuer: str
    origin: str
    jwks_url: str

    @classmethod
    def from_values(cls, host: str, team_domain: str, audience: str, issuer: str | None = None):
        host = (host or "").strip().lower()
        team = (team_domain or "").strip().lower().removeprefix("https://").strip("/")
        audience = (audience or "").strip()
        if not host or host in {"127.0.0.1", "localhost"} or "/" in host or ":" in host:
            raise AccessAuthError("Remote host must be an exact public hostname")
        if not team or "/" in team or not audience:
            raise AccessAuthError("Cloudflare Access team domain and audience are required")
        issuer = (issuer or f"https://{team}").strip().rstrip("/")
        parsed = urlparse(issuer)
        if parsed.scheme != "https" or parsed.netloc.lower() != team or parsed.path not in ("", "/"):
            raise AccessAuthError("Issuer must be https://<team-domain>")
        return cls(
            host=host,
            team_domain=team,
            audience=audience,
            issuer=issuer,
            origin=f"https://{host}",
            jwks_url=f"https://{team}/cdn-cgi/access/certs",
        )



def _token_key_rejected(error: Exception) -> bool:
    """A bad or missing kid is the caller's token. A failed JWKS fetch is not."""
    try:
        from jwt.exceptions import PyJWKClientConnectionError, PyJWKClientError
    except ImportError:
        return False
    if isinstance(error, PyJWKClientConnectionError):
        return False
    if not isinstance(error, PyJWKClientError):
        return False
    message = str(error)
    return message.startswith("Unable to find a signing key")

class AccessVerifier:
    def __init__(self, config: RemoteAccessConfig, client=None):
        self.config = config
        self._client = client
        self._jwks = None
        self._jwks_at = 0.0

    def _jwks_client(self):
        if self._client is not None:
            return self._client
        try:
            from jwt import PyJWKClient
        except ImportError as error:
            raise AccessAuthError("PyJWT is required for remote viewer mode") from error
        self._client = PyJWKClient(
            self.config.jwks_url,
            cache_keys=True,
            lifespan=JWKS_CACHE_SECONDS,
            timeout=JWKS_TIMEOUT,
        )
        return self._client

    def validate(self, token: str, now: float | None = None) -> dict:
        if not token or not isinstance(token, str) or token.count(".") != 2:
            raise AccessAuthError("Missing Access JWT")
        try:
            import jwt
            from jwt import InvalidTokenError
        except ImportError as error:
            raise AccessAuthError("PyJWT is required for remote viewer mode") from error
        try:
            header = jwt.get_unverified_header(token)
        except Exception as error:
            raise AccessAuthError("Invalid Access JWT") from error
        if header.get("alg") != "RS256" or header.get("typ") not in (None, "JWT"):
            raise AccessAuthError("Access JWT must be RS256")
        try:
            signing_key = self._jwks_client().get_signing_key_from_jwt(token)
        except AccessAuthError:
            raise
        except InvalidTokenError as error:
            raise AccessAuthError("Invalid Access JWT") from error
        except Exception as error:
            if _token_key_rejected(error):
                raise AccessAuthError("Invalid Access JWT") from error
            raise VerificationUnavailable("Access verification is unavailable") from error
        try:
            payload = jwt.decode(
                token,
                signing_key.key,
                algorithms=list(ALLOWED_ALGORITHMS),
                audience=self.config.audience,
                issuer=self.config.issuer,
                leeway=0,
                options={
                    "require": ["exp", "iss", "aud"],
                    "verify_nbf": True,
                    "verify_exp": True,
                },
            )
        except InvalidTokenError as error:
            raise AccessAuthError("Access JWT rejected") from error
        except AccessAuthError:
            raise
        except Exception as error:
            raise AccessAuthError("Access JWT rejected") from error
        if now is not None:
            if int(payload["exp"]) <= now:
                raise AccessAuthError("Access JWT expired")
            nbf = payload.get("nbf")
            if nbf is not None and int(nbf) > now:
                raise AccessAuthError("Access JWT not yet valid")
        return payload
