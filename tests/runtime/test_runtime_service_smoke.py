from __future__ import annotations

import os
import subprocess

import pytest

from tests.common.runtime_helpers import assert_http_ok, require_env

pytestmark = pytest.mark.runtime


def test_runtime_uses_isolated_namespace() -> None:
    service_name = require_env("CSFLOW_RUNTIME_SERVICE_NAME")
    prod_service_name = os.environ.get("CSFLOW_PROD_SERVICE_NAME", "csflow")
    csflow_home = require_env("CSFLOW_HOME")

    assert service_name != prod_service_name
    assert service_name.startswith("csflow-test-")
    assert "/.clawsomeflow-test/" in csflow_home
    assert os.environ.get("CSFLOW_DISABLE_BOARD") == "1"


def test_external_runtime_paths_are_isolated() -> None:
    clawteam_data_dir = require_env("CSFLOW_RUNTIME_CLAWTEAM_DATA_DIR")
    openclaw_home = require_env("CSFLOW_RUNTIME_OPENCLAW_HOME")
    assert "/.clawsomeflow-test/" in clawteam_data_dir
    assert "/.clawsomeflow-test/" in openclaw_home
    assert clawteam_data_dir != os.path.expanduser("~/.clawteam")
    assert openclaw_home != os.path.expanduser("~/.openclaw")


def test_runtime_systemd_service_is_active() -> None:
    service_name = require_env("CSFLOW_RUNTIME_SERVICE_NAME")
    proc = subprocess.run(
        ["systemctl", "--user", "is-active", service_name],
        check=False,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    assert proc.returncode == 0, proc.stderr.strip()
    assert proc.stdout.strip() == "active"


def test_runtime_healthcheck_ok() -> None:
    health_url = require_env("CSFLOW_RUNTIME_HEALTH_URL")
    assert_http_ok(health_url, retries=10, retry_interval_sec=0.5)
