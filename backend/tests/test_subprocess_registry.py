"""Tests for the process-group registry used to guarantee no leftover
bootstrap/create subprocesses survive a graceful shutdown."""

from __future__ import annotations

import subprocess
import time

from app.services import subprocess_registry as reg


def _spawn_sleeper() -> subprocess.Popen:
    # Own process group (start_new_session) so kill_group can target the group.
    return subprocess.Popen(  # noqa: S603
        ["sleep", "30"], start_new_session=True,
    )


def test_terminate_all_kills_registered_group() -> None:
    proc = _spawn_sleeper()
    reg.register(proc)
    assert proc.poll() is None  # running

    killed = reg.terminate_all()
    assert killed >= 1
    # Reap and confirm it actually died.
    proc.wait(timeout=5)
    assert proc.poll() is not None


def test_unregister_excludes_from_sweep() -> None:
    proc = _spawn_sleeper()
    reg.register(proc)
    reg.unregister(proc)
    try:
        reg.terminate_all()  # should not target our proc
        time.sleep(0.2)
        assert proc.poll() is None  # still alive
    finally:
        reg.kill_group(proc)
        proc.wait(timeout=5)


def test_kill_group_returns_false_for_dead_proc() -> None:
    proc = _spawn_sleeper()
    reg.kill_group(proc)
    proc.wait(timeout=5)
    # Killing an already-dead group reports no signal delivered.
    assert reg.kill_group(proc) is False
