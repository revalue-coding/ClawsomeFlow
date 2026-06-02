"""Tests for ``scripts/upgrade-user.sh`` channel selection behavior."""

from __future__ import annotations

import json
import os
import stat
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
UPGRADE_SCRIPT = REPO / "scripts" / "upgrade-user.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _build_fake_env(
    tmp_path: Path,
    *,
    pip_fail_pinned_only: bool = False,
    stable_versions: list[str] | None = None,
) -> tuple[dict[str, str], Path]:
    home_dir = tmp_path / "home"
    venv_bin = home_dir / ".clawsomeflow" / ".venv" / "bin"
    venv_bin.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _write_executable(
        venv_bin / "pip",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> "${CSFLOW_TEST_LOG_DIR:?}/pip.commands"
        if [[ "${CSFLOW_TEST_PIP_FAIL_PINNED:-0}" == "1" ]]; then
          for arg in "$@"; do
            if [[ "$arg" == clawsomeflow==* ]]; then
              echo "ERROR: Could not find a version that satisfies the requirement $arg" >&2
              exit 41
            fi
          done
        fi
        if [[ "${CSFLOW_TEST_PIP_FAIL:-0}" == "1" ]]; then
          exit 23
        fi
        exit 0
        """,
    )
    _write_executable(
        venv_bin / "csflow",
        """#!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "version" ]]; then
          echo "0.1.0"
          exit 0
        fi
        printf '%s\\n' "$*" >> "${CSFLOW_TEST_LOG_DIR:?}/csflow.commands"
        exit 0
        """,
    )
    _write_executable(
        venv_bin / "python",
        """#!/usr/bin/env bash
        set -euo pipefail
        exec python3 "$@"
        """,
    )

    if stable_versions is not None:
        pypi_json_path = tmp_path / "pypi.json"
        pypi_json_path.write_text(
            json.dumps({
                "releases": {v: [{"filename": f"{v}.whl"}] for v in stable_versions},
            }),
            encoding="utf-8",
        )
        pypi_json_url = pypi_json_path.as_uri()
    else:
        pypi_json_url = (tmp_path / "missing-pypi.json").as_uri()

    env = os.environ.copy()
    env.update(
        {
            "HOME": str(home_dir),
            "CSFLOW_HOME": str(home_dir / ".clawsomeflow"),
            "CSFLOW_TEST_LOG_DIR": str(log_dir),
            "CSFLOW_TEST_PIP_FAIL": "0",
            "CSFLOW_TEST_PIP_FAIL_PINNED": "1" if pip_fail_pinned_only else "0",
            "CSFLOW_PYPI_JSON_URL": pypi_json_url,
        }
    )
    return env, log_dir


def _run_upgrader(
    tmp_path: Path,
    *,
    args: list[str],
    pip_fail_pinned_only: bool = False,
    stable_versions: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    env, log_dir = _build_fake_env(
        tmp_path,
        pip_fail_pinned_only=pip_fail_pinned_only,
        stable_versions=stable_versions,
    )
    result = subprocess.run(
        ["bash", str(UPGRADE_SCRIPT), *args],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    pip_log = log_dir / "pip.commands"
    pip_commands = pip_log.read_text(encoding="utf-8").splitlines() if pip_log.exists() else []
    csflow_log = log_dir / "csflow.commands"
    csflow_commands = (
        csflow_log.read_text(encoding="utf-8").splitlines()
        if csflow_log.exists()
        else []
    )
    return result, pip_commands, csflow_commands


def _first_clawsomeflow_install_cmd(pip_commands: list[str]) -> str:
    for cmd in pip_commands:
        if "install" in cmd and "clawsomeflow" in cmd:
            return cmd
    raise AssertionError(f"missing clawsomeflow install command in: {pip_commands}")


def test_upgrade_user_defaults_to_stable_channel(tmp_path: Path) -> None:
    result, pip_commands, csflow_commands = _run_upgrader(tmp_path, args=[])
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd
    assert "clawsomeflow" in install_cmd
    assert "upgrade-runtime --restart-service" in csflow_commands


def test_upgrade_user_pre_flag_uses_prerelease_channel(tmp_path: Path) -> None:
    result, pip_commands, csflow_commands = _run_upgrader(tmp_path, args=["--pre"])
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" in install_cmd
    assert "clawsomeflow" in install_cmd
    assert "upgrade-runtime --restart-service" in csflow_commands


def test_upgrade_user_stable_channel_pins_latest_stable_when_metadata_available(
    tmp_path: Path,
) -> None:
    result, pip_commands, csflow_commands = _run_upgrader(
        tmp_path,
        args=[],
        stable_versions=["0.1.1", "0.1.2", "0.1.3b1"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd
    assert "clawsomeflow==0.1.2" in install_cmd
    assert "upgrade-runtime --restart-service" in csflow_commands


def test_upgrade_user_pinned_stable_failure_retries_quietly(tmp_path: Path) -> None:
    result, pip_commands, csflow_commands = _run_upgrader(
        tmp_path,
        args=[],
        pip_fail_pinned_only=True,
        stable_versions=["0.1.3"],
    )
    combined = result.stdout + result.stderr
    assert result.returncode == 0, combined
    assert "Could not find a version that satisfies" not in combined
    assert "Pinned stable artifact is not available yet" in combined
    assert any("clawsomeflow==0.1.3" in cmd for cmd in pip_commands)
    assert any(
        " clawsomeflow" in cmd and "clawsomeflow==" not in cmd and "--pre" not in cmd
        for cmd in pip_commands
    )
    assert "upgrade-runtime --restart-service" in csflow_commands


def test_upgrade_user_help_documents_cli_and_pep668_safety() -> None:
    result = subprocess.run(
        ["bash", str(UPGRADE_SCRIPT), "--help"],
        cwd=REPO,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    output = result.stdout + result.stderr
    assert "curl -fsSL https://clawsomeflow.com/upgrade.sh | bash" in output
    assert "bash -s -- --pre" in output
    assert "externally-managed-environment" in output
    assert "~/.clawsomeflow/.venv/bin/pip" in output
