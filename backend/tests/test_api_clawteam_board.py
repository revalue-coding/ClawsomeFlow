"""Tests for same-origin ClawTeam board proxy routes."""

from __future__ import annotations

import pytest
from fastapi.responses import PlainTextResponse
from fastapi.testclient import TestClient

from app.main import create_app


@pytest.fixture
def app_client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def test_board_page_route_forwards_root_and_rewrites_html(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_proxy(*, request, upstream_path: str, rewrite_html: bool, api_prefix: str = ""):
        captured["upstream_path"] = upstream_path
        captured["rewrite_html"] = rewrite_html
        captured["query"] = request.url.query
        captured["api_prefix"] = api_prefix
        return PlainTextResponse("ok")

    monkeypatch.setattr("app.api.clawteam_board._proxy", fake_proxy)
    resp = app_client.get("/clawteam-board-proxy/?team=csflow-demo")
    assert resp.status_code == 200
    assert captured["upstream_path"] == "/"
    assert captured["rewrite_html"] is True
    assert captured["query"] == "team=csflow-demo"
    assert captured["api_prefix"] == "/clawteam-board-proxy-api"


def test_board_api_route_forwards_prefixed_api_path(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_proxy(*, request, upstream_path: str, rewrite_html: bool, api_prefix: str = ""):
        captured["upstream_path"] = upstream_path
        captured["rewrite_html"] = rewrite_html
        captured["method"] = request.method
        captured["api_prefix"] = api_prefix
        return PlainTextResponse("ok")

    monkeypatch.setattr("app.api.clawteam_board._proxy", fake_proxy)
    resp = app_client.get("/clawteam-board-proxy-api/events/csflow-demo")
    assert resp.status_code == 200
    assert captured["upstream_path"] == "/api/events/csflow-demo"
    assert captured["rewrite_html"] is False
    assert captured["method"] == "GET"
    assert captured["api_prefix"] == ""


def test_rewrite_html_api_calls() -> None:
    from app.api.clawteam_board import _rewrite_board_html

    html = (
        "<script>"
        "fetch('/api/overview');"
        "fetch(\"/api/team/demo\");"
        "const url = `/api/events/demo`;"
        "</script>"
    )
    out = _rewrite_board_html(html, api_prefix="/clawteam-board-proxy-api")
    assert "/clawteam-board-proxy-api/overview" in out
    assert "/clawteam-board-proxy-api/team/demo" in out
    assert "/clawteam-board-proxy-api/events/demo" in out


def test_board_proxy_ws_route_forwards_ws_path(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_proxy_ws(*, websocket, upstream_path: str) -> None:
        captured["upstream_path"] = upstream_path
        captured["query"] = websocket.url.query
        await websocket.accept()
        await websocket.close()

    monkeypatch.setattr("app.api.clawteam_board._proxy_ws", fake_proxy_ws)
    with app_client.websocket_connect("/clawteam-board-proxy-api/ws?team=csflow-demo"):
        pass
    assert captured["upstream_path"] == "/api/ws"
    assert captured["query"] == "team=csflow-demo"


def test_board_proxy_ws_route_forwards_nested_ws_path(
    app_client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    async def fake_proxy_ws(*, websocket, upstream_path: str) -> None:
        captured["upstream_path"] = upstream_path
        await websocket.accept()
        await websocket.close()

    monkeypatch.setattr("app.api.clawteam_board._proxy_ws", fake_proxy_ws)
    with app_client.websocket_connect("/clawteam-board-proxy-api/ws/stream/team"):
        pass
    assert captured["upstream_path"] == "/api/ws/stream/team"

