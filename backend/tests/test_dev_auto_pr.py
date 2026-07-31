"""Developer-mode "submit PR" (自动提交 PR) feature tests.

Covers:

* ``flow_modes.task_dev_submit_pr`` resolution + ``task_self_merges`` exclusion.
* ``finalize.compute_dev_pending_pr_agent_ids`` — PR tasks excluded, auto-PR
  failures unioned in.
* ``run_metadata`` PR-record append/dedupe + failed-marker helpers.
* ``GET /api/runs/{id}/run-diff`` ``prs`` — recorded PRs + gh discovery merge.
* Manual pending-PR submit records a PR (shows in "本次执行的修改").
"""

from __future__ import annotations

import itertools
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from app.config import load_config, save_config
from app.flow_modes import task_dev_submit_pr, task_self_merges
from app.main import create_app
from app.models import (
    AgentKind,
    Flow,
    FlowAgent,
    FlowRun,
    FlowSpec,
    FlowTask,
    MergeStrategy,
    OnFailure,
    RunStatus,
)
from app.scheduler import finalize as fin
from app.scheduler.run_metadata import (
    DEV_PENDING_PR_AGENT_IDS_KEY,
    DEV_PR_FAILED_AGENT_IDS_KEY,
    RUN_PR_RECORDS_KEY,
    append_run_pr_record,
    coalesce_reverted_merge_markers,
    read_dev_pr_failed_agent_ids,
    read_run_pr_records,
    write_dev_pr_failed_agent_ids,
)
from app.storage import get_storage


@pytest.fixture
def app_client(tmp_path: Path):
    cfg = load_config()
    cfg = cfg.model_copy(update={"default_user": "alice"})
    save_config(cfg)
    with TestClient(create_app()) as c:
        yield c


def _agent(aid: str, *, kind: AgentKind = AgentKind.claude,
           leader: bool = False, repo: str = "/tmp/r") -> FlowAgent:
    return FlowAgent(
        id=aid, kind=kind, repo=repo, is_leader=leader,
        merge_strategy=MergeStrategy.manual,
        on_failure=OnFailure.retry, max_retries=2,
    )


def _dev_spec(*, repo: str = "/tmp/r") -> FlowSpec:
    """alice = PR task, bob = auto-merge task, leader = summary (PR)."""
    return FlowSpec(
        agents=[
            _agent("alice", repo=repo),
            _agent("bob", repo=repo),
            _agent("leader", leader=True, repo=repo),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[],
                     dev_auto_merge=False, dev_submit_pr=True),
            FlowTask(id="t2", owner_agent_id="bob", subject="y",
                     description="", depends_on=[], dev_auto_merge=True),
            FlowTask(id="ts", owner_agent_id="leader", subject="s",
                     description="", depends_on=["t1", "t2"],
                     is_leader_summary=True,
                     dev_auto_merge=False, dev_submit_pr=True),
        ],
        variables={"csflow.dev_mode": "true"},
    )


_TEAM_SEQ = itertools.count(1)


def _make_flow_and_run(
    *,
    spec: FlowSpec | None = None,
    status: RunStatus = RunStatus.completed,
    inputs: dict[str, Any] | None = None,
    repo: str = "/tmp/r",
) -> tuple[Flow, FlowRun]:
    storage = get_storage()
    flow = Flow(name="dev-auto-pr", description="", owner_user="alice").with_spec(
        spec or _dev_spec(repo=repo),
    )
    flow = storage.flow_create(flow)
    run = storage.run_create(FlowRun(
        flow_id=flow.id, flow_version=1,
        team_name=f"csflow-autopr{next(_TEAM_SEQ)}",
        status=status, inputs=inputs or {}, user="alice",
    ))
    return flow, run


# ── flow_modes resolution ─────────────────────────────────────────────


