"""Tests for app.flow_modes — Flow execution-mode resolution."""

from __future__ import annotations

import pytest

from app.flow_modes import (
    FLOW_DEV_MODE_KEY,
    FLOW_EASY_MODE_KEY,
    flow_mode,
    task_self_merges,
)
from app.models import AgentKind, FlowAgent, FlowTask, MergeStrategy, OnFailure


def _agent(kind=AgentKind.claude) -> FlowAgent:
    return FlowAgent(
        id="a1",
        kind=kind,
        repo=None if kind == AgentKind.openclaw else "/tmp/main",
        is_leader=False,
        merge_strategy=(
            MergeStrategy.agent_self if kind == AgentKind.openclaw else MergeStrategy.manual
        ),
        on_failure=OnFailure.retry,
        max_retries=1,
    )


def _task(*, auto_merge=True, is_summary=False) -> FlowTask:
    return FlowTask(
        id="t1",
        owner_agent_id="a1",
        subject="do",
        description="d",
        dev_auto_merge=auto_merge,
        is_leader_summary=is_summary,
    )


# ── flow_mode ──────────────────────────────────────────────────────────


def test_flow_mode_normal_when_no_flags() -> None:
    assert flow_mode({}) == "normal"
    assert flow_mode(None) == "normal"
    assert flow_mode({FLOW_EASY_MODE_KEY: "false"}) == "normal"


def test_flow_mode_easy() -> None:
    assert flow_mode({FLOW_EASY_MODE_KEY: "true"}) == "easy"
    assert flow_mode({FLOW_EASY_MODE_KEY: "TRUE"}) == "easy"


def test_flow_mode_dev() -> None:
    assert flow_mode({FLOW_DEV_MODE_KEY: "true"}) == "dev"


def test_flow_mode_dev_wins_over_easy() -> None:
    assert (
        flow_mode({FLOW_DEV_MODE_KEY: "true", FLOW_EASY_MODE_KEY: "true"}) == "dev"
    )


# ── task_self_merges ────────────────────────────────────────────────────


@pytest.mark.parametrize("scheduled", [False, True])
def test_easy_mode_always_self_merges(scheduled: bool) -> None:
    assert task_self_merges(
        mode="easy", run_is_scheduled=scheduled,
        task=_task(), agent=_agent(),
    ) is True


def test_normal_mode_manual_does_not_self_merge() -> None:
    assert task_self_merges(
        mode="normal", run_is_scheduled=False,
        task=_task(), agent=_agent(),
    ) is False


def test_normal_mode_scheduled_self_merges() -> None:
    assert task_self_merges(
        mode="normal", run_is_scheduled=True,
        task=_task(), agent=_agent(),
    ) is True


@pytest.mark.parametrize("scheduled", [False, True])
def test_dev_mode_auto_merge_task_self_merges(scheduled: bool) -> None:
    assert task_self_merges(
        mode="dev", run_is_scheduled=scheduled,
        task=_task(auto_merge=True), agent=_agent(),
    ) is True


@pytest.mark.parametrize("scheduled", [False, True])
def test_dev_mode_no_merge_task_does_not_self_merge(scheduled: bool) -> None:
    assert task_self_merges(
        mode="dev", run_is_scheduled=scheduled,
        task=_task(auto_merge=False), agent=_agent(),
    ) is False


def test_dev_mode_openclaw_forced_to_self_merge_even_when_disabled() -> None:
    # OpenClaw is forced to self-merge regardless of dev_auto_merge.
    assert task_self_merges(
        mode="dev", run_is_scheduled=False,
        task=_task(auto_merge=False), agent=_agent(kind=AgentKind.openclaw),
    ) is True


def test_dev_mode_summary_task_honours_flag() -> None:
    # The summary task participates in the per-task switch too.
    assert task_self_merges(
        mode="dev", run_is_scheduled=False,
        task=_task(auto_merge=False, is_summary=True), agent=_agent(),
    ) is False
