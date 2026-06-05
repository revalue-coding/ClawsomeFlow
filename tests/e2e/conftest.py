from __future__ import annotations

from collections.abc import AsyncIterator, Iterator

import httpx
import pytest

from tests.common.e2e_resources import E2EResourceTracker
from tests.common.isolation import assert_isolated_csflow_home, assert_test_api_base_url
from tests.common.runtime_helpers import require_env


@pytest.fixture
def e2e_resources() -> Iterator[E2EResourceTracker]:
    home = require_env("CSFLOW_HOME")
    assert_isolated_csflow_home(home)
    base_url = require_env("CSFLOW_E2E_BASE_URL").rstrip("/")
    assert_test_api_base_url(base_url)

    tracker = E2EResourceTracker()
    yield tracker
    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        tracker.cleanup(client)


@pytest.fixture
async def e2e_resources_async() -> AsyncIterator[E2EResourceTracker]:
    home = require_env("CSFLOW_HOME")
    assert_isolated_csflow_home(home)
    base_url = require_env("CSFLOW_E2E_BASE_URL").rstrip("/")
    assert_test_api_base_url(base_url)

    tracker = E2EResourceTracker()
    yield tracker
    with httpx.Client(base_url=base_url, timeout=20.0) as client:
        tracker.cleanup(client)
