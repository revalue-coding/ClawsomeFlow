"""The OpenClaw create/bootstrap CLI turn must kill the whole process group on
cancel (not just the parent), so the create stops writing artifacts immediately
and cancel-create converges in ~1s instead of ~20s."""

from __future__ import annotations

import asyncio

import pytest

from app.api import openclaw_agents as mod
from app.api.errors import ApiError


@pytest.mark.asyncio
async def test_cancel_create_kills_process_group(monkeypatch: pytest.MonkeyPatch) -> None:
    agent_id = "killgrp"
    killed: list[object] = []

    class FakeProc:
        pid = 4242

        def __init__(self) -> None:
            self._done = asyncio.Event()

        async def communicate(self):
            await self._done.wait()
            return b"", b""

        def kill(self) -> None:
            self._done.set()

    # Arm a (currently unset) cancellation event so the create branch runs.
    event = mod._register_agent_create_cancellation(agent_id)

    async def fake_exec(*_a, **_k):
        # Simulate the cancel arriving right after spawn.
        event.set()
        return FakeProc()

    def fake_kill_group(proc, **_k):
        killed.append(proc)
        proc.kill()  # unblock communicate() like a real group kill would
        return True

    monkeypatch.setattr(mod, "_resolve_openclaw_executable", lambda: "openclaw")
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(mod._subproc_registry, "kill_group", fake_kill_group)

    try:
        with pytest.raises(ApiError) as ei:
            await mod._chat_completion_via_cli(
                agent_id=agent_id,
                session_key=f"user-chat-alice-{agent_id}-bootstrap-1",
                message="bootstrap",
                model_override=None,
                timeout_sec=5.0,
            )
        assert ei.value.code == "AGENT_CREATE_CANCELLED"
        assert killed, "kill_group must be called to kill the whole process group"
    finally:
        mod._unregister_agent_create_cancellation(agent_id=agent_id, event=event)
