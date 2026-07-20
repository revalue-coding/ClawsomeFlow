"""External execution nodes (AgentKind.external) — model / scheduler / service.

Covers:
* FlowAgent model constraints for kind=external (+ ExternalNodeConfig channels).
* validators: external agents skip repo checks; remote_csflow pair-token ref.
* One-time receipt tickets (mint / verify / cross-task rejection).
* failure.detect_failures timeout exemption for external-owned tasks.
* prompts.build_external_task_text / build_external_task_package.
* ExternalNodeSession no-op spawn + dispatch delegation.
* services.external_tasks.complete_external_task (success / failed / stale /
  idempotent) and prepare_delegate_callback.
* Config backward compatibility (new fields default safely).
"""

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path
from typing import Any

import pytest

from app.config import Config, load_config, save_config
from app.models import (
    AgentKind,
    ExternalChannel,
    ExternalNodeConfig,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    RunEvent,
    RunStatus,
)
from app.scheduler.failure import TaskSnapshot, detect_failures
from app.scheduler.prompts import (
    DispatchContext,
    UpstreamOutput,
    build_external_task_package,
    build_external_task_text,
)
from app.scheduler.run_metadata import (
    EXTERNAL_CALLBACK_KEY,
    EXTERNAL_CALLBACK_SENT_KEY,
)
from app.services import external_tasks as ext_svc
from app.services.external_tasks import (
    EXTERNAL_TASK_COMPLETED_EVENT,
    EXTERNAL_TASK_DISPATCHED_EVENT,
    ExternalTaskError,
    complete_external_task,
    mint_ticket,
    prepare_delegate_callback,
    verify_ticket,
)
from app.storage import get_storage


def _human_cfg(**kw: Any) -> ExternalNodeConfig:
    return ExternalNodeConfig(channel=ExternalChannel.human, **kw)


# ── FlowAgent model constraints ─────────────────────────────────────────


def test_external_agent_defaults() -> None:
    a = FlowAgent(id="alice-human", kind=AgentKind.external, external=_human_cfg())
    assert a.merge_strategy == MergeStrategy.skip
    assert a.target_branch is None
    assert a.repo is None
    assert a.is_temporary is False


def test_external_agent_requires_config() -> None:
    with pytest.raises(ValueError, match="external"):
        FlowAgent(id="x", kind=AgentKind.external)


def test_external_agent_cannot_lead() -> None:
    with pytest.raises(ValueError, match="leader"):
        FlowAgent(id="x", kind=AgentKind.external, is_leader=True, external=_human_cfg())


def test_external_agent_rejects_repo_and_branch() -> None:
    with pytest.raises(ValueError, match="repo"):
        FlowAgent(id="x", kind=AgentKind.external, repo="/tmp/r", external=_human_cfg())
    with pytest.raises(ValueError, match="target_branch"):
        FlowAgent(
            id="x", kind=AgentKind.external, target_branch="main",
            external=_human_cfg(),
        )


def test_external_agent_merge_strategy_must_be_skip() -> None:
    with pytest.raises(ValueError, match="skip"):
        FlowAgent(
            id="x", kind=AgentKind.external,
            merge_strategy=MergeStrategy.manual, external=_human_cfg(),
        )


def test_non_external_agent_rejects_external_config() -> None:
    with pytest.raises(ValueError, match="only allowed"):
        FlowAgent(id="x", kind=AgentKind.claude, repo="/tmp/r", external=_human_cfg())


def test_external_channel_required_fields() -> None:
    with pytest.raises(ValueError, match="endpoint_url"):
        ExternalNodeConfig(channel=ExternalChannel.webhook)
    with pytest.raises(ValueError, match="base_url"):
        ExternalNodeConfig(channel=ExternalChannel.remote_csflow)
    ok = ExternalNodeConfig(
        channel=ExternalChannel.remote_csflow,
        base_url="http://remote:17017", flow_id="flow-1", pair_token_ref="m1",
    )
    assert ok.flow_id == "flow-1"


def test_task_timeout_zero_allowed() -> None:
    t = FlowTask(id="t1", owner_agent_id="a", subject="s", timeout_seconds=0)
    assert t.timeout_seconds == 0
    with pytest.raises(ValueError):
        FlowTask(id="t1", owner_agent_id="a", subject="s", timeout_seconds=-1)


