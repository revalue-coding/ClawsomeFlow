"""Tests for ``scripts/install-user.sh`` end-user install behavior."""

from __future__ import annotations

import json
import os
import socket
import stat
import subprocess
import textwrap
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
INSTALL_SCRIPT = REPO / "scripts" / "install-user.sh"


def _write_executable(path: Path, content: str) -> None:
    path.write_text(textwrap.dedent(content), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def _pick_free_local_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _build_fake_env(
    tmp_path: Path,
    *,
    pip_fail: bool,
    pip_fail_pinned_only: bool,
    existing_deployment: bool,
    pip_candidate_version: str,
    stable_versions: list[str] | None = None,
) -> tuple[dict[str, str], Path]:
    home_dir = tmp_path / "home"
    home_dir.mkdir(parents=True, exist_ok=True)
    if existing_deployment:
        data_home = home_dir / ".clawsomeflow"
        data_home.mkdir(parents=True, exist_ok=True)
        (data_home / "config.json").write_text("{}", encoding="utf-8")
        (data_home / ".csflow-version").write_text("0.0.1\n", encoding="utf-8")
    fake_bin = tmp_path / "fake-bin"
    fake_bin.mkdir(parents=True, exist_ok=True)
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)

    _write_executable(
        fake_bin / "python3.11",
        """#!/usr/bin/env bash
        set -euo pipefail
        log_dir="${CSFLOW_TEST_LOG_DIR:?}"
        fake_bin="${CSFLOW_TEST_FAKE_BIN:?}"
        if [[ "${1:-}" == "-m" && "${2:-}" == "venv" ]]; then
          venv_dir="${3:?}"
          mkdir -p "${venv_dir}/bin"
          ln -sf "${fake_bin}/python3.11" "${venv_dir}/bin/python"
          ln -sf "${fake_bin}/csflow" "${venv_dir}/bin/csflow"
          ln -sf "${fake_bin}/clawsomeflow" "${venv_dir}/bin/clawsomeflow"
          ln -sf "${fake_bin}/clawteam" "${venv_dir}/bin/clawteam"
          cat > "${venv_dir}/bin/pip" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
log_dir="${CSFLOW_TEST_LOG_DIR:?}"
printf '%s\\n' "$*" >> "${log_dir}/pip.commands"
report_path=""
prev=""
for arg in "$@"; do
  if [[ "$prev" == "--report" ]]; then
    report_path="$arg"
    break
  fi
  prev="$arg"
done
if [[ -n "$report_path" ]]; then
  candidate="${CSFLOW_TEST_PIP_CANDIDATE_VERSION:-0.1.0}"
  cat > "$report_path" <<EOF2
{"install":[{"metadata":{"name":"clawsomeflow","version":"${candidate}"}}]}
EOF2
fi
is_dry_run=0
for arg in "$@"; do
  if [[ "$arg" == "--dry-run" ]]; then
    is_dry_run=1
    break
  fi
done
if [[ "${1:-}" == "install" && "${is_dry_run}" == "0" ]]; then
  if [[ "${CSFLOW_TEST_PIP_FAIL_PINNED:-0}" == "1" ]]; then
    for arg in "$@"; do
      if [[ "$arg" == clawsomeflow==* ]]; then
        echo "ERROR: Could not find a version that satisfies the requirement $arg" >&2
        exit 41
      fi
    done
  fi
  if [[ "${CSFLOW_TEST_PIP_FAIL:-0}" == "1" ]]; then
    for arg in "$@"; do
      if [[ "$arg" == "clawsomeflow" ]]; then
        exit 23
      fi
      if [[ "$arg" == clawsomeflow==* ]]; then
        exit 23
      fi
    done
  fi
fi
exit 0
EOF
          chmod +x "${venv_dir}/bin/pip"
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "pip" ]]; then
          shift 2
          printf '%s\\n' "$*" >> "${log_dir}/pip.commands"
          if [[ "${1:-}" == "--version" ]]; then
            exit 0
          fi
          if [[ "${1:-}" == "install" ]]; then
            if [[ "${CSFLOW_TEST_PIP_FAIL:-0}" == "1" ]]; then
              for arg in "$@"; do
                if [[ "$arg" == "clawsomeflow" ]]; then
                  exit 23
                fi
              done
            fi
            exit 0
          fi
          exit 0
        fi
        if [[ "${1:-}" == "-m" && "${2:-}" == "ensurepip" ]]; then
          exit 0
        fi
        exec python3 "$@"
        """,
    )
    _write_executable(
        fake_bin / "clawteam",
        """#!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "runtime" && "${2:-}" == "--help" ]]; then
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "csflow",
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
        fake_bin / "clawsomeflow",
        """#!/usr/bin/env bash
        set -euo pipefail
        exec "${CSFLOW_TEST_FAKE_BIN:?}/csflow" "$@"
        """,
    )
    _write_executable(
        fake_bin / "openclaw",
        """#!/usr/bin/env bash
        set -euo pipefail
        if [[ "${1:-}" == "--version" ]]; then
          echo "OpenClaw 2026.5.18"
          exit 0
        fi
        if [[ "${1:-}" == "health" && "${2:-}" == "--json" ]]; then
          echo '{"ok": true}'
          exit 0
        fi
        if [[ "${1:-}" == "gateway" && "${2:-}" == "start" ]]; then
          exit 0
        fi
        if [[ "${1:-}" == "daemon" && "${2:-}" == "start" ]]; then
          exit 0
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "systemctl",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> "${CSFLOW_TEST_LOG_DIR:?}/systemctl.commands"
        if [[ "${1:-}" == "--user" && "${2:-}" == "--no-pager" ]]; then
          echo "● csflow.service - fake"
          echo "   Active: active (running)"
        fi
        exit 0
        """,
    )
    _write_executable(
        fake_bin / "curl",
        """#!/usr/bin/env bash
        set -euo pipefail
        printf '%s\\n' "$*" >> "${CSFLOW_TEST_LOG_DIR:?}/curl.commands"
        exit 0
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
            "PATH": f"{fake_bin}:{env.get('PATH', '')}",
            "CSFLOW_HOME": str(home_dir / ".clawsomeflow"),
            "CSFLOW_PORT": str(_pick_free_local_port()),
            "CSFLOW_TEST_LOG_DIR": str(log_dir),
            "CSFLOW_TEST_PIP_FAIL": "1" if pip_fail else "0",
            "CSFLOW_TEST_PIP_FAIL_PINNED": "1" if pip_fail_pinned_only else "0",
            "CSFLOW_TEST_PIP_CANDIDATE_VERSION": pip_candidate_version,
            "CSFLOW_TEST_FAKE_BIN": str(fake_bin),
            "CSFLOW_PYPI_JSON_URL": pypi_json_url,
        }
    )
    return env, log_dir


