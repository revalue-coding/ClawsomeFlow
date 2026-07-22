"""Temporary-agent platform support: gemini / kimi / qwen / opencode / nanobot.

These five CLIs spawn like claude/codex/cursor (temporary, ad-hoc inline Flow
agents — no persistent management platform), but ClawsomeFlow SELF-CONTROLS their
permission flags (passes ``skip_permissions=False`` so ClawTeam injects nothing).
That's required because ClawTeam's ``--yolo`` injection is wrong for current CLI
versions, verified against the installed binaries:

* gemini  → ``--approval-mode yolo`` (``--yolo`` conflicts with ``--approval-mode``);
            resume ``--resume latest`` (no ``--continue``).
* qwen    → ``--approval-mode yolo --chat-recording`` (recording needed for resume);
            resume ``--continue``.
* kimi    → ``--yolo``; resume ``--continue`` (kimi-code from ``install.sh``).
* opencode→ no permission flag (ClawTeam's ``--yolo`` is REJECTED → spawn fails;
            interactive auto-approval is config-only); resume ``--continue``.
* pi      → no tool-permission popup at all (read/bash/edit/write auto-execute);
            carry only ``-a/--approve`` (trust project-local files); resume
            ``--continue``. ClawTeam natively special-cases ``pi`` to inject
            nothing, so self-control is the only correct path.
* nanobot → ``nanobot agent`` + a stable per-agent ``-s`` (isolation + resume).
"""

from __future__ import annotations

import pytest

from app.models import AgentKind, FlowAgent
from app.scheduler.sessions.tmux_live import (
    _KIND_TO_CMD,
    _SELF_PERMISSION_KINDS,
    TmuxLiveSession,
)
from app.services.task_decompose import (
    _NON_OPENCLAW_SUPPORTED_KINDS,
    _TEMP_AGENT_PLATFORM_BINARIES,
    _non_openclaw_dispatch_argv,
)

# All kinds with a runtime mapping (nanobot included — its mapping is kept ready
# even though the platform is temporarily not exposed to users).
_NEW_KINDS = [
    AgentKind.gemini,
    AgentKind.kimi,
    AgentKind.qwen,
    AgentKind.opencode,
    AgentKind.pi,
    AgentKind.qoder,
    AgentKind.codebuddy,
    AgentKind.nanobot,
]

# Kinds actually exposed to users (Flow editor + AI decomposer). nanobot excluded.
_USER_EXPOSED_KINDS = [
    AgentKind.gemini,
    AgentKind.kimi,
    AgentKind.qwen,
    AgentKind.opencode,
    AgentKind.pi,
    AgentKind.qoder,
    AgentKind.codebuddy,
]


def test_kind_to_cmd_spawn_and_resume() -> None:
    """Pin the exact verified spawn/resume commands per platform."""
    assert _KIND_TO_CMD[AgentKind.gemini] == (
        ["gemini", "--approval-mode", "yolo"],
        ["gemini", "--approval-mode", "yolo", "--resume", "latest"],
    )
    assert _KIND_TO_CMD[AgentKind.qwen] == (
        ["qwen", "--approval-mode", "yolo", "--chat-recording"],
        ["qwen", "--approval-mode", "yolo", "--chat-recording", "--continue"],
    )
    assert _KIND_TO_CMD[AgentKind.kimi] == (
        ["kimi", "--yolo"],
        ["kimi", "--yolo", "--continue"],
    )
    assert _KIND_TO_CMD[AgentKind.opencode] == (
        ["opencode"],
        ["opencode", "--continue"],
    )
    # pi: only `-a/--approve` (no tool-permission popup exists); resume `--continue`.
    assert _KIND_TO_CMD[AgentKind.pi] == (
        ["pi", "-a"],
        ["pi", "-a", "--continue"],
    )
    assert _KIND_TO_CMD[AgentKind.nanobot] == (
        ["nanobot", "agent"],
        ["nanobot", "agent"],
    )
    # qoder/codebuddy are Claude-style (note qoder's underscore mode value).
    assert _KIND_TO_CMD[AgentKind.qoder] == (
        ["qodercli", "--permission-mode", "bypass_permissions"],
        ["qodercli", "--permission-mode", "bypass_permissions", "--continue"],
    )
    assert _KIND_TO_CMD[AgentKind.codebuddy] == (
        ["codebuddy", "--permission-mode", "bypassPermissions"],
        ["codebuddy", "--permission-mode", "bypassPermissions", "--continue"],
    )