# ── validators ──────────────────────────────────────────────────────────


def _git_repo(tmp_path: Path) -> str:
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-b", "main"], cwd=repo, check=True,
                   capture_output=True)
    subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t",
                    "commit", "--allow-empty", "-m", "init"],
                   cwd=repo, check=True, capture_output=True)
    return str(repo)


def _spec_with_external(repo: str, external: ExternalNodeConfig) -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="leader", kind=AgentKind.claude, repo=repo, is_leader=True),
            FlowAgent(id="ext-node", kind=AgentKind.external, external=external),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="ext-node", subject="external work"),
            FlowTask(id="ts", owner_agent_id="leader", subject="summary",
                     depends_on=["t1"], is_leader_summary=True),
        ],
    )


def test_validate_against_db_skips_repo_for_external(tmp_path: Path) -> None:
    from app.validators.flow import validate_flow_against_db

    spec = _spec_with_external(_git_repo(tmp_path), _human_cfg())
    validate_flow_against_db(spec, get_storage())  # must not raise


def test_validate_remote_csflow_requires_registered_pair_token(
    tmp_path: Path,
) -> None:
    from app.validators.flow import FlowValidationError, validate_flow_against_db

    remote = ExternalNodeConfig(
        channel=ExternalChannel.remote_csflow,
        base_url="http://remote:17017", flow_id="flow-1", pair_token_ref="m1",
    )
    spec = _spec_with_external(_git_repo(tmp_path), remote)
    with pytest.raises(FlowValidationError) as exc:
        validate_flow_against_db(spec, get_storage())
    assert exc.value.code == "EXTERNAL_PAIR_TOKEN_NOT_FOUND"

    cfg = load_config()
    save_config(cfg.model_copy(update={"external_remote_targets": {"m1": "sec"}}))
    validate_flow_against_db(spec, get_storage())  # now passes


# ── tickets ─────────────────────────────────────────────────────────────


def test_ticket_roundtrip_and_rejections() -> None:
    token = mint_ticket("run-1", "t1", "nonce-a")
    assert verify_ticket(token, run_id="run-1", task_id="t1") == "nonce-a"
    with pytest.raises(ExternalTaskError):
        verify_ticket(token, run_id="run-2", task_id="t1")
    with pytest.raises(ExternalTaskError):
        verify_ticket(token, run_id="run-1", task_id="t2")
    with pytest.raises(ExternalTaskError):
        verify_ticket("garbage", run_id="run-1", task_id="t1")


# ── failure timeout exemption ───────────────────────────────────────────


def _snap(task_id: str, dispatched_ago: float, now: float) -> TaskSnapshot:
    return TaskSnapshot(
        task_id=task_id, owner_agent_id="ext-node", status="in_progress",
        locked_by_agent=None, metadata={},
        dispatched_at_epoch=now - dispatched_ago,
    )


def test_detect_failures_external_no_floor_and_zero_disables() -> None:
    now = 1_000_000.0
    agents = {
        "ext-node": FlowAgent(
            id="ext-node", kind=AgentKind.external, external=_human_cfg(),
        ),
    }
    # timeout=0 → never times out, even after "months".
    tasks = {"t1": FlowTask(id="t1", owner_agent_id="ext-node", subject="s",
                            timeout_seconds=0)}
    out = detect_failures(
        team_name="tm", flow_tasks=tasks,
        snapshots=[_snap("t1", dispatched_ago=90 * 86400, now=now)],
        leader_agent_id="leader", now=now, agents=agents,
    )
    assert out == []
    # Explicit small timeout is honoured (no 4h floor) for external owners.
    tasks2 = {"t1": FlowTask(id="t1", owner_agent_id="ext-node", subject="s",
                             timeout_seconds=600)}
    out2 = detect_failures(
        team_name="tm", flow_tasks=tasks2,
        snapshots=[_snap("t1", dispatched_ago=700, now=now)],
        leader_agent_id="leader", now=now, agents=agents,
    )
    assert [r.task_id for r in out2] == ["t1"]
    # Same task owned by a regular agent keeps the 4h floor.
    agents_regular = {
        "ext-node": FlowAgent(id="ext-node", kind=AgentKind.claude, repo="/tmp/r"),
    }
    out3 = detect_failures(
        team_name="tm", flow_tasks=tasks2,
        snapshots=[_snap("t1", dispatched_ago=700, now=now)],
        leader_agent_id="leader", now=now, agents=agents_regular,
    )
    assert out3 == []


