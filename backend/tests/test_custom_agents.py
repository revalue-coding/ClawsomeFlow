"""Custom-agent registry (我的团队 → 自定义Agent) — service, API, resolver, chat.

Covers the plan's test matrix:

* service helpers — slugify / shlex parse / headless argv render / binary probe;
* /api/custom-agents CRUD — validation, duplicate 409, availability probe,
  delete-time referencing-Flow warning;
* spec resolution — registry preferred, save-time snapshot fallback (deleted
  row never breaks controller construction), strict validator gate;
* TmuxLiveSession custom branch — resume_command, skip_permissions=False,
  ready extra pattern honoured by wait_tui_ready;
* chat — one-shot headless subprocess turn (fake CLI script), SSE shape,
  stop / reset / history.
"""

from __future__ import annotations

import json
import stat
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app.models import AgentKind, CustomAgent, Flow, FlowAgent, FlowSpec, FlowTask
from app.scheduler.naming import custom_agent_user_chat_session_id
from app.scheduler.sessions import tmux_ready
from app.scheduler.sessions.tmux_live import TmuxLiveSession
from app.services import custom_agent_chat as chat_svc
from app.services import custom_agents as svc
from app.storage import get_storage
from app.validators import validate_custom_agent_refs
from app.validators.flow import ERROR_CUSTOM_AGENT_NOT_FOUND, FlowValidationError

TEST_USER = "tester"


