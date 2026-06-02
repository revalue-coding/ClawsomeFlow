"""Tests for :mod:`app.logging_setup`."""

from __future__ import annotations

import json

from app import logging_setup as ls


def _read_log_lines() -> list[dict]:
    p = ls._get_log_path_for_today()
    if not p.exists():
        return []
    return [json.loads(line) for line in p.read_text(encoding="utf-8").splitlines() if line.strip()]


def test_configure_is_idempotent() -> None:
    ls.configure_logging()
    ls.configure_logging()
    log = ls.get_logger("t")
    log.info("hello")
    lines = _read_log_lines()
    assert any(l.get("event") == "hello" for l in lines)


def test_bind_context_propagates() -> None:
    ls.configure_logging()
    log = ls.get_logger("t")
    with ls.bind_context(run_id="run-x", agent_id="alice"):
        log.info("contextual")
    lines = _read_log_lines()
    matched = [l for l in lines if l.get("event") == "contextual"]
    assert matched, "contextual log not found"
    assert matched[-1]["run_id"] == "run-x"
    assert matched[-1]["agent_id"] == "alice"


def test_spawn_cmd_built_event_shape() -> None:
    ls.configure_logging()
    ls.spawn_cmd_built(
        cmd_argv=["clawteam", "spawn", "tmux", "claude"],
        workspace=True,
        repo="/tmp/repo",
        keepalive=False,
        has_task=False,
        has_skill=False,
    )
    lines = _read_log_lines()
    event = next((l for l in lines if l.get("event") == "spawn_cmd_built"), None)
    assert event is not None
    assert event["cmd_argv"] == ["clawteam", "spawn", "tmux", "claude"]
    assert event["workspace"] is True
    assert event["repo"] == "/tmp/repo"
    assert event["keepalive"] is False
    assert event["has_task"] is False
    assert event["has_skill"] is False


def test_lock_acquired_warns_on_long_wait() -> None:
    ls.configure_logging()
    ls.lock_acquired(key="k", wait_ms=2500)
    lines = _read_log_lines()
    matched = [l for l in lines if l.get("event") == "lock_acquired" and l.get("wait_ms") == 2500]
    assert matched
    assert matched[-1]["level"] == "warning"


def test_workspace_violation_emits_warning() -> None:
    ls.configure_logging()
    ls.workspace_violation(
        agent_id="alice",
        task_id="t1",
        dirty_files="path/to/file\n",
    )
    lines = _read_log_lines()
    event = next((l for l in lines if l.get("event") == "workspace_violation"), None)
    assert event is not None
    assert event["level"] == "warning"
    assert event["agent_id"] == "alice"