# ── prompts ─────────────────────────────────────────────────────────────


def _ctx() -> DispatchContext:
    agent = FlowAgent(id="ext-node", kind=AgentKind.external, external=_human_cfg())
    task = FlowTask(id="t1", owner_agent_id="ext-node", subject="Review the PCB",
                    description="Check the physical board.", depends_on=["t0"])
    return DispatchContext(
        run_id="run-1", team_name="csflow-x", flow_description="Build a robot",
        flow_inputs={"goal": "v1", "_csflow_unattended": "true"}, user="alice",
        agent=agent, task=task, leader_agent_id="leader",
        clawteam_task_id="CT-9",
        upstream_outputs=[UpstreamOutput(
            task_id="t0", subject="Design", from_agent="designer",
            worktree_path="/wt", branch_name="b", base_branch="main",
            summary="done: schematics at /wt/s.pdf",
        )],
    )


def test_build_external_task_text_has_no_clawteam_protocol() -> None:
    text = build_external_task_text(_ctx())
    assert "ClawsomeFlow External Task" in text
    assert "Review the PCB" in text
    assert "schematics at /wt/s.pdf" in text
    assert "clawteam" not in text  # no protocol steps for external executors
    assert "_csflow_unattended" not in text  # internal markers stay hidden
    # External nodes own no worktree — never inject workspace path/branch lines
    # (a completion summary may still mention paths the upstream agent wrote).
    assert "worktree:" not in text
    assert "base branch" not in text
    assert "no shared worktree path" in text.lower() or "no shared" in text.lower()
    # Human channel must NOT get the webhook-only remote-path notes.
    assert "do not open or fetch them locally" not in text


def test_build_external_task_text_zh_and_notify_brief() -> None:
    from app.scheduler.prompts import (
        build_delegate_runtime_prompt,
        build_external_notify_brief,
    )

    zh = build_external_task_text(_ctx(), lang="zh")
    assert "ClawsomeFlow 外部任务" in zh
    assert "结果提交" in zh
    assert "直接上游产出" in zh
    assert "完成摘要" in zh
    assert "Direct Upstream Outputs" not in zh
    brief = build_external_notify_brief(
        {
            "taskId": "t1",
            "subject": "行程",
            "description": "设计路线",
            "outputRequirement": "路线表",
            "flowDescription": "旅行计划",
        },
        lang="zh",
    )
    assert "设计路线" in brief
    assert "ClawsomeFlow External Task" not in brief
    assert build_delegate_runtime_prompt({
        "subject": "整合",
        "description": "汇总前序结果",
        "outputRequirement": "可执行方案",
    }).startswith("整合")


def test_build_external_task_text_webhook_includes_remote_notes() -> None:
    import dataclasses

    from app.scheduler.prompts import WEBHOOK_REMOTE_NOTES

    ctx = dataclasses.replace(
        _ctx(),
        agent=FlowAgent(
            id="ext-node", kind=AgentKind.external,
            external=ExternalNodeConfig(
                channel=ExternalChannel.webhook,
                endpoint_url="https://partner.example/tasks",
            ),
        ),
    )
    text = build_external_task_text(ctx)
    assert WEBHOOK_REMOTE_NOTES in text
    assert "Notes for remote executors" in text


def test_build_external_task_package_fields() -> None:
    pkg = build_external_task_package(_ctx())
    assert pkg["subject"] == "Review the PCB"
    assert pkg["clawteamTaskId"] == "CT-9"
    assert pkg["leaderAgentId"] == "leader"
    assert pkg["runtimeInputs"] == {"goal": "v1"}
    assert pkg["upstreamOutputs"][0]["taskId"] == "t0"
    assert "worktreePath" not in pkg["upstreamOutputs"][0]


