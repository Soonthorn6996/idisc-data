"""Tests for the fixed-token guard on the MCP endpoint.

``app.auth`` reads its configuration at import time, so each test reloads the
module under a controlled environment rather than mutating shared state.
"""
from __future__ import annotations

import importlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

VALID_TOKEN = "test-token-that-is-definitely-long-enough-1234567890"


def _reload_auth(monkeypatch, token=None, rest_auth=None):
    if token is None:
        monkeypatch.delenv("MCP_AUTH_TOKEN", raising=False)
    else:
        monkeypatch.setenv("MCP_AUTH_TOKEN", token)
    if rest_auth is None:
        monkeypatch.delenv("REST_AUTH_REQUIRED", raising=False)
    else:
        monkeypatch.setenv("REST_AUTH_REQUIRED", rest_auth)

    import app.auth as auth_module
    return importlib.reload(auth_module)


def _headers(value: str | None, name: bytes = b"authorization") -> dict[bytes, bytes]:
    return {name: value.encode("latin-1")} if value is not None else {}


class TestFailClosed:
    """No usable token must mean no MCP endpoint, never an unguarded one."""

    def test_missing_token_disables_mcp(self, monkeypatch):
        auth = _reload_auth(monkeypatch, token=None)
        assert auth.MCP_ENABLED is False
        assert auth.AUTH_TOKEN is None

    def test_blank_token_disables_mcp(self, monkeypatch):
        auth = _reload_auth(monkeypatch, token="   ")
        assert auth.MCP_ENABLED is False

    def test_short_token_is_refused(self, monkeypatch):
        # A weak shared secret is rejected rather than quietly accepted.
        auth = _reload_auth(monkeypatch, token="changeme")
        assert auth.MCP_ENABLED is False

    def test_authorization_always_fails_without_a_token(self, monkeypatch):
        auth = _reload_auth(monkeypatch, token=None)
        assert auth.is_authorized(_headers(f"Bearer {VALID_TOKEN}")) is False

    def test_rest_auth_cannot_be_enabled_without_a_token(self, monkeypatch):
        auth = _reload_auth(monkeypatch, token=None, rest_auth="true")
        assert auth.REST_AUTH_REQUIRED is False


class TestTokenValidation:
    @pytest.fixture
    def auth(self, monkeypatch):
        return _reload_auth(monkeypatch, token=VALID_TOKEN)

    def test_enabled_with_valid_token(self, auth):
        assert auth.MCP_ENABLED is True

    def test_correct_bearer_token_is_accepted(self, auth):
        assert auth.is_authorized(_headers(f"Bearer {VALID_TOKEN}")) is True

    def test_bearer_scheme_is_case_insensitive(self, auth):
        assert auth.is_authorized(_headers(f"bearer {VALID_TOKEN}")) is True
        assert auth.is_authorized(_headers(f"BEARER {VALID_TOKEN}")) is True

    def test_x_api_key_fallback_is_accepted(self, auth):
        # Some MCP clients can only set a custom header.
        assert auth.is_authorized(_headers(VALID_TOKEN, name=b"x-api-key")) is True

    @pytest.mark.parametrize("value", [
        None,
        "",
        "Bearer",
        "Bearer ",
        "Bearer wrong-token-of-a-similar-length-9999999999999",
        VALID_TOKEN,                      # missing the Bearer scheme
        f"Basic {VALID_TOKEN}",           # wrong scheme
        f"Bearer {VALID_TOKEN}x",         # trailing character
        f"Bearer {VALID_TOKEN[:-1]}",     # truncated
    ])
    def test_bad_credentials_are_rejected(self, auth, value):
        assert auth.is_authorized(_headers(value)) is False

    def test_token_prefix_does_not_authorise(self, auth):
        # Guards against a comparison that stops at the shorter string.
        assert auth.is_authorized(_headers("Bearer test-token")) is False

    def test_fingerprint_does_not_leak_the_token(self, auth):
        fingerprint = auth.token_fingerprint()
        assert fingerprint and len(fingerprint) == 8
        assert VALID_TOKEN not in fingerprint
        # Stable across calls, and distinct per token.
        assert fingerprint == auth.token_fingerprint()
        assert auth.token_fingerprint("a-completely-different-token-value") != fingerprint

    def test_undecodable_header_is_rejected_not_crashed(self, auth):
        assert auth.is_authorized({b"authorization": b"Bearer \xff\xfe"}) is False


class TestMiddleware:
    """The guard must reject over ASGI, and must not eat lifespan events."""

    @pytest.fixture
    def auth(self, monkeypatch):
        return _reload_auth(monkeypatch, token=VALID_TOKEN)

    @pytest.mark.anyio
    async def test_unauthorised_request_gets_401(self, auth):
        sent = []

        async def inner_app(scope, receive, send):  # pragma: no cover
            raise AssertionError("inner app must not be reached")

        middleware = auth.BearerTokenMiddleware(inner_app)
        await middleware(
            {"type": "http", "headers": []},
            None,
            lambda message: sent.append(message) or _noop(),
        )
        assert sent[0]["status"] == 401
        headers = dict(sent[0]["headers"])
        assert b"www-authenticate" in headers

    @pytest.mark.anyio
    async def test_authorised_request_reaches_the_inner_app(self, auth):
        reached = []

        async def inner_app(scope, receive, send):
            reached.append(True)

        middleware = auth.BearerTokenMiddleware(inner_app)
        await middleware(
            {"type": "http",
             "headers": [(b"authorization", f"Bearer {VALID_TOKEN}".encode())]},
            None,
            None,
        )
        assert reached == [True]

    @pytest.mark.anyio
    async def test_lifespan_scope_passes_through_unauthenticated(self, auth):
        """Blocking lifespan would stop the mounted MCP app from ever starting."""
        reached = []

        async def inner_app(scope, receive, send):
            reached.append(scope["type"])

        middleware = auth.BearerTokenMiddleware(inner_app)
        await middleware({"type": "lifespan"}, None, None)
        assert reached == ["lifespan"]


async def _noop():
    return None


@pytest.fixture
def anyio_backend():
    return "asyncio"