@pytest.mark.parametrize("kind", _NEW_KINDS)
def test_new_platforms_self_control_permissions(kind: AgentKind) -> None:
    """All five tell ClawTeam NOT to inject a permission flag."""
    assert kind in _SELF_PERMISSION_KINDS
    agent = FlowAgent(
        id="w1", kind=kind, repo="/tmp", target_branch="main", is_temporary=True
    )
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert s._skip_permissions() is False
    assert s._resolve_profile() is None  # temporary agents carry no profile


def test_nanobot_injects_stable_session_id() -> None:
    """nanobot resume/isolation needs a stable per-agent ``-s`` on both commands."""
    agent = FlowAgent(
        id="nb1", kind=AgentKind.nanobot, repo="/tmp", target_branch="main",
        is_temporary=True,
    )
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert s._spawn_cmd == ["nanobot", "agent", "-s", "csflow-x-nb1"]
    assert s._resume_cmd == ["nanobot", "agent", "-s", "csflow-x-nb1"]
    # Shared template must not be mutated by the per-agent injection.
    assert _KIND_TO_CMD[AgentKind.nanobot] == (["nanobot", "agent"], ["nanobot", "agent"])


def test_persistent_hermes_resume_binds_profile_for_precise_continue() -> None:
    """A managed Hermes agent binds -p <id>, isolating -c to its own profile."""
    agent = FlowAgent(
        id="h1", kind=AgentKind.hermes, repo="/tmp", target_branch="main",
        is_temporary=False, profile="h1",
    )
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert s._resume_cmd == ["hermes", "--yolo", "-c", "-p", "h1"]
    # Template must not be mutated.
    assert _KIND_TO_CMD[AgentKind.hermes] == (
        ["hermes", "--yolo"], ["hermes", "--yolo", "-c"],
    )


def test_temporary_hermes_resume_drops_continue_to_avoid_cross_session() -> None:
    """Temp Hermes has no -p (shared default profile); -c would risk resuming a
    different agent's / the operator's most-recent session, so it is dropped —
    resume runs fresh in the reused worktree."""
    agent = FlowAgent(
        id="w1", kind=AgentKind.hermes, repo="/tmp", target_branch="main",
        is_temporary=True,
    )
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert "-c" not in s._resume_cmd
    assert "-p" not in s._resume_cmd
    assert s._resume_cmd == ["hermes", "--yolo"]


@pytest.mark.parametrize("kind", [k for k in _NEW_KINDS if k != AgentKind.nanobot])
def test_non_nanobot_session_uses_template_verbatim(kind: AgentKind) -> None:
    agent = FlowAgent(
        id="w1", kind=kind, repo="/tmp", target_branch="main", is_temporary=True
    )
    s = TmuxLiveSession(agent=agent, team_name="csflow-x", run_id="run-1", cli=object())
    assert s._spawn_cmd == list(_KIND_TO_CMD[kind][0])
    assert s._resume_cmd == list(_KIND_TO_CMD[kind][1])


def test_existing_platforms_still_use_clawteam_injection() -> None:
    """claude/codex keep relying on ClawTeam's bypass injection (skip_permissions=True)."""
    for kind in (AgentKind.claude, AgentKind.codex):
        agent = FlowAgent(
            id="w1", kind=kind, repo="/tmp", target_branch="main", is_temporary=True
        )
        s = TmuxLiveSession(
            agent=agent, team_name="t", run_id="r", cli=object()
        )
        assert s._skip_permissions() is True


def test_user_exposed_platforms_are_decompose_supported() -> None:
    binaries = {k for k, _ in _TEMP_AGENT_PLATFORM_BINARIES}
    for kind in _USER_EXPOSED_KINDS:
        assert kind in _NON_OPENCLAW_SUPPORTED_KINDS
        assert kind in binaries


def test_nanobot_is_not_user_exposed() -> None:
    """nanobot keeps a runtime mapping but is hidden from the decomposer."""
    assert AgentKind.nanobot in _KIND_TO_CMD  # runtime mapping kept
    assert AgentKind.nanobot not in _NON_OPENCLAW_SUPPORTED_KINDS
    binaries = {k for k, _ in _TEMP_AGENT_PLATFORM_BINARIES}
    assert AgentKind.nanobot not in binaries


