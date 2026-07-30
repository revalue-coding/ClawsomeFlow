"""Unit tests for the remote-node parameter hand-off parsers (controller)."""

from __future__ import annotations

import pytest

from app.models import (
    AgentKind,
    ExternalChannel,
    ExternalNodeConfig,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    RunStatus,
)
from app.scheduler.controller import (
    RunController,
    _declared_flow_param_fields,
    _extract_first_json_object,
    _extract_remote_params_block,
)
from app.scheduler.prompts import EMPTY_PARAM_PLACEHOLDER, REMOTE_PARAMS_HEADER


def test_extract_first_json_object_plain() -> None:
    assert _extract_first_json_object('{"a": "1", "b": "2"}') == {"a": "1", "b": "2"}


def test_extract_first_json_object_with_prose_and_fences() -> None:
    text = 'Sure, here it is:\n```json\n{"x": "hello"}\n```\nthanks'
    assert _extract_first_json_object(text) == {"x": "hello"}


def test_extract_first_json_object_nested_and_braces_in_strings() -> None:
    text = 'noise {"outer": {"inner": "a}b"}} trailing'
    assert _extract_first_json_object(text) == {"outer": {"inner": "a}b"}}


def test_extract_first_json_object_none_when_absent() -> None:
    assert _extract_first_json_object("no json here") is None
    assert _extract_first_json_object("") is None


def test_extract_remote_params_block_matches_header_and_task_id() -> None:
    text = (
        f"{REMOTE_PARAMS_HEADER}: t-upstream\n"
        '{"需求描述": "抓取周报", "目标目录": ""}'
    )
    parsed = _extract_remote_params_block(text, "t-upstream")
    assert parsed == {"需求描述": "抓取周报", "目标目录": ""}


def test_extract_remote_params_block_prefers_per_downstream_header() -> None:
    text = (
        f"{REMOTE_PARAMS_HEADER}: t-up remote-a\n"
        '{"fa": "1"}\n'
        f"{REMOTE_PARAMS_HEADER}: t-up remote-b\n"
        '{"fb": "2"}'
    )
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-a",
    ) == {"fa": "1"}
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-b",
    ) == {"fb": "2"}


def test_extract_remote_params_block_legacy_fallback_for_downstream() -> None:
    """Old single-block form still works when asking for a specific downstream."""
    text = f'{REMOTE_PARAMS_HEADER}: t-up\n{{"shared": "v"}}'
    assert _extract_remote_params_block(
        text, "t-up", downstream_task_id="remote-a",
    ) == {"shared": "v"}


def test_extract_remote_params_block_ignores_other_task_id() -> None:
    text = f"{REMOTE_PARAMS_HEADER}: other\n{{\"a\": \"1\"}}"
    assert _extract_remote_params_block(text, "t-upstream") is None


def test_extract_remote_params_block_absent_returns_none() -> None:
    assert _extract_remote_params_block("task t1 done: did the thing", "t1") is None


def test_extract_remote_params_block_present_but_bad_json_returns_empty() -> None:
    text = f"{REMOTE_PARAMS_HEADER}: t1 (no json object follows)"
    assert _extract_remote_params_block(text, "t1") == {}


def test_extract_remote_params_block_null_values_become_empty_string() -> None:
    text = f'{REMOTE_PARAMS_HEADER}: t1\n{{"a": null, "b": "v"}}'
    assert _extract_remote_params_block(text, "t1") == {"a": "", "b": "v"}


def _spec_with_remote_params(*, root: bool) -> FlowSpec:
    external = FlowAgent(
        id="ext",
        kind=AgentKind.external,
        external=ExternalNodeConfig(
            channel=ExternalChannel.remote_csflow,
            base_url="http://peer:17017",
            flow_id="flow-remote",
            pair_token_ref="peer",
            inputs={"需求描述": "手工固定值"},
            input_param_refs={"需求描述": "本地需求"},
            remote_param_fields=["需求描述", "目标目录"],
        ),
    )
    leader = FlowAgent(
        id="leader",
        kind=AgentKind.claude,
        repo="/tmp/main",
        is_leader=True,
        merge_strategy=MergeStrategy.manual,
    )
    tasks: list[FlowTask] = []
    if not root:
        tasks.append(
            FlowTask(id="t0", owner_agent_id="worker", subject="upstream", description="d")
        )
    tasks.extend(
        [
            FlowTask(
                id="t1",
                owner_agent_id="ext",
                subject="remote",
                description="d",
                depends_on=[] if root else ["t0"],
            ),
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=["t1"],
                is_leader_summary=True,
            ),
        ]
    )
    agents = [external, leader]
    if not root:
        agents.insert(
            0,
            FlowAgent(
                id="worker",
                kind=AgentKind.claude,
                repo="/tmp/main",
                merge_strategy=MergeStrategy.manual,
            ),
        )
    return FlowSpec(
        agents=agents,
        tasks=tasks,
        variables={"csflow.runtime.param_fields": '["本地需求", "本地目录"]'},
    )


