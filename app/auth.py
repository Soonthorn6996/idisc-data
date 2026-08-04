"""Fixed-token authentication for the MCP endpoint (and optionally REST).

Design notes
------------
* **Fail closed.** If no token is configured the MCP endpoint is not mounted at
  all, rather than mounted without protection. An unauthenticated MCP server
  exposed on a public URL is worse than a missing feature.
* **Constant-time comparison.** :func:`secrets.compare_digest` avoids leaking the
  token through response-timing differences.
* **Raw ASGI middleware, not ``BaseHTTPMiddleware``.** Streamable HTTP may hold a
  long-lived streaming response open; ``BaseHTTPMiddleware`` buffers and can
  deadlock such responses, so the guard is implemented at the ASGI level.
* **The token is never logged.** A short SHA-256 prefix is logged instead so
  operators can confirm *which* token is loaded without exposing it.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import secrets
from typing import Optional

logger = logging.getLogger(__name__)

# Shorter than this is not worth calling a secret. 32 chars of base64url is
# ~192 bits; the floor is set low enough to allow deliberate test tokens but
# high enough to reject "changeme".
MIN_TOKEN_LENGTH = 24

_ENV_TOKEN = "MCP_AUTH_TOKEN"
_ENV_REST_AUTH = "REST_AUTH_REQUIRED"


def _read_token() -> Optional[str]:
    raw = os.environ.get(_ENV_TOKEN, "")
    token = raw.strip()
    if not token:
        return None
    if len(token) < MIN_TOKEN_LENGTH:
        # Refuse rather than silently accepting a weak shared secret.
        logger.error(
            "%s is set but only %d characters; refusing to use it "
            "(minimum %d). The MCP endpoint stays disabled.",
            _ENV_TOKEN, len(token), MIN_TOKEN_LENGTH,
        )
        return None
    return token


AUTH_TOKEN: Optional[str] = _read_token()
# Compared as bytes: secrets.compare_digest raises TypeError on str inputs that
# contain non-ASCII characters, so a malformed header would surface as a 500
# instead of a clean 401. Bytes comparison accepts any input.
_AUTH_TOKEN_BYTES: Optional[bytes] = (
    AUTH_TOKEN.encode("utf-8") if AUTH_TOKEN is not None else None
)
MCP_ENABLED: bool = AUTH_TOKEN is not None
REST_AUTH_REQUIRED: bool = (
    os.environ.get(_ENV_REST_AUTH, "").strip().lower() in {"1", "true", "yes", "on"}
    and AUTH_TOKEN is not None
)


def token_fingerprint(token: Optional[str] = None) -> Optional[str]:
    """First 8 hex chars of the token's SHA-256 - safe to log or display."""
    value = token if token is not None else AUTH_TOKEN
    if not value:
        return None
    return hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]


def _extract_presented_token(headers: dict[bytes, bytes]) -> Optional[str]:
    """Pull the caller's token from ``Authorization`` or ``X-API-Key``.

    ``Authorization: Bearer <token>`` is the documented form. ``X-API-Key`` is
    accepted too because some MCP clients only allow setting a custom header.
    """
    auth = headers.get(b"authorization")
    if auth:
        try:
            scheme, _, value = auth.decode("latin-1").partition(" ")
        except UnicodeDecodeError:
            return None
        if scheme.lower() == "bearer" and value.strip():
            return value.strip()

    api_key = headers.get(b"x-api-key")
    if api_key:
        try:
            return api_key.decode("latin-1").strip() or None
        except UnicodeDecodeError:
            return None
    return None


def is_authorized(headers: dict[bytes, bytes]) -> bool:
    if not _AUTH_TOKEN_BYTES:
        return False
    presented = _extract_presented_token(headers)
    if not presented:
        return False
    return secrets.compare_digest(presented.encode("utf-8"), _AUTH_TOKEN_BYTES)


class BearerTokenMiddleware:
    """ASGI middleware enforcing the fixed token on everything below it."""

    def __init__(self, app, realm: str = "idisc-mcp"):
        self.app = app
        self.realm = realm

    async def __call__(self, scope, receive, send):
        # Non-HTTP scopes (lifespan, websocket) must pass through untouched, or
        # the mounted app never starts up.
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = dict(scope.get("headers") or [])
        if is_authorized(headers):
            await self.app(scope, receive, send)
            return

        await self._reject(send, presented=bool(_extract_presented_token(headers)))

    async def _reject(self, send, presented: bool) -> None:
        detail = (
            "Token ไม่ถูกต้อง (invalid token)"
            if presented
            else "ต้องระบุ Authorization: Bearer <token> (missing credentials)"
        )
        body = json.dumps(
            {
                "error": "unauthorized",
                "detail": detail,
                "hint": "ส่ง header: Authorization: Bearer <MCP_AUTH_TOKEN>",
            },
            ensure_ascii=False,
        ).encode("utf-8")

        await send({
            "type": "http.response.start",
            "status": 401,
            "headers": [
                (b"content-type", b"application/json; charset=utf-8"),
                (b"www-authenticate", f'Bearer realm="{self.realm}"'.encode("latin-1")),
                (b"content-length", str(len(body)).encode("latin-1")),
                (b"cache-control", b"no-store"),
            ],
        })
        await send({"type": "http.response.body", "body": body})


def log_auth_status() -> None:
    """Report the auth posture once at startup, without leaking the secret."""
    if MCP_ENABLED:
        logger.info(
            "MCP endpoint enabled with fixed-token auth (token fingerprint sha256:%s)",
            token_fingerprint(),
        )
    else:
        logger.warning(
            "%s not set (or too short) - the MCP endpoint is DISABLED. "
            "Set a token of at least %d characters to enable it.",
            _ENV_TOKEN, MIN_TOKEN_LENGTH,
        )
    if REST_AUTH_REQUIRED:
        logger.info("REST API also requires the token (%s is on)", _ENV_REST_AUTH)
