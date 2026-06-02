from __future__ import annotations

import math
import time
from urllib.request import urlopen

import pytest

from tests.common.runtime_helpers import require_env

pytestmark = [pytest.mark.perf, pytest.mark.slow]


def test_http_root_p95_latency_within_threshold() -> None:
    base_url = require_env("CSFLOW_PERF_BASE_URL").rstrip("/")
    sample_count = int(require_env("CSFLOW_PERF_SAMPLE_COUNT"))
    max_p95_ms = float(require_env("CSFLOW_PERF_MAX_P95_MS"))

    latencies_ms: list[float] = []
    for _ in range(sample_count):
        start = time.perf_counter()
        with urlopen(f"{base_url}/", timeout=8.0) as response:  # nosec B310
            assert response.status == 200
        latencies_ms.append((time.perf_counter() - start) * 1000.0)

    latencies_ms.sort()
    p95_index = max(0, math.ceil(len(latencies_ms) * 0.95) - 1)
    p95_ms = latencies_ms[p95_index]
    assert p95_ms <= max_p95_ms, (
        f"p95 latency {p95_ms:.2f}ms exceeds threshold {max_p95_ms:.2f}ms"
    )