def test_new_platform_headless_dispatch_commands() -> None:
    assert _non_openclaw_dispatch_argv(kind=AgentKind.gemini, message="m") == [
        "gemini", "--approval-mode", "yolo", "-p", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.qwen, message="m") == [
        "qwen", "--approval-mode", "yolo", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.kimi, message="m") == [
        "kimi", "-p", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.opencode, message="m") == [
        "opencode", "run", "--dangerously-skip-permissions", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.pi, message="m") == [
        "pi", "-a", "-p", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.nanobot, message="m") == [
        "nanobot", "agent", "-m", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.qoder, message="m") == [
        "qodercli", "--permission-mode", "bypass_permissions",
        "--dangerously-skip-permissions", "-p", "m",
    ]
    assert _non_openclaw_dispatch_argv(kind=AgentKind.codebuddy, message="m") == [
        "codebuddy", "--permission-mode", "bypassPermissions",
        "--dangerously-skip-permissions", "-p", "m",
    ]


# ── opencode global-config seeding ───────────────────────────────────


def test_opencode_config_seeds_permission_allow(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import opencode_config as oc

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = oc.opencode_config_path()
    assert path == tmp_path / "opencode" / "opencode.json"

    # force=True bypasses the which("opencode") gate.
    assert oc.ensure_opencode_permission_allow(force=True) is True
    data = json.loads(path.read_text())
    assert data["permission"] == "allow"
    assert data["$schema"] == "https://opencode.ai/config.json"

    # Idempotent: second call is a no-op.
    assert oc.ensure_opencode_permission_allow(force=True) is False


def test_opencode_config_preserves_user_permission(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import opencode_config as oc

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    path = oc.opencode_config_path()
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"permission": "ask", "model": "x/y"}))

    # Never clobber an explicit user policy; other keys preserved.
    assert oc.ensure_opencode_permission_allow(force=True) is False
    data = json.loads(path.read_text())
    assert data["permission"] == "ask"
    assert data["model"] == "x/y"


def test_opencode_config_skipped_when_not_installed(tmp_path, monkeypatch) -> None:
    from app.integrations import opencode_config as oc

    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))
    monkeypatch.setattr(oc.shutil, "which", lambda _name: None)
    # Without force and with opencode "absent", do nothing.
    assert oc.ensure_opencode_permission_allow() is False
    assert not (tmp_path / "opencode").exists()


# ── Qoder / CodeBuddy folder-trust seeding ───────────────────────────


def test_codebuddy_trust_all_seeded(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import temp_agent_trust as tat

    monkeypatch.setattr(tat.Path, "home", classmethod(lambda cls: tmp_path))
    assert tat.ensure_codebuddy_trust_all(force=True) is True
    data = json.loads((tmp_path / ".codebuddy" / "settings.json").read_text())
    assert data["trustAll"] is True
    # Idempotent.
    assert tat.ensure_codebuddy_trust_all(force=True) is False


def test_codebuddy_trust_all_preserves_user_value(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import temp_agent_trust as tat

    monkeypatch.setattr(tat.Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / ".codebuddy" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"trustAll": False, "model": "x"}))
    assert tat.ensure_codebuddy_trust_all(force=True) is False
    data = json.loads(p.read_text())
    assert data["trustAll"] is False  # not clobbered
    assert data["model"] == "x"


def test_qoder_trust_dirs_seeded_with_home(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import temp_agent_trust as tat

    monkeypatch.setattr(tat.Path, "home", classmethod(lambda cls: tmp_path))
    assert tat.ensure_qoder_trust_dirs(force=True) is True
    data = json.loads((tmp_path / ".qoder" / "settings.json").read_text())
    assert str(tmp_path) in data["permissions"]["trustDirectories"]
    # Idempotent.
    assert tat.ensure_qoder_trust_dirs(force=True) is False


def test_qoder_trust_dirs_merges_existing(tmp_path, monkeypatch) -> None:
    import json

    from app.integrations import temp_agent_trust as tat

    monkeypatch.setattr(tat.Path, "home", classmethod(lambda cls: tmp_path))
    p = tmp_path / ".qoder" / "settings.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"permissions": {"trustDirectories": ["/keep/me"]},
                             "general": {"enableAutoUpdate": False}}))
    assert tat.ensure_qoder_trust_dirs(force=True) is True
    data = json.loads(p.read_text())
    assert "/keep/me" in data["permissions"]["trustDirectories"]
    assert str(tmp_path) in data["permissions"]["trustDirectories"]
    assert data["general"]["enableAutoUpdate"] is False  # other keys preserved


def test_trust_seeders_skipped_when_not_installed(tmp_path, monkeypatch) -> None:
    from app.integrations import temp_agent_trust as tat

    monkeypatch.setattr(tat.Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr(tat.shutil, "which", lambda _name: None)
    assert tat.ensure_codebuddy_trust_all() is False
    assert tat.ensure_qoder_trust_dirs() is False
    assert not (tmp_path / ".codebuddy").exists()
    assert not (tmp_path / ".qoder").exists()
