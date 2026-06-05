from __future__ import annotations

import os

import pytest

from tests.common.isolation import (
    assert_isolated_clawteam_dir,
    assert_isolated_csflow_home,
    assert_isolated_openclaw_home,
    assert_test_api_base_url,
    assert_test_api_port,
    assert_test_service_name,
)

_MARKER_FLAGS = {
    "runtime": "CSFLOW_RUNTIME_TEST_ACTIVE",
    "e2e": "CSFLOW_E2E_TEST_ACTIVE",
    "perf": "CSFLOW_PERF_TEST_ACTIVE",
}


def _require_flag(name: str, expected: str = "1") -> None:
    if os.environ.get(name) != expected:
        pytest.skip(f"{name} != {expected!r}; skipping environment-bound test")


def _enforce_isolated_namespace(marker_name: str) -> None:
    home = os.environ.get("CSFLOW_HOME")
    if home:
        assert_isolated_csflow_home(home)

    clawteam_data_dir = os.environ.get("CSFLOW_RUNTIME_CLAWTEAM_DATA_DIR") or os.environ.get(
        "CLAWTEAM_DATA_DIR"
    )
    if clawteam_data_dir:
        assert_isolated_clawteam_dir(clawteam_data_dir)

    openclaw_home = os.environ.get("CSFLOW_RUNTIME_OPENCLAW_HOME") or os.environ.get(
        "OPENCLAW_HOME"
    )
    if openclaw_home:
        assert_isolated_openclaw_home(openclaw_home)

    if marker_name in {"runtime", "e2e"}:
        service_name = os.environ.get("CSFLOW_RUNTIME_SERVICE_NAME") or os.environ.get(
            "CSFLOW_SERVICE_NAME"
        )
        if service_name:
            prod_service_name = os.environ.get("CSFLOW_PROD_SERVICE_NAME", "csflow")
            assert_test_service_name(service_name, prod_service_name=prod_service_name)

    if marker_name == "e2e":
        base_url = os.environ.get("CSFLOW_E2E_BASE_URL")
        if base_url:
            assert_test_api_base_url(base_url.rstrip("/"))

    if marker_name == "runtime":
        health_url = os.environ.get("CSFLOW_RUNTIME_HEALTH_URL")
        if health_url:
            from urllib.parse import urlparse

            port = urlparse(health_url).port
            if port is not None:
                assert_test_api_port(port)

    if marker_name == "perf":
        base_url = os.environ.get("CSFLOW_PERF_BASE_URL")
        if base_url:
            from urllib.parse import urlparse

            port = urlparse(base_url).port
            if port is not None:
                assert_test_api_port(port)


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker_name, flag_name in _MARKER_FLAGS.items():
        if item.get_closest_marker(marker_name) is not None:
            _require_flag(flag_name)
            _enforce_isolated_namespace(marker_name)
