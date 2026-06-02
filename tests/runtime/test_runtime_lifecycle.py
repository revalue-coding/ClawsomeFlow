from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from urllib.parse import urlparse

import pytest

from tests.common.runtime_helpers import assert_http_ok, require_env

pytestmark = pytest.mark.runtime


def _run(cmd: list[str]) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=os.environ.copy(),
    )


def _runtime_ports() -> tuple[int, int]:
    cfg_path = Path(require_env("CSFLOW_HOME")) / "config.json"
    payload = cfg_path.read_text(encoding="utf-8")
    cfg = json.loads(payload)
    health_port = urlparse(require_env("CSFLOW_RUNTIME_HEALTH_URL")).port
    if health_port is None:
        raise AssertionError("invalid CSFLOW_RUNTIME_HEALTH_URL: missing port")
    board_port = int(cfg["clawteam_board_port"])
    return int(health_port), board_port


def _service_main_pid(service_name: str) -> int:
    proc = _run(
        [
            "systemctl",
            "--user",
            "show",
            service_name,
            "--property",
            "MainPID",
            "--value",
        ]
    )
    assert proc.returncode == 0, proc.stderr.strip()
    return int(proc.stdout.strip() or "0")


def test_runtime_install_idempotent_marker_stable() -> None:
    marker_path = Path(require_env("CSFLOW_HOME")) / ".csflow-version"
    assert marker_path.exists(), f"missing marker file: {marker_path}"
    marker_before = marker_path.read_text(encoding="utf-8").strip()

    port, board_port = _runtime_ports()
    cmd = [
        sys.executable,
        "-m",
        "app.cli",
        "install",
        "--skip-openclaw",
        "--no-restart-service",
        "--port",
        str(port),
        "--board-port",
        str(board_port),
    ]

    first = _run(cmd)
    assert first.returncode == 0, first.stdout + first.stderr
    marker_after_first = marker_path.read_text(encoding="utf-8").strip()
    assert marker_after_first == marker_before

    second = _run(cmd)
    assert second.returncode == 0, second.stdout + second.stderr
    marker_after_second = marker_path.read_text(encoding="utf-8").strip()
    assert marker_after_second == marker_before


def test_runtime_restart_keeps_service_healthy() -> None:
    service_name = require_env("CSFLOW_RUNTIME_SERVICE_NAME")
    health_url = require_env("CSFLOW_RUNTIME_HEALTH_URL")
    pid_before = _service_main_pid(service_name)
    assert pid_before > 0

    restart = _run(["systemctl", "--user", "restart", service_name])
    assert restart.returncode == 0, restart.stderr.strip()

    active = _run(["systemctl", "--user", "is-active", service_name])
    assert active.returncode == 0, active.stderr.strip()
    assert active.stdout.strip() == "active"

    pid_after = _service_main_pid(service_name)
    assert pid_after > 0
    assert pid_after != pid_before
    assert_http_ok(health_url, retries=20, retry_interval_sec=0.5)


def test_runtime_start_recovers_after_stop() -> None:
    service_name = require_env("CSFLOW_RUNTIME_SERVICE_NAME")
    health_url = require_env("CSFLOW_RUNTIME_HEALTH_URL")
    port, board_port = _runtime_ports()

    stop = _run(["systemctl", "--user", "stop", service_name])
    assert stop.returncode == 0, stop.stderr.strip()

    start = _run(
        [
            sys.executable,
            "-m",
            "app.cli",
            "start",
            "--yes",
            "--skip-deps",
            "--port",
            str(port),
            "--board-port",
            str(board_port),
        ]
    )
    assert start.returncode == 0, start.stdout + start.stderr

    active = _run(["systemctl", "--user", "is-active", service_name])
    assert active.returncode == 0, active.stderr.strip()
    assert active.stdout.strip() == "active"
    assert_http_ok(health_url, retries=20, retry_interval_sec=0.5)