def test_task_dev_submit_pr_resolution() -> None:
    spec = _dev_spec()
    agents = {a.id: a for a in spec.agents}
    t1 = next(t for t in spec.tasks if t.id == "t1")
    assert task_dev_submit_pr(mode="dev", task=t1, agent=agents["alice"]) is True
    # Not dev mode → never a PR task.
    assert task_dev_submit_pr(mode="easy", task=t1, agent=agents["alice"]) is False
    assert task_dev_submit_pr(mode="normal", task=t1, agent=agents["alice"]) is False
    # OpenClaw is never a PR task.
    oc = FlowAgent(
        id="ocw", kind=AgentKind.openclaw, repo="", is_leader=False,
        merge_strategy=MergeStrategy.agent_self,
        on_failure=OnFailure.retry, max_retries=2,
    )
    assert task_dev_submit_pr(mode="dev", task=t1, agent=oc) is False
    # External execution nodes own no worktree — never a PR task.
    from types import SimpleNamespace

    ext = SimpleNamespace(kind=AgentKind.external)
    assert task_dev_submit_pr(mode="dev", task=t1, agent=ext) is False
    # A PR task never self-merges (even when dev_auto_merge is also True).
    both = t1.model_copy(update={"dev_auto_merge": True})
    assert task_self_merges(
        mode="dev", run_is_scheduled=False, task=both, agent=agents["alice"],
    ) is False
    # Leader summary PR task is eligible too.
    ts = next(t for t in spec.tasks if t.id == "ts")
    assert task_dev_submit_pr(mode="dev", task=ts, agent=agents["leader"]) is True


def test_dev_submit_pr_defaults_false() -> None:
    task = FlowTask(id="t", owner_agent_id="a", subject="s", description="")
    assert task.dev_submit_pr is False


# ── pending-PR marker computation ─────────────────────────────────────


def test_pending_pr_marker_excludes_pr_tasks() -> None:
    flow, run = _make_flow_and_run()
    # alice (PR task) and leader (PR summary task) must NOT be pending.
    assert fin.compute_dev_pending_pr_agent_ids(flow=flow, run=run) == []


def test_pending_pr_marker_unions_auto_pr_failures() -> None:
    flow, run = _make_flow_and_run(
        inputs={DEV_PR_FAILED_AGENT_IDS_KEY: ["alice"]},
    )
    assert fin.compute_dev_pending_pr_agent_ids(flow=flow, run=run) == ["alice"]


# ── run_metadata helpers ──────────────────────────────────────────────


def test_append_run_pr_record_dedupes() -> None:
    storage = get_storage()
    _flow, run = _make_flow_and_run()
    rec = {
        "agent_id": "alice", "task_id": "t1",
        "branch": "clawteam/x/alice", "target_branch": "main",
        "repo_root": "/tmp/r", "pr_url": "https://github.com/a/b/pull/1",
        "source": "auto", "at": "2026-07-31T00:00:00+00:00",
    }
    append_run_pr_record(run, storage, rec)
    append_run_pr_record(run, storage, rec)  # duplicate → ignored
    records = read_run_pr_records(storage.run_get(run.id))
    assert len(records) == 1
    assert records[0]["pr_url"] == "https://github.com/a/b/pull/1"
    assert records[0]["source"] == "auto"
    assert records[0]["task_id"] == "t1"
    # A second, distinct PR from the same worktree is kept alongside.
    append_run_pr_record(
        run, storage, {**rec, "pr_url": "https://github.com/a/b/pull/2"},
    )
    assert len(read_run_pr_records(storage.run_get(run.id))) == 2


def test_append_run_pr_record_ignores_incomplete() -> None:
    storage = get_storage()
    _flow, run = _make_flow_and_run()
    append_run_pr_record(run, storage, {"agent_id": "alice", "pr_url": ""})
    append_run_pr_record(run, storage, {"agent_id": "", "pr_url": "https://x"})
    assert read_run_pr_records(storage.run_get(run.id)) == []


