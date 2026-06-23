"""Tests for shared chat connection retry helpers."""

from __future__ import annotations

from app.services.chat_retry import is_transient_connection_error


def test_is_transient_connection_error_dns() -> None:
    assert is_transient_connection_error("[Errno -2] Name or service not known")
    assert is_transient_connection_error("Connection refused")


def test_is_transient_connection_error_not_api_error() -> None:
    assert not is_transient_connection_error("Bad Request: invalid model")
    assert not is_transient_connection_error("")
