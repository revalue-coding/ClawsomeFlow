"""Tests for :mod:`app.api._auth_internal`."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from starlette.requests import Request

from app.api import _auth_internal as auth_internal
from app.api.errors import ApiError
from app.config import load_config, save_config
from app.integrations import internal_token as it


def _request_with_client(host: str) -> Request:
    scope = {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": "GET",
        "scheme": "http",
        "path": "/api/internal/task-decompose/commit",
        "raw_path": b"/api/internal/task-decompose/commit",
        "query_string": b"",
        "headers": [],
        "client": (host, 39000),
        "server": ("127.0.0.1", 17017),
        "root_path": "",
    }
    return Request(scope)


@pytest.fixture
def fixed_internal_secret() -> Iterator[None]:
    original = load_config(force_reload=True)
    save_config(original.model_copy(update={"internal_token_secret": "fixed-internal-secret"}))
    try:
        yield
    finally:
        save_config(original)


def test_is_loopback_accepts_known_hosts() -> None:
    assert auth_internal._is_loopback("127.0.0.1") is True  # noqa: SLF001
    assert auth_internal._is_loopback("127.12.34.56") is True  # noqa: SLF001
    assert auth_internal._is_loopback("::1") is True  # noqa: SLF001
    assert auth_internal._is_loopback("localhost") is True  # noqa: SLF001
    assert auth_internal._is_loopback("testclient") is True  # noqa: SLF001


def test_is_loopback_rejects_non_loopback() -> None:
    assert auth_internal._is_loopback("192.168.1.10") is False  # noqa: SLF001
    assert auth_internal._is_loopback("8.8.8.8") is False  # noqa: SLF001
    assert auth_internal._is_loopback("not-an-ip-host") is False  # noqa: SLF001


def test_require_loopback_allows_loopback_client() -> None:
    auth_internal.require_loopback(_request_with_client("127.0.0.1"))


def test_require_loopback_rejects_non_loopback_client() -> None:
    with pytest.raises(ApiError) as exc:
        auth_internal.require_loopback(_request_with_client("203.0.113.77"))
    assert exc.value.code == "UNAUTHORIZED"
    assert exc.value.status_code == 401


def test_require_internal_token_rejects_missing_or_non_bearer() -> None:
    with pytest.raises(ApiError) as missing:
        auth_internal.require_internal_token(None)
    assert missing.value.code == "UNAUTHORIZED"

    with pytest.raises(ApiError) as non_bearer:
        auth_internal.require_internal_token("Token abc")
    assert non_bearer.value.code == "UNAUTHORIZED"


def test_require_internal_token_rejects_invalid_token(fixed_internal_secret) -> None:
    with pytest.raises(ApiError) as exc:
        auth_internal.require_internal_token("Bearer not-a-token")
    assert exc.value.code == "UNAUTHORIZED"
    assert exc.value.status_code == 401


def test_require_internal_token_accepts_valid_token(fixed_internal_secret) -> None:
    token = it.mint_token(request_id="req-int-1", user="alice")
    claims = auth_internal.require_internal_token(f"Bearer {token}")
    assert claims.request_id == "req-int-1"
    assert claims.user == "alice"
    assert claims.purpose == "openclaw_agent_mgmt"


def test_require_internal_caller_returns_verified_claims(fixed_internal_secret) -> None:
    token = it.mint_token(request_id="req-int-2", user="bob")
    claims = auth_internal.require_internal_token(f"Bearer {token}")
    merged = auth_internal.require_internal_caller(None, claims)
    assert merged is claims