def test_build_external_task_package_splits_output_requirement() -> None:
    """The canonical merged description is split back into the two UI fields
    so integrated systems get the briefing and the deliverable shape apart."""
    import dataclasses

    ctx = dataclasses.replace(_ctx(), task=FlowTask(
        id="t1", owner_agent_id="ext-node", subject="Review the PCB",
        description="Check the physical board.",
        output_summary_requirement="Return a pass/fail verdict per subsystem.",
    ))
    pkg = build_external_task_package(ctx)
    assert pkg["description"] == "Check the physical board."
    assert pkg["outputRequirement"] == "Return a pass/fail verdict per subsystem."
    # No requirement → explicit None (field always present for the contract).
    pkg_plain = build_external_task_package(_ctx())
    assert pkg_plain["outputRequirement"] is None


# ── ExternalNodeSession ─────────────────────────────────────────────────


def test_external_session_spawn_and_dispatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scheduler.sessions.base import SessionState
    from app.scheduler.sessions.external import ExternalNodeSession

    recorded: dict[str, Any] = {}

    async def fake_dispatch(**kw: Any) -> None:
        recorded.update(kw)

    monkeypatch.setattr(ext_svc, "dispatch_external_task", fake_dispatch)

    async def scenario() -> None:
        agent = FlowAgent(id="ext-node", kind=AgentKind.external,
                          external=_human_cfg())

        async def package_provider(task_id: str) -> dict[str, Any]:
            return {"subject": "s", "clawteamTaskId": "CT-1"}

        sess = ExternalNodeSession(
            agent=agent, team_name="tm", run_id="run-1",
            storage=get_storage(), package_provider=package_provider,
        )
        assert sess.state == SessionState.Absent
        await sess.spawn()  # no-op → Idle instantly, no tmux
        assert sess.state == SessionState.Idle
        outcome = await sess.dispatch(task_id="t1", message="sheet")
        assert outcome.success
        assert sess.state == SessionState.Busy
        assert recorded["task_id"] == "t1"
        assert recorded["message"] == "sheet"
        assert recorded["package"]["clawteamTaskId"] == "CT-1"
        sess.mark_idle()
        await sess.shutdown()

    asyncio.run(scenario())


def test_external_session_dispatch_failure_reverts_to_idle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.scheduler.sessions.base import SessionState
    from app.scheduler.sessions.external import ExternalNodeSession

    async def failing_dispatch(**kw: Any) -> None:
        raise RuntimeError("endpoint down")

    monkeypatch.setattr(ext_svc, "dispatch_external_task", failing_dispatch)

    async def scenario() -> None:
        agent = FlowAgent(id="ext-node", kind=AgentKind.external,
                          external=_human_cfg())

        async def package_provider(task_id: str) -> dict[str, Any]:
            return {}

        sess = ExternalNodeSession(
            agent=agent, team_name="tm", run_id="run-1",
            storage=get_storage(), package_provider=package_provider,
        )
        await sess.spawn()
        outcome = await sess.dispatch(task_id="t1", message="sheet")
        assert not outcome.success
        assert "endpoint down" in outcome.detail
        # Reverted to Idle so the controller can retry next tick.
        assert sess.state == SessionState.Idle

    asyncio.run(scenario())


# ── completion service ──────────────────────────────────────────────────


class _FakeMcp:
    def __init__(self) -> None:
        self.mailbox_calls: list[dict[str, Any]] = []
        self.task_updates: list[dict[str, Any]] = []

    async def mailbox_send(self, **kw: Any) -> None:
        self.mailbox_calls.append(kw)

    async def task_update(self, **kw: Any) -> dict[str, Any]:
        self.task_updates.append(kw)
        return {}


def _mk_run_with_dispatch(nonce: str = "n-1") -> FlowRun:
    storage = get_storage()
    flow = storage.flow_create(Flow(name="f", owner_user="alice").with_spec(
        FlowSpec(agents=[
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=True),
            FlowAgent(id="ext-node", kind=AgentKind.external,
                      external=_human_cfg()),
        ], tasks=[
            FlowTask(id="t1", owner_agent_id="ext-node", subject="s"),
            FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                     depends_on=["t1"], is_leader_summary=True),
        ]),
    ))
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-ext-test",
        status=RunStatus.running, inputs={}, user="alice",
    ))
    storage.event_append(RunEvent(
        run_id=run.id, type=EXTERNAL_TASK_DISPATCHED_EVENT,
        agent_id="ext-node", task_id="t1",
        payload={
            "channel": "human", "nonce": nonce,
            "clawteamTaskId": "CT-77", "leaderAgentId": "leader",
            "subject": "s",
        },
    ))
    return run