def _controller(spec: FlowSpec, inputs: dict[str, object]) -> RunController:
    run = FlowRun(
        id="run-remote-params",
        flow_id="flow-test",
        flow_version=1,
        team_name="csflow-test",
        status=RunStatus.running,
        inputs=inputs,
        user="alice",
    )

    class _Lookup:
        async def list_team(self, team, *, repo=None, force=False):
            return []

        async def get(self, team, agent_name, *, repo=None, force=False):
            raise AssertionError("resolver must not touch worktrees")

    async def inbox_provider():
        return []

    return RunController(
        run=run,
        spec=spec,
        flow_description="goal",
        worktree_lookup=_Lookup(),  # type: ignore[arg-type]
        leader_inbox_provider=inbox_provider,
    )


@pytest.mark.asyncio
async def test_remote_delegate_inputs_pass_through_current_flow_param_root() -> None:
    spec = _spec_with_remote_params(root=True)
    rc = _controller(spec, {"本地需求": "运行时填写的需求", "本地目录": "/tmp/out"})

    got = await rc._resolve_remote_delegate_inputs(spec.agents[0], spec.tasks[0])

    # The explicit field binding wins over the literal override; unfilled
    # declared remote fields keep the existing placeholder behaviour.
    assert got == {"需求描述": "运行时填写的需求", "目标目录": EMPTY_PARAM_PLACEHOLDER}


@pytest.mark.asyncio
async def test_remote_delegate_inputs_pass_through_current_flow_param_downstream() -> None:
    spec = _spec_with_remote_params(root=False)
    rc = _controller(spec, {"本地需求": "运行时填写的需求"})

    got = await rc._resolve_remote_delegate_inputs(spec.agents[1], spec.tasks[1])

    assert got["需求描述"] == "运行时填写的需求"
    assert got["目标目录"] == EMPTY_PARAM_PLACEHOLDER


@pytest.mark.asyncio
async def test_remote_delegate_input_refs_are_limited_to_declared_flow_params() -> None:
    spec = _spec_with_remote_params(root=True)
    ext = spec.agents[0].external
    assert ext is not None
    ext.input_param_refs = {"需求描述": "_csflow_unattended"}
    rc = _controller(spec, {"_csflow_unattended": "true"})

    got = await rc._resolve_remote_delegate_inputs(spec.agents[0], spec.tasks[0])

    # Internal scheduler keys are not valid passthrough targets; the literal
    # value remains, and no internal marker leaks into the delegate payload.
    assert got["需求描述"] == "手工固定值"


def test_declared_flow_param_fields_legacy_fallbacks() -> None:
    spec = FlowSpec(
        agents=[
            FlowAgent(
                id="leader",
                kind=AgentKind.claude,
                repo="/tmp/main",
                is_leader=True,
                merge_strategy=MergeStrategy.manual,
            )
        ],
        tasks=[
            FlowTask(
                id="ts",
                owner_agent_id="leader",
                subject="summary",
                description="d",
                depends_on=[],
                is_leader_summary=True,
            )
        ],
        variables={"csflow.runtime.requirement": "需求描述"},
    )
    assert _declared_flow_param_fields(spec) == ["需求描述"]


def test_external_input_param_refs_camel_case_round_trip() -> None:
    cfg = ExternalNodeConfig.model_validate(
        {
            "channel": "remote_csflow",
            "baseUrl": "http://peer:17017",
            "flowId": "flow-remote",
            "pairTokenRef": "peer",
            "inputParamRefs": {"远端需求": "本地需求"},
        }
    )

    dumped = cfg.model_dump(mode="json", by_alias=True)

    assert dumped["inputParamRefs"] == {"远端需求": "本地需求"}
