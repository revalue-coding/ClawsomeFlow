"""Tests for app.integrations.clawteam_spawn_config."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.integrations import clawteam_spawn_config as csc


def test_ensure_spawn_ready_timeout_creates_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "config.json"
    monkeypatch.setattr(csc, "clawteam_config_path", lambda: cfg)

    changed = csc.ensure_spawn_ready_timeout(seconds=2.0)

    assert changed is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["spawn_ready_timeout"] == 2.0


def test_ensure_spawn_ready_timeout_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(json.dumps({"spawn_ready_timeout": 2.0}) + "\n", encoding="utf-8")
    monkeypatch.setattr(csc, "clawteam_config_path", lambda: cfg)

    assert csc.ensure_spawn_ready_timeout(seconds=2.0) is False


def test_ensure_spawn_ready_timeout_overwrites_stock_30(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text(
        json.dumps({"spawn_ready_timeout": 30.0, "skip_permissions": True}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(csc, "clawteam_config_path", lambda: cfg)

    assert csc.ensure_spawn_ready_timeout(seconds=2.0) is True
    data = json.loads(cfg.read_text(encoding="utf-8"))
    assert data["spawn_ready_timeout"] == 2.0
    assert data["skip_permissions"] is True


def test_ensure_spawn_ready_timeout_never_raises_on_bad_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    cfg = tmp_path / "config.json"
    cfg.write_text("{not json", encoding="utf-8")
    monkeypatch.setattr(csc, "clawteam_config_path", lambda: cfg)

    assert csc.ensure_spawn_ready_timeout(seconds=2.0) is False