def _fake_mcp(monkeypatch: pytest.MonkeyPatch) -> _FakeMcp:
    from app.integrations import clawteam_mcp as mcp_mod

    fake = _FakeMcp()

    async def fake_get(**kw: Any) -> _FakeMcp:
        return fake

    monkeypatch.setattr(mcp_mod, "get_mcp_client", fake_get)
    return fake


def test_complete_success_sends_mailbox_and_marks_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch()
    storage = get_storage()

    result = asyncio.run(complete_external_task(
        storage=storage, run=run, task_id="t1", nonce="n-1",
        ok=True, summary="printed and mounted", source="test",
    ))
    assert result["status"] == "recorded"
    # Same message shape a worker sends itself (strict downstream matching).
    assert fake.mailbox_calls[0]["from_agent"] == "ext-node"
    assert fake.mailbox_calls[0]["to"] == "leader"
    assert fake.mailbox_calls[0]["content"] == "task t1 done: printed and mounted"
    # ClawTeam task flipped via the recorded opaque id, forced.
    assert fake.task_updates[0]["task_id"] == "CT-77"
    assert fake.task_updates[0]["status"] == "completed"
    assert fake.task_updates[0]["force"] is True
    # Completion event recorded → idempotency (second call is a no-op).
    events = storage.event_list(run_id=run.id, limit=100)
    assert any(e.type == EXTERNAL_TASK_COMPLETED_EVENT for e in events)
    again = asyncio.run(complete_external_task(
        storage=storage, run=run, task_id="t1", nonce="n-1",
        ok=True, summary="dup", source="test",
    ))
    assert again["status"] == "already_recorded"
    assert len(fake.task_updates) == 1  # no second ClawTeam write


def test_complete_failed_sends_failure_signal_not_completed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch()
    asyncio.run(complete_external_task(
        storage=get_storage(), run=run, task_id="t1", nonce="n-1",
        ok=False, summary="parts missing", source="test",
    ))
    # Legacy FAILED signal → failure detector applies on_failure next tick.
    assert fake.mailbox_calls[0]["content"] == "FAILED: t1: parts missing"
    assert fake.task_updates == []  # never marked completed on failure


def test_complete_rejects_stale_nonce_and_undispatched(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _fake_mcp(monkeypatch)
    run = _mk_run_with_dispatch(nonce="n-latest")
    with pytest.raises(ExternalTaskError) as exc:
        asyncio.run(complete_external_task(
            storage=get_storage(), run=run, task_id="t1", nonce="n-old",
            ok=True, summary="x", source="test",
        ))
    assert exc.value.code == "EXTERNAL_TICKET_STALE"
    with pytest.raises(ExternalTaskError) as exc2:
        asyncio.run(complete_external_task(
            storage=get_storage(), run=run, task_id="t-unknown", nonce="n",
            ok=True, summary="x", source="test",
        ))
    assert exc2.value.code == "EXTERNAL_TASK_NOT_DISPATCHED"


# ── dispatch: outbound contracts + human-channel notification ───────────


def _mk_run_for_dispatch() -> FlowRun:
    storage = get_storage()
    flow = storage.flow_create(Flow(name="f-dispatch", owner_user="alice").with_spec(
        FlowSpec(agents=[
            FlowAgent(id="leader", kind=AgentKind.claude, repo="/tmp/r",
                      is_leader=True),
            FlowAgent(id="ext-node", kind=AgentKind.external,
                      external=_human_cfg()),
        ], tasks=[
            FlowTask(id="t1", owner_agent_id="ext-node", subject="s"),
            FlowTask(id="ts", owner_agent_id="leader", subject="sum",
                     depends_on=["t1"], is_leader_summary=True),
        ]),
    ))
    return storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1, team_name="csflow-ext-disp",
        status=RunStatus.running, inputs={}, user="alice",
    ))


