"""Tests for the public /api loopback + bearer-token guard (:mod:`app.api._api_guard`).

The guard is a no-op unless ``Config.api_token`` is set, so the rest of the
suite (which never sets one) is unaffected — `test_guard_noop_when_token_unset`
pins that contract.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient

from app.api._api_guard import _is_guarded_path, evaluate  # noqa: PLC2701
from app.config import load_config, save_config
from app.main import create_app

_TOKEN = "test-secret-token-abc123"


def _set_token(token: str | None) -> None:
    cfg = load_config()
    save_config(cfg.model_copy(update={"api_token": token}))


@pytest.fixture(autouse=True)
def _restore_api_token_after_guard_tests() -> Iterator[None]:
    original = load_config(force_reload=True).api_token
    yield
    _set_token(original)


def _client(base_url: str = "http://127.0.0.1:17017") -> Iterator[TestClient]:
    with TestClient(create_app(), base_url=base_url) as c:
        yield c


# ── path scoping ───────────────────────────────────────────────────────


def test_guarded_path_scope() -> None:
    assert _is_guarded_path("/api/flows") is True
    assert _is_guarded_path("/api/runs/r1") is True
    # Internal API keeps its own minted-token auth — guard must skip it.
    assert _is_guarded_path("/api/internal/task-decompose/commit") is False
    # Non-/api paths (SPA, health, websocket) are never gated here.
    assert _is_guarded_path("/health") is False
    assert _is_guarded_path("/version") is False
    assert _is_guarded_path("/ws/run123") is False
    assert _is_guarded_path("/") is False


# ── no-op when token unset (protects every other test) ─────────────────


def test_guard_noop_when_token_unset() -> None:
    # No api_token configured → guard inactive; works even off-loopback Host.
    with TestClient(create_app(), base_url="http://testserver") as c:
        assert c.get("/api/flows").status_code == 200


# ── enforcement when token set ─────────────────────────────────────────


def test_guard_blocks_without_token() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows")
        assert r.status_code == 401
        assert r.json()["error"] == "UNAUTHENTICATED"


def test_guard_allows_bearer_token() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert r.status_code == 200


def test_guard_allows_x_api_key() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"X-API-Key": _TOKEN})
        assert r.status_code == 200


def test_guard_rejects_wrong_token() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"Authorization": "Bearer nope"})
        assert r.status_code == 401


def test_guard_allows_same_origin_browser() -> None:
    """The bundled SPA (same-origin) works without embedding the token."""
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"Sec-Fetch-Site": "same-origin"})
        assert r.status_code == 200


def test_guard_allows_origin_loopback_fallback() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"Origin": "http://127.0.0.1:17017"})
        assert r.status_code == 200


def test_guard_blocks_cross_site_browser() -> None:
    _set_token(_TOKEN)
    for c in _client():
        r = c.get("/api/flows", headers={"Sec-Fetch-Site": "cross-site"})
        assert r.status_code == 401


def test_guard_rejects_non_loopback_host() -> None:
    """Anti DNS-rebinding: a non-loopback Host is rejected even with a token."""
    _set_token(_TOKEN)
    for c in _client(base_url="http://evil.test"):
        r = c.get("/api/flows", headers={"Authorization": f"Bearer {_TOKEN}"})
        assert r.status_code == 403
        assert r.json()["error"] == "HOST_NOT_ALLOWED"


def test_guard_ignores_non_api_paths() -> None:
    _set_token(_TOKEN)
    for c in _client():
        assert c.get("/health").status_code == 200
        assert c.get("/version").status_code == 200


# ── evaluate() unit edges ──────────────────────────────────────────────


class _FakeReq:
    def __init__(self, headers: dict[str, str]) -> None:
        self.headers = {k.lower(): v for k, v in headers.items()}


def test_evaluate_ipv6_loopback_host() -> None:
    req = _FakeReq({"host": "[::1]:17017", "authorization": f"Bearer {_TOKEN}"})
    assert evaluate(req, _TOKEN) is None  # type: ignore[arg-type]


def test_evaluate_missing_host_allows_with_token() -> None:
    req = _FakeReq({"authorization": f"Bearer {_TOKEN}"})
    assert evaluate(req, _TOKEN) is None  # type: ignore[arg-type]


# ── Law 1: remote source IPs may reach ONLY /api/external/* ───────────


def _remote_client_app(client_host: str = "203.0.113.9"):
    """Wrap the app so every connection appears to come from *client_host*."""
    inner = create_app()

    async def wrapped(scope, receive, send):
        scope = dict(scope)
        scope["client"] = (client_host, 55555)
        await inner(scope, receive, send)

    return wrapped


def test_remote_client_blocked_from_main_api() -> None:
    with TestClient(_remote_client_app(), base_url="http://127.0.0.1:17017") as c:
        r = c.get("/api/flows")
        assert r.status_code == 403
        assert r.json()["error"] == "REMOTE_NOT_ALLOWED"


def test_remote_client_cannot_forge_same_origin_headers() -> None:
    """A remote socket + forged loopback Host + Sec-Fetch-Site must NOT pass."""
    _set_token(_TOKEN)
    with TestClient(_remote_client_app(), base_url="http://127.0.0.1:17017") as c:
        r = c.get(
            "/api/flows",
            headers={"Host": "127.0.0.1", "Sec-Fetch-Site": "same-origin"},
        )
        assert r.status_code == 403
        assert r.json()["error"] == "REMOTE_NOT_ALLOWED"


def test_remote_client_blocked_from_spa_and_health() -> None:
    with TestClient(_remote_client_app(), base_url="http://127.0.0.1:17017") as c:
        assert c.get("/health").status_code == 403
        assert c.get("/").status_code == 403


def test_remote_client_allowed_on_external_prefix() -> None:
    # Reaches the endpoint; rejected only by the endpoint's own ticket auth.
    with TestClient(_remote_client_app(), base_url="http://203.0.113.9:17017") as c:
        r = c.post(
            "/api/external/tasks/r1/t1/complete",
            json={"status": "success", "summary": "x", "token": "a.b"},
        )
        assert r.status_code == 401
        assert r.json()["error"] == "EXTERNAL_TICKET_INVALID"


def test_remote_client_blocked_from_websocket() -> None:
    import pytest as _pytest
    from starlette.websockets import WebSocketDisconnect

    with TestClient(_remote_client_app(), base_url="http://127.0.0.1:17017") as c:
        with _pytest.raises(WebSocketDisconnect):
            with c.websocket_connect("/ws/run-nonexistent"):
                pass  # pragma: no cover - handshake must be rejected


def test_loopback_client_still_reaches_websocket_route() -> None:
    """Loopback clients pass Law 1 and reach the route's own logic (4404)."""
    from starlette.websockets import WebSocketDisconnect

    with TestClient(create_app(), base_url="http://127.0.0.1:17017") as c:
        try:
            with c.websocket_connect("/ws/run-nonexistent"):
                pass
        except WebSocketDisconnect as exc:
            assert exc.code == 4404
