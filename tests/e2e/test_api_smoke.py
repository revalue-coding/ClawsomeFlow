from __future__ import annotations

import pytest

from tests.common.runtime_helpers import assert_http_ok, require_env

pytestmark = pytest.mark.e2e


def test_api_docs_endpoint_smoke() -> None:
    base_url = require_env("CSFLOW_E2E_BASE_URL").rstrip("/")
    assert_http_ok(f"{base_url}/docs")