@pytest.fixture(autouse=True)
def _fixed_user(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("CSFLOW_USER", TEST_USER)
    yield


@pytest.fixture(autouse=True)
def _clear_chat_jobs():
    # The chat-job registry is module-global (keyed by user+agent id) and
    # would otherwise leak a finished job into the next test's status probe.
    chat_svc._JOBS.clear()
    yield
    chat_svc._JOBS.clear()


@pytest.fixture
def client() -> TestClient:
    with TestClient(create_app()) as c:
        yield c


def _write_script(tmp_path: Path, name: str, body: str) -> str:
    """Drop an executable shell script and return its absolute path."""
    path = tmp_path / name
    path.write_text("#!/bin/sh\n" + body)
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _register(client: TestClient, **overrides) -> dict:
    payload = {
        "name": "My Agent",
        "spawnCommand": "bash --norc",
        **overrides,
    }
    r = client.post("/api/custom-agents", json=payload)
    assert r.status_code == 201, r.text
    return r.json()


# ──────────────────────────────────────────────────────────────────────
# Service helpers
# ──────────────────────────────────────────────────────────────────────


class TestServiceHelpers:
    def test_slugify_basic(self) -> None:
        assert svc.slugify_name("My Cool Agent") == "my-cool-agent"

    def test_slugify_non_ascii_falls_back_to_hash(self) -> None:
        slug = svc.slugify_name("中文名字")
        assert slug.startswith("agent-")
        # Deterministic: the same name always slugs to the same id.
        assert slug == svc.slugify_name("中文名字")

    def test_slugify_reserved_platform_name(self) -> None:
        # "claude" is an AgentKind value — must not collide with the enum.
        assert svc.slugify_name("Claude") == "claude-agent"

    def test_parse_command_shlex(self) -> None:
        assert svc.parse_command(
            "mycli --flag 'a b'", field="spawn_command"
        ) == ["mycli", "--flag", "a b"]

    def test_parse_command_required_empty_raises(self) -> None:
        with pytest.raises(svc.CustomAgentError):
            svc.parse_command("  ", field="spawn_command", required=True)

    def test_parse_command_unbalanced_quote_raises(self) -> None:
        with pytest.raises(svc.CustomAgentError):
            svc.parse_command("mycli 'oops", field="spawn_command")

    def test_render_headless_argv_placeholder(self) -> None:
        assert svc.render_headless_argv(
            ["mycli", "-p", "{message}"], "hi there"
        ) == ["mycli", "-p", "hi there"]

    def test_render_headless_argv_appends_without_placeholder(self) -> None:
        assert svc.render_headless_argv(["mycli", "-p"], "hi") == ["mycli", "-p", "hi"]

    def test_no_binary_availability_probe_is_exported(self) -> None:
        # A registered agent is always offered; whether its command runs is the
        # user's own responsibility, so the module carries no PATH probe.
        assert not hasattr(svc, "command_available")
        assert not hasattr(svc, "ensure_command_available")


# ──────────────────────────────────────────────────────────────────────
# Registry API (CRUD)
# ──────────────────────────────────────────────────────────────────────


class TestRegistryApi:
    def test_create_and_list(self, client: TestClient) -> None:
        created = _register(client, name="Deep Worker", description="d")
        assert created["id"] == "deep-worker"
        assert created["spawnCommand"] == "bash --norc"
        assert created["spawnArgv"] == ["bash", "--norc"]
        assert created["chatAvailable"] is False
        assert created["createdByUser"] == TEST_USER

        r = client.get("/api/custom-agents")
        assert r.status_code == 200
        items = r.json()["items"]
        assert [a["id"] for a in items] == ["deep-worker"]

    def test_create_accepts_command_not_on_path(self, client: TestClient) -> None:
        # No availability gate: the user vouches for the command themselves
        # (wrappers / shell functions / a differing PATH all make a probe a
        # false gate). A command that can't start fails at dispatch time.
        r = client.post(
            "/api/custom-agents",
            json={"name": "Ghost", "spawnCommand": "no-such-binary-xyz --yolo"},
        )
        assert r.status_code == 201, r.text
        assert r.json()["spawnArgv"] == ["no-such-binary-xyz", "--yolo"]

    def test_create_duplicate_rejected(self, client: TestClient) -> None:
        _register(client, name="Dup Agent")
        r = client.post(
            "/api/custom-agents",
            json={"name": "Dup Agent", "spawnCommand": "bash"},
        )
        assert r.status_code == 409
        assert r.json()["error"] == "CUSTOM_AGENT_DUPLICATE"

    def test_headless_resume_requires_headless(self, client: TestClient) -> None:
        r = client.post(
            "/api/custom-agents",
            json={
                "name": "NoHeadless",
                "spawnCommand": "bash",
                "headlessResumeCommand": "bash -c x",
            },
        )
        assert r.status_code == 400

    def test_patch_updates_fields(self, client: TestClient, tmp_path: Path) -> None:
        created = _register(client, name="Patchable")
        script = _write_script(tmp_path, "hl.sh", "echo ok\n")
        r = client.patch(
            f"/api/custom-agents/{created['id']}",
            json={
                "description": "new desc",
                "headlessCommand": f"{script} -p {{message}}",
                "readyPattern": "READY>",
            },
        )
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["description"] == "new desc"
        assert body["chatAvailable"] is True
        assert body["readyPattern"] == "READY>"

    def test_get_missing_404(self, client: TestClient) -> None:
        r = client.get("/api/custom-agents/nope")
        assert r.status_code == 404
        assert r.json()["error"] == "CUSTOM_AGENT_NOT_FOUND"

    def test_delete_reports_referencing_flows(self, client: TestClient) -> None:
        created = _register(client, name="Referenced")
        storage = get_storage()
        spec = _spec_with_custom_agent(created["id"], command=["bash", "--norc"])
        storage.flow_create(
            Flow(
                name="uses-custom",
                spec=json.loads(spec.model_dump_json(by_alias=True)),
                owner_user=TEST_USER,
            )
        )
        r = client.delete(f"/api/custom-agents/{created['id']}")
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["deleted"] is True
        assert [f["name"] for f in body["referencedFlows"]] == ["uses-custom"]
        assert client.get(f"/api/custom-agents/{created['id']}").status_code == 404


# ──────────────────────────────────────────────────────────────────────
# Spec resolution + strict validator gate
# ──────────────────────────────────────────────────────────────────────


def _spec_with_custom_agent(
    ref: str, *, command: list[str], resume: list[str] | None = None
) -> FlowSpec:
    return FlowSpec(
        agents=[
            FlowAgent(id="lead", kind=AgentKind.claude, repo="/tmp/r", is_leader=True),
            FlowAgent(
                id=ref,
                kind=AgentKind.custom,
                custom_agent_ref=ref,
                command=command,
                resume_command=resume,
                repo="/tmp/r",
                is_temporary=True,
            ),
        ],
        tasks=[
            FlowTask(id="t0", owner_agent_id=ref, subject="w"),
            FlowTask(
                id="t1", owner_agent_id="lead", subject="s",
                depends_on=["t0"], is_leader_summary=True,
            ),
        ],
    )


class TestSpecResolution:
    def _make_row(self, **overrides) -> CustomAgent:
        row = CustomAgent(
            id="my-agent",
            name="My Agent",
            spawn_command=["bash", "--norc", "-i"],
            resume_command=["bash", "--norc", "--continue"],
            created_by_user=TEST_USER,
        )
        for k, v in overrides.items():
            setattr(row, k, v)
        return get_storage().custom_agent_create(row)

    def test_registry_preferred_over_snapshot(self) -> None:
        self._make_row()
        spec = _spec_with_custom_agent("my-agent", command=["stale", "snapshot"])
        rows = svc.resolve_spec_custom_agents(spec, storage=get_storage())
        agent = next(a for a in spec.agents if a.kind == AgentKind.custom)
        assert agent.command == ["bash", "--norc", "-i"]
        assert agent.resume_command == ["bash", "--norc", "--continue"]
        assert "my-agent" in rows

    def test_deleted_row_falls_back_to_snapshot(self) -> None:
        spec = _spec_with_custom_agent(
            "gone-agent", command=["bash", "--norc"], resume=["bash", "-c", "r"]
        )
        rows = svc.resolve_spec_custom_agents(spec, storage=get_storage())
        agent = next(a for a in spec.agents if a.kind == AgentKind.custom)
        # NEVER raises; snapshot untouched.
        assert agent.command == ["bash", "--norc"]
        assert agent.resume_command == ["bash", "-c", "r"]
        assert rows == {}

    def test_validator_rejects_missing_ref(self) -> None:
        spec = _spec_with_custom_agent("gone-agent", command=["bash"])
        with pytest.raises(FlowValidationError) as exc:
            validate_custom_agent_refs(spec, get_storage())
        assert exc.value.code == ERROR_CUSTOM_AGENT_NOT_FOUND

    def test_validator_accepts_command_not_on_path(self) -> None:
        # Existence-only gate: a registered row whose binary is missing still
        # passes save/trigger validation (the run surfaces the spawn failure).
        self._make_row(spawn_command=["no-such-binary-xyz"])
        spec = _spec_with_custom_agent("my-agent", command=["no-such-binary-xyz"])
        validate_custom_agent_refs(spec, get_storage())  # no exception

    def test_validator_accepts_live_row(self) -> None:
        self._make_row()
        spec = _spec_with_custom_agent("my-agent", command=["bash"])
        validate_custom_agent_refs(spec, get_storage())  # no exception


# ──────────────────────────────────────────────────────────────────────
# TmuxLiveSession custom branch
# ──────────────────────────────────────────────────────────────────────


class TestTmuxLiveCustom:
    def _agent(self, *, resume: list[str] | None = None) -> FlowAgent:
        return FlowAgent(
            id="my-agent",
            kind=AgentKind.custom,
            command=["mycli", "--yolo"],
            resume_command=resume,
            custom_agent_ref="my-agent",
            repo="/tmp/r",
            is_temporary=True,
        )

    def test_resume_command_used_when_present(self) -> None:
        s = TmuxLiveSession(
            agent=self._agent(resume=["mycli", "--yolo", "--continue"]),
            team_name="tm", run_id="r1", cli=object(),
        )
        assert s._spawn_cmd == ["mycli", "--yolo"]
        assert s._resume_cmd == ["mycli", "--yolo", "--continue"]

    def test_resume_falls_back_to_spawn(self) -> None:
        s = TmuxLiveSession(
            agent=self._agent(), team_name="tm", run_id="r1", cli=object(),
        )
        assert s._resume_cmd == ["mycli", "--yolo"]

    def test_skip_permissions_false_for_custom(self) -> None:
        # ClawTeam must never inject a permission flag for a custom command
        # (basename-keyed injection would be nondeterministic).
        s = TmuxLiveSession(
            agent=self._agent(), team_name="tm", run_id="r1", cli=object(),
        )
        assert s._skip_permissions() is False

    def test_ready_extra_pattern_stashed(self) -> None:
        import re

        pat = re.compile("READY>")
        s = TmuxLiveSession(
            agent=self._agent(), team_name="tm", run_id="r1", cli=object(),
            ready_extra_pattern=pat,
        )
        assert s._ready_extra_pattern is pat


@pytest.mark.asyncio
async def test_wait_tui_ready_honours_extra_pattern() -> None:
    import re

    async def capture(target: str) -> str:
        return "booting my custom cli\nREADY> \n"

    # Generic prompt patterns do NOT match this pane; the registered
    # ready_pattern does.
    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=0.3, poll_interval=0.01, capture=capture,
    )
    assert result.ok is False

    result = await tmux_ready.wait_tui_ready(
        "x:y", timeout_sec=1.0, poll_interval=0.01, capture=capture,
        extra_patterns=[re.compile(r"READY>")],
    )
    assert result.ok is True
    assert result.reason_code == "composer_ready"