def _run_installer(
    tmp_path: Path,
    *,
    args: list[str],
    pip_fail: bool = False,
    pip_fail_pinned_only: bool = False,
    pip_candidate_version: str = "0.1.0",
    via_stdin: bool = False,
    existing_deployment: bool = False,
    stable_versions: list[str] | None = None,
) -> tuple[subprocess.CompletedProcess[str], list[str], list[str]]:
    env, log_dir = _build_fake_env(
        tmp_path,
        pip_fail=pip_fail,
        pip_fail_pinned_only=pip_fail_pinned_only,
        existing_deployment=existing_deployment,
        pip_candidate_version=pip_candidate_version,
        stable_versions=stable_versions,
    )
    base_args = ["--yes", "--skip-linger", *args]
    if via_stdin:
        cmd = ["bash", "-s", "--", *base_args]
        result = subprocess.run(
            cmd,
            input=INSTALL_SCRIPT.read_text(encoding="utf-8"),
            cwd=REPO,
            env=env,
            text=True,
            capture_output=True,
            check=False,
        )
    else:
        cmd = ["bash", str(INSTALL_SCRIPT), *base_args]
        result = subprocess.run(
            cmd,
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


def test_install_user_defaults_to_stable_channel(tmp_path: Path) -> None:
    result, pip_commands, _ = _run_installer(tmp_path, args=[])
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd
    assert "clawsomeflow" in install_cmd


def test_install_user_pre_flag_uses_prerelease_channel(tmp_path: Path) -> None:
    result, pip_commands, _ = _run_installer(tmp_path, args=["--pre"])
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" in install_cmd
    assert "clawsomeflow" in install_cmd


def test_install_user_pypi_failure_has_no_local_source_fallback(tmp_path: Path) -> None:
    result, _, _ = _run_installer(tmp_path, args=[], pip_fail=True)
    combined = result.stdout + result.stderr
    assert result.returncode != 0
    assert "Failed to install clawsomeflow from PyPI" in combined
    assert "falling back to local source" not in combined


def test_install_user_stable_channel_skips_candidate_gating_and_upgrades(
    tmp_path: Path,
) -> None:
    result, pip_commands, csflow_commands = _run_installer(
        tmp_path,
        args=[],
        existing_deployment=True,
        pip_candidate_version="0.1.1b15",
    )
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd
    assert "clawsomeflow==" not in install_cmd
    assert not any("--dry-run" in cmd for cmd in pip_commands)
    assert "upgrade-runtime --yes --no-restart-service" in csflow_commands


def test_install_user_supports_remote_pipe_execution(tmp_path: Path) -> None:
    result, pip_commands, _ = _run_installer(tmp_path, args=[], via_stdin=True)
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd


def test_install_user_first_time_uses_install_pipeline(tmp_path: Path) -> None:
    result, _, csflow_commands = _run_installer(
        tmp_path, args=[], existing_deployment=False
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "install --yes --no-restart-service" in csflow_commands
    assert not any(cmd.startswith("upgrade ") for cmd in csflow_commands)


def test_install_user_existing_deployment_uses_upgrade_without_force(
    tmp_path: Path,
) -> None:
    result, _, csflow_commands = _run_installer(
        tmp_path, args=[], existing_deployment=True
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "upgrade-runtime --yes --no-restart-service" in csflow_commands
    assert not any(cmd.startswith("install ") for cmd in csflow_commands)
    assert not any(
        "--force" in cmd for cmd in csflow_commands if cmd.startswith("upgrade ")
    )


def test_install_user_stable_channel_pins_latest_stable_when_metadata_available(
    tmp_path: Path,
) -> None:
    result, pip_commands, _ = _run_installer(
        tmp_path,
        args=[],
        stable_versions=["0.1.1", "0.1.2", "0.1.3b1"],
    )
    assert result.returncode == 0, result.stdout + result.stderr
    install_cmd = _first_clawsomeflow_install_cmd(pip_commands)
    assert "--pre" not in install_cmd
    assert "clawsomeflow==0.1.2" in install_cmd


def test_install_user_pinned_stable_failure_retries_quietly(
    tmp_path: Path,
) -> None:
    result, pip_commands, _ = _run_installer(
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


def test_install_user_help_documents_upgrade_cli_and_pep668_safety() -> None:
    result = subprocess.run(
        ["bash", str(INSTALL_SCRIPT), "--help"],
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