def test_dispatch_webhook_outbound_includes_callback_contract(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The webhook outbound package must be self-describing: split briefing
    fields + an explicit completion-callback contract."""
    run = _mk_run_for_dispatch()
    agent = FlowAgent(
        id="ext-node", kind=AgentKind.external,
        external=ExternalNodeConfig(
            channel=ExternalChannel.webhook,
            endpoint_url="https://partner.example/tasks",
        ),
    )
    captured: dict[str, Any] = {}

    async def fake_post(url: str, body: dict[str, Any]) -> None:
        captured["url"] = url
        captured["body"] = body

    monkeypatch.setattr(ext_svc, "_post_outbound", fake_post)
    asyncio.run(ext_svc.dispatch_external_task(
        storage=get_storage(), run_id=run.id, team_name=run.team_name,
        agent=agent, task_id="t1", message="sheet",
        package={
            "subject": "s", "description": "briefing",
            "outputRequirement": "a verdict", "clawteamTaskId": "CT-1",
        },
    ))
    body = captured["body"]
    assert captured["url"] == "https://partner.example/tasks"
    assert body["event"] == "external_task_dispatch"
    assert body["schemaVersion"] == 1
    assert body["description"] == "briefing"
    assert body["outputRequirement"] == "a verdict"
    # Self-describing completion contract (flat convenience fields kept too).
    assert body["callback"]["method"] == "POST"
    assert body["callback"]["url"] == body["callbackUrl"]
    assert body["callback"]["bodyExample"]["status"] == "success | failed"
    assert body["callbackToken"].count(".") == 1
    from app.scheduler.prompts import WEBHOOK_REMOTE_NOTES
    assert body["notes"] == WEBHOOK_REMOTE_NOTES


def test_dispatch_remote_csflow_forwards_configured_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """external.inputs (remote Flow param fields) travel in the delegate body."""
    import httpx

    cfg = load_config()
    cfg.external_remote_targets = {"peer": "sec-123"}
    save_config(cfg)

    run = _mk_run_for_dispatch()
    agent = FlowAgent(
        id="ext-node", kind=AgentKind.external,
        external=ExternalNodeConfig(
            channel=ExternalChannel.remote_csflow,
            base_url="http://remote:17017",
            flow_id="flow-abc",
            pair_token_ref="peer",
            inputs={"需求描述": "抓取周报", "目标目录": "/data"},
        ),
    )
    captured: dict[str, Any] = {}

    class _FakeResp:
        status_code = 202

        @staticmethod
        def json() -> dict[str, Any]:
            return {"id": "run-remote-1"}

    class _FakeClient:
        def __init__(self, **kw: Any) -> None:
            pass

        async def __aenter__(self) -> _FakeClient:
            return self

        async def __aexit__(self, *a: Any) -> None:
            return None

        async def post(self, url: str, **kw: Any) -> _FakeResp:
            captured["url"] = url
            captured["json"] = kw.get("json")
            captured["headers"] = kw.get("headers")
            return _FakeResp()

    monkeypatch.setattr(httpx, "AsyncClient", _FakeClient)
    asyncio.run(ext_svc.dispatch_external_task(
        storage=get_storage(), run_id=run.id, team_name=run.team_name,
        agent=agent, task_id="t1", message="runtime prompt text",
        package={"subject": "s", "clawteamTaskId": "CT-1"},
    ))
    assert captured["url"] == "http://remote:17017/api/external/delegate"
    assert captured["headers"] == {"Authorization": "Bearer sec-123"}
    body = captured["json"]
    assert body["flowId"] == "flow-abc"
    # Slim brief from package fields — not the full external task sheet.
    assert body["runtimePrompt"] == "s"
    assert body["inputs"] == {"需求描述": "抓取周报", "目标目录": "/data"}


def test_build_external_dispatch_notification_payload() -> None:
    """The human-channel notify payload is a TASK notification: identity +
    compact task brief as content (never a 'run finished' body)."""
    from app.services.external_tasks import build_external_dispatch_notification
    from app.services.run_notify import render_message_text

    run = _mk_run_for_dispatch()
    payload = build_external_dispatch_notification(
        run,
        package={
            "taskId": "t1", "subject": "Review the PCB",
            "channel": "human", "assignee": "Alice",
            "description": "do the review",
            "outputRequirement": "a verdict",
            "flowDescription": "Board bring-up",
        },
        message="## ClawsomeFlow External Task\n(should not appear when package is set)",
        flow_name="Board bring-up",
        lang="en",
    )
    assert payload["event"] == "run_external_task"
    assert payload["taskId"] == "t1"
    assert payload["taskSubject"] == "Review the PCB"
    assert payload["channel"] == "human"
    assert payload["assignee"] == "Alice"
    assert payload["runUrl"].endswith(f"/runs/{run.id}")
    assert "do the review" in payload["content"]
    assert "ClawsomeFlow External Task" not in payload["content"]

    text = render_message_text(payload, lang="en")
    assert "external task dispatched" in text
    assert "Human" in text
    assert "run finished" not in text  # the old buggy headline
    assert "Task briefing" in text
    assert "Review the PCB" in text


def test_resolve_external_callback_base_url_rewrites_poisoned_loopback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Config, save_config
    from app.services.external_tasks import resolve_external_callback_base_url

    cfg = Config(csflow_port=17017, external_callback_base_url="http://127.0.0.1:10208")
    save_config(cfg)
    assert resolve_external_callback_base_url() == "http://127.0.0.1:17017"


def test_prepare_rewrites_poisoned_callback_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import Config, save_config

    save_config(Config(csflow_port=17017))
    run = FlowRun(
        flow_id="f", flow_version=1, team_name="tm",
        status=RunStatus.completed, user="alice",
        inputs={EXTERNAL_CALLBACK_KEY: json.dumps({
            "url": "http://127.0.0.1:10208/api/external/tasks/r/t/complete",
            "token": "tok",
        })},
    )
    prepared = prepare_delegate_callback(run)
    assert prepared is not None
    assert prepared["url"].startswith("http://127.0.0.1:17017/")


# ── delegate callback preparation ───────────────────────────────────────


def test_prepare_delegate_callback_terminal_with_marker() -> None:
    run = FlowRun(
        flow_id="f", flow_version=1, team_name="tm",
        status=RunStatus.completed, user="alice",
        inputs={EXTERNAL_CALLBACK_KEY: json.dumps(
            {"url": "http://origin/cb", "token": "tok"},
        )},
    )
    prepared = prepare_delegate_callback(run)
    assert prepared is not None
    assert prepared["url"] == "http://origin/cb"
    assert prepared["ok"] is True
    # In-flight dedupe marker stamped in the same commit; send clears it on
    # exhausted failure so a later run_update can retry.
    assert EXTERNAL_CALLBACK_SENT_KEY in run.inputs
    # Second call is a no-op while the marker is present.
    assert prepare_delegate_callback(run) is None


def test_prepare_delegate_callback_skips_nonterminal_and_unmarked() -> None:
    marked = FlowRun(
        flow_id="f", flow_version=1, team_name="tm",
        status=RunStatus.running, user="alice",
        inputs={EXTERNAL_CALLBACK_KEY: json.dumps({"url": "u", "token": "t"})},
    )
    assert prepare_delegate_callback(marked) is None
    unmarked = FlowRun(
        flow_id="f", flow_version=1, team_name="tm",
        status=RunStatus.completed, user="alice", inputs={},
    )
    assert prepare_delegate_callback(unmarked) is None


def test_failed_terminal_maps_to_failed_callback() -> None:
    run = FlowRun(
        flow_id="f", flow_version=1, team_name="tm",
        status=RunStatus.failed, user="alice",
        inputs={EXTERNAL_CALLBACK_KEY: json.dumps({"url": "u", "token": "t"})},
    )
    prepared = prepare_delegate_callback(run)
    assert prepared is not None and prepared["ok"] is False


# ── config compatibility (upgrade parity) ───────────────────────────────


def test_config_new_fields_have_safe_defaults() -> None:
    # An upgrade-only user's old config.json (no external_* keys) must load.
    cfg = Config.model_validate({"deployment_mode": "local"})
    # /api/external defaults open — credential-gated, no CLI opt-in needed.
    assert cfg.external_api_expose is True
    assert cfg.external_callback_base_url is None
    assert cfg.external_pair_tokens == {}
    assert cfg.external_remote_targets == {}
