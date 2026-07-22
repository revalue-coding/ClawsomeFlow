from __future__ import annotations

import plistlib
import shutil
import subprocess
from pathlib import Path

from app.cli import _user_service as svc


def _isolated_test_home(name: str) -> Path:
    home = Path.home() / ".clawsomeflow-test" / name
    home.mkdir(parents=True, exist_ok=True)
    return home


def test_ensure_user_service_file_writes_unit(
    monkeypatch, tmp_path: Path,
) -> None:
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _fake_require(name: str) -> str:
        if name == "systemctl":
            return "/usr/bin/systemctl"
        raise AssertionError(f"unexpected command lookup: {name}")

    monkeypatch.setattr(svc, "_require_command", _fake_require)
    monkeypatch.setattr(
        svc,
        "resolve_binary",
        lambda name: "/home/test/.local/bin/csflow" if name == "csflow" else None,
    )

    unit = svc.ensure_user_service_file(host="127.0.0.1", port=17017)
    assert unit.exists()
    text = unit.read_text()
    assert "Description=ClawsomeFlow backend (local mode)" in text
    assert (
        "ExecStart=/home/test/.local/bin/csflow serve --host 127.0.0.1 --port 17017"
        in text
    )
    # OOM guard: bias the killer away from the long-lived orchestrator.
    assert "OOMScoreAdjust=-800" in text


def test_restart_and_enable_runs_expected_systemctl_commands(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    # This test verifies the default-name ("csflow") command sequence; _run is
    # mocked so nothing real is touched. Bypass the prod-namespace guard, which
    # only exists to block real operations against the live unit.
    monkeypatch.setattr(svc, "_guard_service_namespace", lambda **_kw: None)
    monkeypatch.setattr(svc, "ensure_user_service_file", lambda **_kw: Path("/tmp/csflow.service"))
    monkeypatch.setattr(svc, "ensure_linger", lambda **_kw: None)
    monkeypatch.setattr(svc, "_cleanup_stale_port_conflicts", lambda _port: [])

    def _fake_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--user", "is-active", "csflow"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)

    svc.restart_and_enable(host="127.0.0.1", port=17017, non_interactive=True)

    assert calls == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", "csflow"],
        ["systemctl", "--user", "restart", "csflow"],
        ["systemctl", "--user", "is-active", "csflow"],
    ]


def test_stop_if_running_stops_active_service(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    monkeypatch.setattr(svc, "_guard_service_namespace", lambda **_kw: None)
    monkeypatch.setattr(svc, "_require_command", lambda _name: "/usr/bin/systemctl")

    def _fake_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--user", "is-active", "csflow"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)
    assert svc.stop_if_running() is True
    assert calls == [
        ["systemctl", "--user", "is-active", "csflow"],
        ["systemctl", "--user", "stop", "csflow"],
    ]


def test_service_name_override_is_used_in_systemctl_commands(monkeypatch) -> None:
    calls: list[list[str]] = []
    test_home = _isolated_test_home("pytest-service-override-123")
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    monkeypatch.setenv("CSFLOW_HOME", str(test_home))
    monkeypatch.setenv("CSFLOW_SERVICE_NAME", "csflow-test-123")
    monkeypatch.setattr(
        svc, "ensure_user_service_file", lambda **_kw: Path("/tmp/csflow-test.service")
    )
    monkeypatch.setattr(svc, "ensure_linger", lambda **_kw: None)
    monkeypatch.setattr(svc, "_cleanup_stale_port_conflicts", lambda _port: [])

    def _fake_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:4] == ["systemctl", "--user", "is-active", "csflow-test-123"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)
    try:
        svc.restart_and_enable(host="127.0.0.1", port=17017, non_interactive=True)
        assert calls == [
            ["systemctl", "--user", "daemon-reload"],
            ["systemctl", "--user", "enable", "csflow-test-123"],
            ["systemctl", "--user", "restart", "csflow-test-123"],
            ["systemctl", "--user", "is-active", "csflow-test-123"],
        ]
    finally:
        shutil.rmtree(test_home, ignore_errors=True)


