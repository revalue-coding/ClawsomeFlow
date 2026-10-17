"""Claude folder-trust pre-seeding (``~/.claude.json``) before spawn/resume.

Claude 2.1.27x defaults its trust dialog to "No, exit" and ClawTeam answers it
with a bare Enter, so an untrusted worktree makes Claude quit at spawn. The
worktree path is written into ``projects[<path>].hasTrustDialogAccepted``
first so the dialog never shows. ``conftest`` points ``claude_config_path`` at
a per-test tmp file.
"""

from __future__ import annotations

import json
import os
import stat

import pytest

from app.integrations import claude_trust
from app.models import AgentKind, FlowAgent
from app.scheduler.sessions.tmux_live import TmuxLiveSession
from app.worktree.lookup import WorktreeInfo


def _write_cfg(data: dict) -> None:
    path = claude_trust.claude_config_path()
    path.write_text(json.dumps(data))
    os.chmod(path, 0o600)


def _read_cfg() -> dict:
    return json.loads(claude_trust.claude_config_path().read_text())


def test_seeds_trust_and_preserves_existing_config(tmp_path) -> None:
    wt = tmp_path / "wt"
    wt.mkdir()
    _write_cfg({
        "oauthAccount": {"id": "x"},
        "projects": {
            "/other": {"allowedTools": ["Bash"]},
            str(wt): {"lastCost": 1.5},
        },
    })

    assert claude_trust.ensure_claude_folder_trusted(str(wt)) is True

    data = _read_cfg()
    assert data["oauthAccount"] == {"id": "x"}
    assert data["projects"]["/other"] == {"allowedTools": ["Bash"]}
    assert data["projects"][str(wt)] == {"lastCost": 1.5, "hasTrustDialogAccepted": True}
    mode = stat.S_IMODE(claude_trust.claude_config_path().stat().st_mode)
    assert mode == 0o600
    # Idempotent: nothing left to change.
    assert claude_trust.ensure_claude_folder_trusted(str(wt)) is False


def test_seeds_realpath_variant_for_symlinked_worktree(tmp_path) -> None:
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)
    _write_cfg({})

    assert claude_trust.ensure_claude_folder_trusted(str(link)) is True

    projects = _read_cfg()["projects"]
    assert projects[str(link)]["hasTrustDialogAccepted"] is True
    assert projects[str(real)]["hasTrustDialogAccepted"] is True


def test_missing_config_is_left_alone(tmp_path) -> None:
    assert claude_trust.ensure_claude_folder_trusted(str(tmp_path)) is False
    assert not claude_trust.claude_config_path().exists()


def test_unparsable_config_is_never_rewritten(tmp_path) -> None:
    path = claude_trust.claude_config_path()
    path.write_text("{not json")
    assert claude_trust.ensure_claude_folder_trusted(str(tmp_path)) is False
    assert path.read_text() == "{not json"


class _RecordingCli:
    def __init__(self) -> None:
        self.trusted_at_spawn: list[bool] = []

    def _record(self, folder: str) -> None:
        projects = _read_cfg().get("projects", {})
        self.trusted_at_spawn.append(
            projects.get(folder, {}).get("hasTrustDialogAccepted") is True
        )

    async def spawn_fresh(self, *, team: str, agent_name: str, **_kw) -> None:
        self._record(os.environ["CLAWTEAM_DATA_DIR"] + f"/workspaces/{team}/{agent_name}")
        raise RuntimeError("stop after spawn")

    async def spawn_resume(self, *, existing_worktree: str, **_kw) -> None:
        self._record(existing_worktree)
        raise RuntimeError("stop after spawn")


def _claude_session(cli: _RecordingCli, repo: str) -> TmuxLiveSession:
    agent = FlowAgent(
        id="alice", kind=AgentKind.claude, repo=repo, target_branch="main",
        is_temporary=True,
    )
    return TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=cli)


async def test_fresh_spawn_trusts_future_worktree_before_spawn(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", str(tmp_path / "ct"))
    _write_cfg({})
    cli = _RecordingCli()
    session = _claude_session(cli, str(tmp_path))

    assert session._fresh_worktree_path() == str(tmp_path / "ct" / "workspaces" / "csflow-x" / "alice")
    with pytest.raises(RuntimeError, match="stop after spawn"):
        await session._do_spawn()
    assert cli.trusted_at_spawn == [True]


async def test_resume_trusts_existing_worktree_before_spawn(tmp_path) -> None:
    _write_cfg({})
    cli = _RecordingCli()
    session = _claude_session(cli, str(tmp_path))
    session.worktree = WorktreeInfo(
        agent_name="alice", branch_name="clawteam/csflow-x/alice",
        worktree_path=str(tmp_path / "existing-wt"), repo_root=str(tmp_path),
        base_branch="main",
    )

    with pytest.raises(RuntimeError, match="stop after spawn"):
        await session._spawn_resume_in_existing_worktree(command=["claude"], phase="resume")
    assert cli.trusted_at_spawn == [True]


def test_fresh_worktree_path_falls_back_to_clawteam_config_data_dir(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("CLAWTEAM_DATA_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    ct_cfg = tmp_path / ".clawteam" / "config.json"
    ct_cfg.parent.mkdir()
    ct_cfg.write_text(json.dumps({"data_dir": str(tmp_path / "custom")}))
    session = _claude_session(_RecordingCli(), str(tmp_path))
    assert session._fresh_worktree_path() == str(tmp_path / "custom" / "workspaces" / "csflow-x" / "alice")

    ct_cfg.write_text("{}")
    assert session._fresh_worktree_path() == str(tmp_path / ".clawteam" / "workspaces" / "csflow-x" / "alice")