# ──────────────────────────────────────────────────────────────────────
# Chat (SSE + stop + history + reset)
# ──────────────────────────────────────────────────────────────────────


def _sse_events(text: str) -> list[dict | str]:
    out: list[dict | str] = []
    for line in text.splitlines():
        if not line.startswith("data: "):
            continue
        payload = line[len("data: "):]
        out.append(payload if payload == "[DONE]" else json.loads(payload))
    return out


class TestChat:
    def _register_chatty(
        self, client: TestClient, tmp_path: Path, *, body: str | None = None
    ) -> dict:
        script = _write_script(
            tmp_path, "chat.sh", body or 'echo "reply to: $2"\n'
        )
        return _register(
            client,
            name="Chatty",
            spawnCommand="bash --norc",
            headlessCommand=f"{script} -p {{message}}",
            chatWorkdir=str(tmp_path),
        )

    def test_chat_turn_streams_reply_and_persists_history(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        created = self._register_chatty(client, tmp_path)
        r = client.post(
            f"/api/custom-agents/{created['id']}/chat",
            json={"message": "hello"},
        )
        assert r.status_code == 200, r.text
        events = _sse_events(r.text)
        assert events[-1] == "[DONE]"
        deltas = [e for e in events if isinstance(e, dict) and "delta" in e]
        assert deltas and deltas[-1]["delta"] == "reply to: hello"

        # Final answer persisted (detached task) — poll briefly.
        deadline = time.monotonic() + 5.0
        roles: list[str] = []
        while time.monotonic() < deadline:
            h = client.get(f"/api/custom-agents/{created['id']}/chat-history")
            roles = [m["role"] for m in h.json()["messages"]]
            if "assistant" in roles:
                break
            time.sleep(0.05)
        assert roles == ["user", "assistant"]

    def test_chat_error_when_cli_fails(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        created = self._register_chatty(
            client, tmp_path, body='echo "boom" >&2\nexit 3\n'
        )
        r = client.post(
            f"/api/custom-agents/{created['id']}/chat",
            json={"message": "hello"},
        )
        assert r.status_code == 200
        events = _sse_events(r.text)
        errors = [e for e in events if isinstance(e, dict) and "error" in e]
        assert errors and "boom" in errors[-1]["error"]

    def test_chat_missing_binary_reported_on_the_turn(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        # Not pre-gated by a PATH probe — the turn starts and the subprocess'
        # own "cannot start" error is streamed back.
        created = _register(
            client,
            name="Ghostly",
            spawnCommand="no-such-binary-xyz",
            headlessCommand="no-such-binary-xyz -p {message}",
            chatWorkdir=str(tmp_path),
        )
        r = client.post(
            f"/api/custom-agents/{created['id']}/chat", json={"message": "hi"},
        )
        assert r.status_code == 200
        errors = [
            e for e in _sse_events(r.text) if isinstance(e, dict) and "error" in e
        ]
        assert errors and "failed to start" in errors[-1]["error"]

    def test_chat_unconfigured_409(self, client: TestClient) -> None:
        created = _register(client, name="NoChat")
        r = client.post(
            f"/api/custom-agents/{created['id']}/chat", json={"message": "hi"},
        )
        assert r.status_code == 409
        assert r.json()["error"] == "CUSTOM_AGENT_CHAT_UNCONFIGURED"

    def test_stop_and_status(self, client: TestClient, tmp_path: Path) -> None:
        created = self._register_chatty(client, tmp_path)
        # No job yet → idle.
        st = client.get(f"/api/custom-agents/{created['id']}/chat/status")
        assert st.json()["status"] == "idle"
        # Stop with nothing in flight is a 204 no-op.
        assert (
            client.post(f"/api/custom-agents/{created['id']}/chat/stop").status_code
            == 204
        )

    def test_kill_chat_marks_job_cancelled(self, tmp_path: Path) -> None:
        # The appended message lands in $0 — the turn just sleeps.
        row = CustomAgent(
            id="slow", name="Slow", spawn_command=["bash"],
            headless_command=["sh", "-c", "sleep 30"],
            created_by_user=TEST_USER,
        )
        key = custom_agent_user_chat_session_id(TEST_USER, "slow")
        job = chat_svc.start_chat(
            row, message="x", workdir=str(tmp_path), resume=False, session_key=key,
        )
        deadline = time.monotonic() + 5.0
        while job.proc is None and time.monotonic() < deadline:
            time.sleep(0.02)
        assert chat_svc.kill_chat(key) is True
        assert chat_svc.get_job(key) is None
        snap = job.snapshot()
        assert snap["status"] == "error"

    def test_reset_appends_divider(self, client: TestClient, tmp_path: Path) -> None:
        created = self._register_chatty(client, tmp_path)
        client.post(f"/api/custom-agents/{created['id']}/chat", json={"message": "a"})
        assert (
            client.post(f"/api/custom-agents/{created['id']}/reset").status_code
            == 204
        )
        h = client.get(f"/api/custom-agents/{created['id']}/chat-history")
        kinds = [m.get("kind") for m in h.json()["messages"]]
        assert "session_divider" in kinds

    def test_resume_uses_continue_template_after_reply(
        self, client: TestClient, tmp_path: Path
    ) -> None:
        marker = tmp_path / "mode.log"
        fresh = _write_script(
            tmp_path, "fresh.sh", f'echo fresh >> "{marker}"\necho "r1"\n'
        )
        cont = _write_script(
            tmp_path, "cont.sh", f'echo cont >> "{marker}"\necho "r2"\n'
        )
        created = _register(
            client,
            name="Resumer",
            spawnCommand="bash --norc",
            headlessCommand=f"{fresh} {{message}}",
            headlessResumeCommand=f"{cont} {{message}}",
            chatWorkdir=str(tmp_path),
        )

        def _turn(msg: str) -> None:
            r = client.post(
                f"/api/custom-agents/{created['id']}/chat", json={"message": msg},
            )
            assert r.status_code == 200
            # Wait for the detached history-finalize task before the next turn.
            deadline = time.monotonic() + 5.0
            while time.monotonic() < deadline:
                h = client.get(f"/api/custom-agents/{created['id']}/chat-history")
                msgs = h.json()["messages"]
                if msgs and msgs[-1]["role"] == "assistant":
                    return
                time.sleep(0.05)
            raise AssertionError("assistant reply never persisted")

        _turn("first")
        _turn("second")
        modes = marker.read_text().split()
        assert modes == ["fresh", "cont"]

        # After a reset divider the next turn starts fresh again.
        client.post(f"/api/custom-agents/{created['id']}/reset")
        _turn("third")
        assert marker.read_text().split() == ["fresh", "cont", "fresh"]


class TestChatWorkdir:
    def test_resolve_default_home(self) -> None:
        row = CustomAgent(
            id="x", name="X", spawn_command=["bash"], created_by_user=TEST_USER,
        )
        assert chat_svc.resolve_chat_workdir(row) == str(Path("~").expanduser().resolve())

    def test_resolve_missing_dir_raises(self) -> None:
        row = CustomAgent(
            id="x", name="X", spawn_command=["bash"],
            chat_workdir="/definitely/not/here",
            created_by_user=TEST_USER,
        )
        with pytest.raises(svc.CustomAgentError):
            chat_svc.resolve_chat_workdir(row)
