"""Tests for request-level user resolution in :mod:`app.api._auth`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.api._auth import resolve_current_user
from app.api.errors import ApiError
from app.main import create_app
from app.user_context import set_request_user


def test_resolve_current_user_local_mode_uses_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(deployment_mode="local", default_user="alice"),
    )
    set_request_user(None)
    user = resolve_current_user()
    assert user == "alice"


def test_resolve_current_user_server_mode_requires_request_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_request_user(None)
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(deployment_mode="server", default_user="alice"),
    )
    with pytest.raises(ApiError) as exc:
        resolve_current_user()
    assert exc.value.code == "UNAUTHENTICATED"
    assert exc.value.status_code == 401


def test_server_mode_accepts_x_csflow_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_request_user(None)
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(deployment_mode="server", default_user="alice"),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/flows", headers={"X-CSFLOW-User": "bob"})
    assert r.status_code == 200, r.text


def test_server_mode_rejects_invalid_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_request_user(None)
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(deployment_mode="server", default_user="alice"),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/flows", headers={"X-CSFLOW-User": "bad user"})
    assert r.status_code == 401
    assert r.json()["error"] == "UNAUTHENTICATED"


def test_server_mode_accepts_x_forwarded_user_header(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    set_request_user(None)
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(deployment_mode="server", default_user="alice"),
    )
    with TestClient(create_app()) as client:
        r = client.get("/api/flows", headers={"X-Forwarded-User": "bob"})
    assert r.status_code == 200, r.text