def test_failed_agent_ids_roundtrip() -> None:
    _flow, run = _make_flow_and_run()
    assert read_dev_pr_failed_agent_ids(run) == set()
    write_dev_pr_failed_agent_ids(run, {"alice", " bob "})
    assert set(run.inputs[DEV_PR_FAILED_AGENT_IDS_KEY]) == {"alice", "bob"}
    write_dev_pr_failed_agent_ids(run, set())
    assert DEV_PR_FAILED_AGENT_IDS_KEY not in run.inputs


def test_coalesce_unions_pr_records_into_stale_run() -> None:
    storage = get_storage()
    _flow, run = _make_flow_and_run()
    append_run_pr_record(
        run, storage,
        {"agent_id": "alice", "pr_url": "https://github.com/a/b/pull/1",
         "branch": "b", "source": "manual"},
    )
    stale = run.model_copy(update={"inputs": {}})
    coalesce_reverted_merge_markers(stale, storage)
    assert len(stale.inputs.get(RUN_PR_RECORDS_KEY) or []) == 1


# ── run-diff PR entries ───────────────────────────────────────────────


def test_run_diff_lists_recorded_prs(
    app_client: TestClient, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    flow, run = _make_flow_and_run(
        repo=str(repo),
        inputs={
            RUN_PR_RECORDS_KEY: [
                {"agent_id": "alice", "task_id": "t1",
                 "branch": "clawteam/csflow-autoprX/alice",
                 "target_branch": "main", "repo_root": str(repo),
                 "pr_url": "https://github.com/a/b/pull/1",
                 "source": "auto", "at": "2026-07-31T00:00:00+00:00"},
            ],
        },
    )
    del flow
    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200, r.text
    prs = r.json()["prs"]
    assert len(prs) == 1
    assert prs[0]["agentId"] == "alice"
    assert len(prs[0]["prs"]) == 1
    assert prs[0]["prs"][0]["prUrl"] == "https://github.com/a/b/pull/1"
    assert prs[0]["prs"][0]["source"] == "auto"


def test_run_diff_discovers_agent_opened_prs(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _flow, run = _make_flow_and_run(repo=str(repo))

    async def fake_list_branch_prs(*, cwd: str, branch: str):
        del cwd
        if branch.endswith("/alice"):
            return [
                {"url": "https://github.com/a/b/pull/9", "title": "agent PR",
                 "state": "OPEN", "base": "main"},
                {"url": "https://github.com/a/b/pull/10", "title": "agent PR 2",
                 "state": "MERGED", "base": "main"},
            ]
        return []

    from app.services import dev_pr

    monkeypatch.setattr(dev_pr, "list_branch_prs", fake_list_branch_prs)
    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200, r.text
    prs = r.json()["prs"]
    # Multiple PRs from ONE worktree collapse into a single group entry.
    assert len(prs) == 1
    assert prs[0]["agentId"] == "alice"
    urls = {p["prUrl"] for p in prs[0]["prs"]}
    assert urls == {
        "https://github.com/a/b/pull/9",
        "https://github.com/a/b/pull/10",
    }
    assert all(p["source"] == "discovered" for p in prs[0]["prs"])


def test_run_diff_discovery_dedupes_and_enriches_records(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    branch = "clawteam/x/alice"
    _flow, run = _make_flow_and_run(
        repo=str(repo),
        inputs={
            RUN_PR_RECORDS_KEY: [
                {"agent_id": "alice", "task_id": "t1", "branch": branch,
                 "target_branch": "main", "repo_root": str(repo),
                 "pr_url": "https://github.com/a/b/pull/1",
                 "source": "auto", "at": ""},
            ],
        },
    )

    async def fake_list_branch_prs(*, cwd: str, branch: str):
        del cwd
        if branch != "clawteam/x/alice":
            return []
        return [
            {"url": "https://github.com/a/b/pull/1", "title": "auto PR",
             "state": "OPEN", "base": "main"},
        ]

    from app.services import dev_pr

    monkeypatch.setattr(dev_pr, "list_branch_prs", fake_list_branch_prs)
    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    prs = r.json()["prs"]
    assert len(prs) == 1
    assert len(prs[0]["prs"]) == 1
    link = prs[0]["prs"][0]
    assert link["source"] == "auto"  # recorded entry wins
    assert link["title"] == "auto PR"  # enriched with live title
    assert link["state"] == "OPEN"


def test_run_diff_no_prs_outside_dev_mode(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    spec = _dev_spec(repo=str(repo)).model_copy(update={"variables": {}})
    _flow, run = _make_flow_and_run(spec=spec)

    async def boom(*, cwd: str, branch: str):  # must not be called
        raise AssertionError("discovery should be skipped outside dev mode")

    from app.services import dev_pr

    monkeypatch.setattr(dev_pr, "list_branch_prs", boom)
    r = app_client.get(f"/api/runs/{run.id}/run-diff")
    assert r.status_code == 200
    assert r.json()["prs"] == []


# ── manual submit records the PR ──────────────────────────────────────


class _ApiStubCli:
    def __init__(self, rows: list[dict[str, str]]) -> None:
        self.rows = rows

    async def workspace_list(self, *, team: str, repo: str | None = None):
        del team, repo
        return list(self.rows)

    async def workspace_has_uncommitted_changes(self, *, worktree_path: str):
        del worktree_path
        return False, []


def test_manual_pending_pr_submit_records_pr(
    app_client: TestClient, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    wt = tmp_path / "wt-alice"
    wt.mkdir()
    spec = FlowSpec(
        agents=[
            _agent("alice", repo=str(repo)),
            _agent("bob", repo=str(repo)),
            _agent("leader", leader=True, repo=str(repo)),
        ],
        tasks=[
            FlowTask(id="t1", owner_agent_id="alice", subject="x",
                     description="", depends_on=[], dev_auto_merge=False),
            FlowTask(id="t2", owner_agent_id="bob", subject="y",
                     description="", depends_on=[], dev_auto_merge=True),
            FlowTask(id="ts", owner_agent_id="leader", subject="s",
                     description="", depends_on=["t1", "t2"],
                     is_leader_summary=True),
        ],
        variables={"csflow.dev_mode": "true"},
    )
    flow, run = _make_flow_and_run(
        spec=spec,
        inputs={
            DEV_PENDING_PR_AGENT_IDS_KEY: ["alice"],
            DEV_PR_FAILED_AGENT_IDS_KEY: ["alice"],
        },
    )
    del flow
    from app.api import runs as runs_mod

    rows = [{
        "agent_name": "alice",
        "branch_name": f"clawteam/{run.team_name}/alice",
        "base_branch": "main",
        "repo_root": str(repo),
        "worktree_path": str(wt),
        "team_name": run.team_name,
    }]
    monkeypatch.setattr(runs_mod, "get_clawteam_cli", lambda: _ApiStubCli(rows))

    async def fake_run_pr_command(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        if argv[0] == "git":
            return 0, "", ""
        return 0, "https://github.com/acme/x/pull/7\n", ""

    from app.services import dev_pr

    # gh is the only URL source in this fake → exercises the gh fallback path.
    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run_pr_command)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: True)

    async def fake_cleanup(*, run, agent_id, storage, **kw):
        del run, agent_id, storage, kw
        return True

    monkeypatch.setattr(
        runs_mod, "cleanup_non_openclaw_workspace_after_review_decision",
        fake_cleanup,
    )

    async def fake_tail(*, run, storage, flow=None, **kw):
        del run, storage, flow, kw

    monkeypatch.setattr(runs_mod, "_cleanup_terminal_tail", fake_tail)

    r = app_client.post(f"/api/runs/{run.id}/pending-prs/alice/submit")
    assert r.status_code == 200, r.text
    assert r.json()["success"] is True

    # PR recorded → appears in "本次执行的修改" (run-diff prs); failure marker cleared.
    refreshed = get_storage().run_get(run.id)
    records = read_run_pr_records(refreshed)
    assert len(records) == 1
    assert records[0]["pr_url"] == "https://github.com/acme/x/pull/7"
    assert records[0]["source"] == "manual"
    assert records[0]["agent_id"] == "alice"
    assert read_dev_pr_failed_agent_ids(refreshed) == set()


# ── auto-PR service pipeline (git-native first) ───────────────────────


@pytest.mark.asyncio
async def test_push_pr_url_harvested_from_push_output(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Platform prints an existing/created PR link on push → done, one command."""
    from app.services import dev_pr

    commands: list[list[str]] = []

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        commands.append(list(argv))
        return 0, "", "remote: https://github.com/a/b/pull/3"

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: False)
    ok, url, step, _detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is True and url == "https://github.com/a/b/pull/3" and step == ""
    assert commands == [["git", "push", "-u", "origin", "br"]]


@pytest.mark.asyncio
async def test_push_pr_created_via_git_push_options(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """GitLab/Gitee style: ``git push -o merge_request.create`` (git-native)."""
    from app.services import dev_pr

    commands: list[list[str]] = []

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        commands.append(list(argv))
        if "merge_request.create" in argv:
            return 0, "", "remote: https://gitlab.com/a/b/-/merge_requests/5"
        return 0, "", ""

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: False)
    ok, url, _step, _detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is True and url == "https://gitlab.com/a/b/-/merge_requests/5"
    assert commands[1][:3] == ["git", "push", "-o"]


@pytest.mark.asyncio
async def test_push_pr_gh_fallback_when_installed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Optional gh fallback covers GitHub auto-creation when gh exists."""
    from app.services import dev_pr

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        if argv[0] == "gh":
            return 0, "https://github.com/a/b/pull/8\n", ""
        return 0, "", ""  # push ok, push-options tolerated failure/no URL

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: True)
    ok, url, _step, _detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is True and url == "https://github.com/a/b/pull/8"


@pytest.mark.asyncio
async def test_push_pr_falls_back_to_create_page_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No gh, no push-options: GitHub's pre-filled create-PR page from push output."""
    from app.services import dev_pr

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        if "merge_request.create" in argv:
            return 128, "", "error: push options not supported"
        return 0, "", (
            "remote: Create a pull request for 'br' on GitHub by visiting:\n"
            "remote:      https://github.com/a/b/pull/new/br"
        )

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: False)
    ok, url, step, _detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is True and step == ""
    assert url == "https://github.com/a/b/pull/new/br"


@pytest.mark.asyncio
async def test_push_and_create_pr_push_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dev_pr

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        return 1, "", "permission denied"

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    ok, url, step, detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is False and url == "" and step == "push"
    assert "permission denied" in detail


@pytest.mark.asyncio
async def test_push_pr_total_failure_without_any_url(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No URL anywhere → honest pr_create failure (never blocks the run)."""
    from app.services import dev_pr

    async def fake_run(argv, *, cwd, timeout_sec):
        del cwd, timeout_sec
        if argv[0] == "gh":
            return 1, "", "gh boom"
        return 0, "", ""

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: True)
    ok, url, step, detail = await dev_pr.push_and_create_pr(
        worktree="/tmp/wt", branch="br", target_branch="main",
        title="t", body="b",
    )
    assert ok is False and url == "" and step == "pr_create"
    assert "gh boom" in detail


@pytest.mark.asyncio
async def test_list_branch_prs_without_gh_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.services import dev_pr

    async def boom(argv, *, cwd, timeout_sec):
        raise AssertionError("must not run any command without gh")

    monkeypatch.setattr(dev_pr, "run_pr_command", boom)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: False)
    assert await dev_pr.list_branch_prs(cwd="/tmp", branch="br") == []


@pytest.mark.asyncio
async def test_list_branch_prs_tolerates_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.services import dev_pr

    async def fake_run(argv, *, cwd, timeout_sec):
        del argv, cwd, timeout_sec
        return 1, "", "no git remote"

    monkeypatch.setattr(dev_pr, "run_pr_command", fake_run)
    monkeypatch.setattr(dev_pr, "_gh_available", lambda: True)
    assert await dev_pr.list_branch_prs(cwd="/tmp", branch="br") == []
