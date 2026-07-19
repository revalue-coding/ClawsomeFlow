"""Tests for request-level user resolution in :mod:`app.api._auth`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.api._auth import resolve_current_user
from app.api.errors import ApiError
from app.user_context import set_request_user


def test_resolve_current_user_uses_default_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("CSFLOW_USER", raising=False)
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(default_user="alice"),
    )
    set_request_user(None)
    user = resolve_current_user()
    assert user == "alice"


def test_resolve_current_user_env_override_wins(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSFLOW_USER", "bob")
    monkeypatch.setattr(
        "app.api._auth.load_config",
        lambda: SimpleNamespace(default_user="alice"),
    )
    set_request_user(None)
    assert resolve_current_user() == "bob"


def test_resolve_current_user_rejects_invalid_identifier(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CSFLOW_USER", "bad user")
    set_request_user(None)
    with pytest.raises(ApiError) as exc:
        resolve_current_user()
    assert exc.value.code == "UNAUTHENTICATED"
    assert exc.value.status_code == 401