def test_unit_text_includes_resource_directives_from_env(monkeypatch) -> None:
    monkeypatch.setenv("CSFLOW_SERVICE_CPU_QUOTA", "40%")
    monkeypatch.setenv("CSFLOW_SERVICE_MEMORY_MAX", "2G")
    text = svc._unit_text(
        csflow_bin="/home/test/.local/bin/csflow",
        host="127.0.0.1",
        port=17017,
    )
    assert "CPUQuota=40%" in text
    assert "MemoryMax=2G" in text


def test_unit_text_passes_through_runtime_isolation_env(monkeypatch) -> None:
    monkeypatch.setenv("CSFLOW_HOME", "/tmp/csflow-test-home")
    monkeypatch.setenv("CSFLOW_DISABLE_BOARD", "1")
    monkeypatch.setenv("CLAWTEAM_DATA_DIR", "/tmp/csflow-test-home/.clawteam")
    monkeypatch.setenv("OPENCLAW_HOME", "/tmp/csflow-test-home/.openclaw")
    text = svc._unit_text(
        csflow_bin="/home/test/.local/bin/csflow",
        host="127.0.0.1",
        port=17017,
    )
    assert 'Environment="CSFLOW_HOME=/tmp/csflow-test-home"' in text
    assert 'Environment="CSFLOW_DISABLE_BOARD=1"' in text
    assert (
        'Environment="CLAWTEAM_DATA_DIR=/tmp/csflow-test-home/.clawteam"' in text
    )
    assert 'Environment="OPENCLAW_HOME=/tmp/csflow-test-home/.openclaw"' in text


def test_ensure_launchd_service_file_writes_plist(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "launchd")
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)

    def _fake_require(name: str) -> str:
        if name == "launchctl":
            return "/bin/launchctl"
        raise AssertionError(f"unexpected command lookup: {name}")

    monkeypatch.setattr(svc, "_require_command", _fake_require)
    monkeypatch.setattr(
        svc,
        "resolve_binary",
        lambda name: "/Users/test/.local/bin/csflow" if name == "csflow" else None,
    )

    plist_path = svc.ensure_user_service_file(host="127.0.0.1", port=17017)
    assert plist_path.exists()
    assert plist_path.suffix == ".plist"
    data = plistlib.loads(plist_path.read_bytes())
    assert data["Label"] == "dev.clawsomeflow.csflow"
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is True
    assert data["ProgramArguments"] == [
        "/Users/test/.local/bin/csflow",
        "serve",
        "--host",
        "127.0.0.1",
        "--port",
        "17017",
    ]


