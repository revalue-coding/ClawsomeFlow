"""Tests for :mod:`app.models` (Pydantic field-level validation)."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.models import (
    AgentKind,
    FlowAgent,
    FlowSpec,
    FlowTask,
    MergeStrategy,
)


class TestFlowAgent:
    def test_minimal_tui_agent(self) -> None:
        a = FlowAgent(id="alice", kind=AgentKind.claude, repo="/tmp/r", is_leader=False)
        assert a.merge_strategy == MergeStrategy.manual  # TUI default
        assert a.on_failure.value == "retry"

    def test_minimal_openclaw_agent(self) -> None:
        a = FlowAgent(id="oc1", kind=AgentKind.openclaw, is_leader=False)
        # OpenClaw default is agent_self
        assert a.merge_strategy == MergeStrategy.agent_self
        assert a.repo is None

    def test_openclaw_with_repo_rejected(self) -> None:
        with pytest.raises(ValidationError, match="kind=openclaw must NOT set 'repo'"):
            FlowAgent(id="oc1", kind=AgentKind.openclaw, repo="/x")

    def test_custom_requires_command(self) -> None:
        with pytest.raises(ValidationError, match="kind=custom requires"):
            FlowAgent(id="x", kind=AgentKind.custom, repo="/r")

    def test_invalid_id_charset(self) -> None:
        with pytest.raises(ValidationError, match="agent id must be"):
            FlowAgent(id="alice/bob", kind=AgentKind.claude, repo="/r")

    def test_tui_cannot_use_agent_self(self) -> None:
        with pytest.raises(ValidationError, match="cannot use merge_strategy='agent_self'"):
            FlowAgent(
                id="alice", kind=AgentKind.claude, repo="/r",
                merge_strategy=MergeStrategy.agent_self,
            )

    def test_openclaw_cannot_use_manual(self) -> None:
        with pytest.raises(ValidationError, match="OpenClaw agent cannot use"):
            FlowAgent(
                id="oc1", kind=AgentKind.openclaw,
                merge_strategy=MergeStrategy.manual,
            )

    def test_explicit_merge_strategy_kept(self) -> None:
        a = FlowAgent(
            id="alice", kind=AgentKind.claude, repo="/r",
            merge_strategy=MergeStrategy.auto,
        )
        assert a.merge_strategy == MergeStrategy.auto


class TestFlowTask:
    def test_minimal(self) -> None:
        t = FlowTask(id="t1", owner_agent_id="alice", subject="do x")
        assert t.timeout_seconds == 14400
        assert t.depends_on == []

    def test_invalid_id(self) -> None:
        with pytest.raises(ValidationError, match="task id must be"):
            FlowTask(id="../escape", owner_agent_id="alice", subject="x")

    def test_negative_timeout(self) -> None:
        with pytest.raises(ValidationError, match="timeout_seconds must be positive"):
            FlowTask(id="t1", owner_agent_id="alice", subject="x", timeout_seconds=0)

    def test_summary_task_cannot_enable_human_checkpoint(self) -> None:
        with pytest.raises(
            ValidationError,
            match="leader summary task cannot enable requires_human_checkpoint",
        ):
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                is_leader_summary=True,
                requires_human_checkpoint=True,
            )


class TestFlowSpecRoundTrip:
    def test_serialise_camelcase(self) -> None:
        spec = FlowSpec(
            agents=[FlowAgent(id="alice", kind=AgentKind.claude, repo="/r", is_leader=True)],
            tasks=[FlowTask(id="t1", owner_agent_id="alice", subject="x", is_leader_summary=True)],
        )
        dumped = spec.model_dump(mode="json", by_alias=True)
        assert dumped["agents"][0]["isLeader"] is True
        assert dumped["tasks"][0]["isLeaderSummary"] is True

    def test_parse_either_alias_or_snake(self) -> None:
        # JSON in camelCase (front-end style)
        camel = {
            "agents": [{
                "id": "a", "kind": "claude", "repo": "/r", "isLeader": True,
                "ownFailure": "retry",  # arbitrary extra; should be ignored / not parsed
            }],
            "tasks": [{
                "id": "t1", "ownerAgentId": "a", "subject": "x",
                "isLeaderSummary": True, "timeoutSeconds": 60,
            }],
        }
        spec = FlowSpec.model_validate(camel)
        assert spec.tasks[0].owner_agent_id == "a"
        assert spec.tasks[0].timeout_seconds == 60
