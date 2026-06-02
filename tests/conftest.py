from __future__ import annotations

import os

import pytest

_MARKER_FLAGS = {
    "runtime": "CSFLOW_RUNTIME_TEST_ACTIVE",
    "e2e": "CSFLOW_E2E_TEST_ACTIVE",
    "perf": "CSFLOW_PERF_TEST_ACTIVE",
}


def _require_flag(name: str, expected: str = "1") -> None:
    if os.environ.get(name) != expected:
        pytest.skip(f"{name} != {expected!r}; skipping environment-bound test")


def pytest_runtest_setup(item: pytest.Item) -> None:
    for marker_name, flag_name in _MARKER_FLAGS.items():
        if item.get_closest_marker(marker_name) is not None:
            _require_flag(flag_name)
