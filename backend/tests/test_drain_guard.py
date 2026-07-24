"""Tests for :mod:`app.api._drain_guard` (mutating /api blocked while draining)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.scheduler.engine import get_scheduler, reset_scheduler


@pytest.fixture
def client():
    reset_scheduler()
    with TestClient(create_app()) as c:
        yield c
    # Leave a clean scheduler for sibling tests.
    get_scheduler()._draining = False


def test_mutating_api_rejected_while_draining(client: TestClient) -> None:
    sched = get_scheduler()
    sched._draining = True
    try:
        r = client.post("/api/flows", json={"name": "x", "description": "", "spec": {
            "agents": [], "tasks": [],
        }})
        assert r.status_code == 503
        assert r.json()["error"] == "SERVICE_DRAINING"
    finally:
        sched._draining = False


def test_get_api_allowed_while_draining(client: TestClient) -> None:
    sched = get_scheduler()
    sched._draining = True
    try:
        r = client.get("/api/flows")
        assert r.status_code == 200
        assert client.get("/health").json()["draining"] is True
    finally:
        sched._draining = False


def test_external_api_not_blocked_by_drain_guard(client: TestClient) -> None:
    """Remote receipts must still land mid-drain — only the path prefix is
    exempted here (auth failure is fine; 503 SERVICE_DRAINING is not)."""
    sched = get_scheduler()
    sched._draining = True
    try:
        r = client.post(
            "/api/external/delegate",
            json={},
            headers={"Host": "127.0.0.1:17017"},
        )
        # Anything but the drain guard's 503 — missing creds / validation, etc.
        assert r.status_code != 503 or r.json().get("error") != "SERVICE_DRAINING"
    finally:
        sched._draining = False


def test_mutating_api_ok_when_not_draining(client: TestClient) -> None:
    assert get_scheduler().is_draining() is False
    # Invalid body → validation error, not 503.
    r = client.post("/api/flows", json={})
    assert r.status_code != 503