def test_restart_and_enable_runs_expected_launchctl_commands(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setattr(svc, "_guard_service_namespace", lambda **_kw: None)
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "launchd")
    monkeypatch.setenv("CSFLOW_LAUNCHD_LABEL", "dev.clawsomeflow.csflow-test")
    monkeypatch.setattr(svc.os, "getuid", lambda: 501)
    monkeypatch.setattr(
        svc, "ensure_user_service_file", lambda **_kw: Path("/tmp/csflow-test.plist")
    )
    monkeypatch.setattr(svc, "_require_command", lambda _name: "/bin/launchctl")

    def _fake_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:2] == ["launchctl", "print"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="state = running\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)
    svc.restart_and_enable(host="127.0.0.1", port=17017, non_interactive=True)

    target = "gui/501/dev.clawsomeflow.csflow-test"
    fallback_target = "user/501/dev.clawsomeflow.csflow-test"
    assert calls == [
        ["launchctl", "bootout", target],
        ["launchctl", "bootout", fallback_target],
        ["launchctl", "unload", "/tmp/csflow-test.plist"],
        ["launchctl", "bootstrap", "gui/501", "/tmp/csflow-test.plist"],
        ["launchctl", "enable", target],
        ["launchctl", "kickstart", "-k", target],
        ["launchctl", "print", target],
    ]


def test_stop_if_running_stops_active_launchd_service(monkeypatch) -> None:
    calls: list[list[str]] = []
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "launchd")
    monkeypatch.setenv("CSFLOW_LAUNCHD_LABEL", "dev.clawsomeflow.csflow-stop")
    monkeypatch.setattr(svc.os, "getuid", lambda: 502)
    monkeypatch.setattr(svc, "_require_command", lambda _name: "/bin/launchctl")

    def _fake_run(cmd: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["launchctl", "list", "dev.clawsomeflow.csflow-stop"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)
    assert svc.stop_if_running() is True
    assert calls == [
        ["launchctl", "list", "dev.clawsomeflow.csflow-stop"],
        ["launchctl", "bootout", "gui/502/dev.clawsomeflow.csflow-stop"],
    ]


def test_cleanup_stale_port_conflicts_skips_managed_csflow_serve_listeners(
    monkeypatch,
) -> None:
    monkeypatch.setattr(svc, "_listening_pids_for_port", lambda _port: [501, 502])
    monkeypatch.setattr(svc, "_pid_owned_by_current_user", lambda _pid: True)
    monkeypatch.setattr(
        svc,
        "_pid_cmdline",
        lambda pid: (
            "python3 -m uvicorn app.main:app --port 17017"
            if pid == 501
            else "python3 -m uvicorn app.main:app --reload --port 17017"
        ),
    )
    monkeypatch.setattr(svc, "_is_managed_csflow_listener", lambda pid: pid == 501)
    monkeypatch.setattr(svc, "_descendant_pids", lambda _pid: [])
    killed: list[int] = []
    monkeypatch.setattr(
        svc,
        "_terminate_pid",
        lambda pid, grace_seconds=8.0: (killed.append(pid), True)[1],
    )

    reclaimed = svc._cleanup_stale_port_conflicts(17017)
    assert reclaimed == [502]
    assert killed == [502]


def test_stop_if_running_rejects_prod_service_with_pytest_home(
    monkeypatch,
) -> None:
    """Backend pytest uses a tmp CSFLOW_HOME — must not stop the live ``csflow`` unit."""
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    monkeypatch.setenv("CSFLOW_HOME", "/tmp/pytest-csflow-home")
    monkeypatch.delenv("CSFLOW_SERVICE_NAME", raising=False)
    try:
        svc.stop_if_running()
    except svc.ServiceError as exc:
        assert "Refusing to stop production-managed service" in str(exc)
        assert "non-production path" in str(exc)
    else:
        raise AssertionError("expected ServiceError")


def test_restart_and_enable_rejects_prod_service_with_isolated_home(
    monkeypatch,
) -> None:
    test_home = _isolated_test_home("pytest-reject-prod-restart")
    monkeypatch.setenv("CSFLOW_HOME", str(test_home))
    monkeypatch.delenv("CSFLOW_SERVICE_NAME", raising=False)

    try:
        try:
            svc.restart_and_enable(host="127.0.0.1", port=27117, non_interactive=True)
        except svc.ServiceError as exc:
            assert "Refusing to restart production-managed service" in str(exc)
        else:
            raise AssertionError("expected ServiceError")
    finally:
        shutil.rmtree(test_home, ignore_errors=True)


def test_cleanup_stale_port_conflicts_kills_only_matching_user_owned_pids(
    monkeypatch,
) -> None:
    monkeypatch.setattr(svc, "_is_managed_csflow_listener", lambda _pid: False)
    monkeypatch.setattr(svc, "_listening_pids_for_port", lambda _port: [101, 202, 303])
    monkeypatch.setattr(
        svc,
        "_pid_owned_by_current_user",
        lambda pid: pid != 303,
    )
    monkeypatch.setattr(
        svc,
        "_pid_cmdline",
        lambda pid: {
            101: "python3 -m uvicorn app.main:app --reload --port 17017",
            202: "python3 -m http.server 17017",
            303: "python3 -m uvicorn app.main:app --reload --port 17017",
        }[pid],
    )
    monkeypatch.setattr(svc, "_descendant_pids", lambda _pid: [])
    killed: list[int] = []
    monkeypatch.setattr(
        svc,
        "_terminate_pid",
        lambda pid, grace_seconds=8.0: (killed.append(pid), True)[1],
    )

    reclaimed = svc._cleanup_stale_port_conflicts(17017)
    assert reclaimed == [101]
    assert killed == [101]


def test_cleanup_stale_port_conflicts_reaps_descendant_tree(monkeypatch) -> None:
    """A stale uvicorn supervisor and its detached children (reload worker,
    clawteam-mcp that inherited the socket) are all reclaimed."""
    monkeypatch.setattr(svc, "_is_managed_csflow_listener", lambda _pid: False)
    monkeypatch.setattr(svc, "_listening_pids_for_port", lambda _port: [100, 130])
    monkeypatch.setattr(svc, "_pid_owned_by_current_user", lambda _pid: True)
    monkeypatch.setattr(
        svc,
        "_pid_cmdline",
        lambda pid: (
            "python3 -m uvicorn app.main:app --reload --port 17017"
            if pid == 100
            else "python3 .../clawteam-mcp"
        ),
    )
    # pid 100 (supervisor) → worker 110 → clawteam-mcp 130 (also a listener).
    descendants = {100: [110, 130], 110: [130]}
    monkeypatch.setattr(svc, "_descendant_pids", lambda pid: descendants.get(pid, []))
    killed: list[int] = []
    monkeypatch.setattr(
        svc,
        "_terminate_pid",
        lambda pid, grace_seconds=8.0: (killed.append(pid), True)[1],
    )

    reclaimed = svc._cleanup_stale_port_conflicts(17017)
    # Supervisor first, then its descendants; pid 130 reaped once (deduped).
    assert killed == [100, 110, 130]
    assert reclaimed == [100, 110, 130]


def test_reclaim_stale_port_listeners_delegates_to_cleanup(monkeypatch) -> None:
    seen: list[int] = []

    def _fake_cleanup(port: int) -> list[int]:
        seen.append(port)
        return [4242]

    monkeypatch.setattr(svc, "_cleanup_stale_port_conflicts", _fake_cleanup)
    assert svc.reclaim_stale_port_listeners(17017) == [4242]
    assert seen == [17017]


def test_restart_and_enable_retries_after_reclaiming_conflicted_port(
    monkeypatch,
) -> None:
    calls: list[list[str]] = []
    cleanup_calls: list[int] = []
    monkeypatch.setenv("CSFLOW_SERVICE_MANAGER", "systemd")
    monkeypatch.setattr(svc, "_guard_service_namespace", lambda **_kw: None)
    monkeypatch.setattr(
        svc, "ensure_user_service_file", lambda **_kw: Path("/tmp/csflow.service")
    )
    monkeypatch.setattr(svc, "ensure_linger", lambda **_kw: None)

    def _fake_cleanup(port: int) -> list[int]:
        cleanup_calls.append(port)
        # 1st: pre-cleanup before restart. 2nd: after first restart failure.
        return [] if len(cleanup_calls) == 1 else [9999]

    monkeypatch.setattr(svc, "_cleanup_stale_port_conflicts", _fake_cleanup)
    monkeypatch.setattr(
        svc,
        "_describe_port_listeners",
        lambda _port: ["pid=9999 cmd=python3 -m uvicorn app.main:app --port 17017"],
    )

    restart_attempt = {"n": 0}

    def _fake_run(
        cmd: list[str], *, check: bool = True
    ) -> subprocess.CompletedProcess[str]:
        calls.append(cmd)
        if cmd[:3] == ["systemctl", "--user", "restart"]:
            restart_attempt["n"] += 1
            if restart_attempt["n"] == 1:
                return subprocess.CompletedProcess(
                    cmd, 1, stdout="", stderr="Address already in use"
                )
            return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")
        if cmd[:4] == ["systemctl", "--user", "is-active", "csflow"]:
            return subprocess.CompletedProcess(cmd, 0, stdout="active\n", stderr="")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(svc, "_run", _fake_run)
    svc.restart_and_enable(host="127.0.0.1", port=17017, non_interactive=True)

    assert restart_attempt["n"] == 2
    assert cleanup_calls == [17017, 17017]


def test_runtime_environment_path_prefers_runtime_bin(monkeypatch) -> None:
    monkeypatch.setenv("PATH", "/usr/bin:/bin")
    env_map = svc._runtime_environment_map(runtime_bin_dir="/opt/csflow/bin")
    assert env_map["PATH"].split(":")[0] == "/opt/csflow/bin"

