"""Guards and helpers for L2/L3 tests that must never touch production data."""

from __future__ import annotations

import os
from pathlib import Path
from urllib.parse import urlparse

import pytest

_PROD_HOME = Path.home() / ".clawsomeflow"
_PROD_CLAWTEAM = Path.home() / ".clawteam"
_PROD_OPENCLAW = Path.home() / ".openclaw"
_TEST_HOME_PREFIX = Path.home() / ".clawsomeflow-test"
_DEFAULT_PROD_PORT = 17017
_DEFAULT_PROD_BOARD_PORT = 17018
_TEST_NAME_PREFIXES = ("e2e-flow-", "e2e-openclaw-", "runtime-e2e-")


def production_home() -> Path:
    return _PROD_HOME


def production_clawteam_dir() -> Path:
    return _PROD_CLAWTEAM


def production_openclaw_home() -> Path:
    return _PROD_OPENCLAW


def default_production_port() -> int:
    return int(os.environ.get("CSFLOW_PROD_PORT", str(_DEFAULT_PROD_PORT)))


def default_production_board_port() -> int:
    return int(os.environ.get("CSFLOW_PROD_BOARD_PORT", str(_DEFAULT_PROD_BOARD_PORT)))


def _realpath(path: str | Path) -> Path:
    return Path(path).expanduser().resolve()


def assert_isolated_csflow_home(home: str | Path) -> None:
    resolved = _realpath(home)
    if resolved == _realpath(_PROD_HOME):
        pytest.fail(
            "CSFLOW_HOME must not point to production data (~/.clawsomeflow); "
            f"got {resolved}"
        )
    if _TEST_HOME_PREFIX not in resolved.parents and resolved != _TEST_HOME_PREFIX:
        pytest.fail(
            "CSFLOW_HOME must live under ~/.clawsomeflow-test/<run-id>; "
            f"got {resolved}"
        )


def assert_isolated_clawteam_dir(path: str | Path) -> None:
    resolved = _realpath(path)
    if resolved == _realpath(_PROD_CLAWTEAM):
        pytest.fail(
            "CLAWTEAM_DATA_DIR must not point to production ~/.clawteam; "
            f"got {resolved}"
        )
    if _TEST_HOME_PREFIX not in resolved.parents and resolved != _TEST_HOME_PREFIX:
        pytest.fail(
            "CLAWTEAM_DATA_DIR must live under ~/.clawsomeflow-test/<run-id>; "
            f"got {resolved}"
        )


def assert_isolated_openclaw_home(path: str | Path) -> None:
    resolved = _realpath(path)
    if resolved == _realpath(_PROD_OPENCLAW):
        pytest.fail(
            "OPENCLAW_HOME must not point to production ~/.openclaw; "
            f"got {resolved}"
        )
    if _TEST_HOME_PREFIX not in resolved.parents and resolved != _TEST_HOME_PREFIX:
        pytest.fail(
            "OPENCLAW_HOME must live under ~/.clawsomeflow-test/<run-id>; "
            f"got {resolved}"
        )


def assert_test_service_name(service_name: str, *, prod_service_name: str = "csflow") -> None:
    if service_name == prod_service_name:
        pytest.fail("test systemd service name must differ from production csflow")
    if not service_name.startswith("csflow-test-"):
        pytest.fail(
            "test systemd service name must use csflow-test-<run-id> prefix; "
            f"got {service_name!r}"
        )


def assert_test_api_port(port: int) -> None:
    prod_port = default_production_port()
    prod_board = default_production_board_port()
    if port in {prod_port, prod_board}:
        pytest.fail(
            "test API/board ports must not reuse production ports "
            f"({prod_port}/{prod_board}); got {port}"
        )


def assert_test_api_base_url(base_url: str) -> None:
    """Mutating e2e/runtime tests must target the isolated test service, not prod."""
    parsed = urlparse(base_url)
    port = parsed.port
    if port is None:
        pytest.fail(f"test API base URL must include an explicit port: {base_url!r}")
    assert_test_api_port(port)


def is_test_artifact_name(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.startswith(prefix) for prefix in _TEST_NAME_PREFIXES)
