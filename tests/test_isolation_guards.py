from __future__ import annotations

import pytest

from tests.common.isolation import (
    assert_isolated_clawteam_dir,
    assert_isolated_csflow_home,
    assert_isolated_openclaw_home,
    assert_test_api_base_url,
    assert_test_api_port,
    assert_test_service_name,
    is_test_artifact_name,
)


def test_is_test_artifact_name() -> None:
    assert is_test_artifact_name("e2e-flow-abc123")
    assert is_test_artifact_name("E2E-OPENCLAW-deadbeef")
    assert not is_test_artifact_name("my-production-flow")


def test_assert_isolated_csflow_home_rejects_production() -> None:
    with pytest.raises(pytest.fail.Exception, match="must not point to production"):
        assert_isolated_csflow_home("~/.clawsomeflow")


def test_assert_isolated_csflow_home_requires_test_prefix() -> None:
    with pytest.raises(pytest.fail.Exception, match="must live under ~/.clawsomeflow-test"):
        assert_isolated_csflow_home("/tmp/not-under-test-prefix")


def test_assert_test_api_port_rejects_production_ports() -> None:
    with pytest.raises(pytest.fail.Exception, match="must not reuse production ports"):
        assert_test_api_port(17017)


def test_assert_test_api_base_url_rejects_production_port() -> None:
    with pytest.raises(pytest.fail.Exception, match="must not reuse production ports"):
        assert_test_api_base_url("http://127.0.0.1:17017")


def test_assert_test_service_name_rejects_prod_name() -> None:
    with pytest.raises(pytest.fail.Exception, match="must differ from production"):
        assert_test_service_name("csflow", prod_service_name="csflow")


def test_external_paths_must_be_under_test_home() -> None:
    from pathlib import Path
    import shutil

    from tests.common.isolation import production_home

    home = production_home().parent / ".clawsomeflow-test" / "pytest-isolation-check"
    clawteam = home / ".clawteam"
    openclaw = home / ".openclaw"
    home.mkdir(parents=True, exist_ok=True)
    clawteam.mkdir(exist_ok=True)
    openclaw.mkdir(exist_ok=True)
    try:
        assert_isolated_csflow_home(home)
        assert_isolated_clawteam_dir(clawteam)
        assert_isolated_openclaw_home(openclaw)
    finally:
        shutil.rmtree(home, ignore_errors=True)
