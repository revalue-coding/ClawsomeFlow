from __future__ import annotations

import os
import time
from urllib.request import urlopen

import pytest


def require_env(name: str) -> str:
    value = os.environ.get(name)
    if not value:
        pytest.skip(f"missing environment variable: {name}")
    return value


def require_flag(name: str, expected: str = "1") -> None:
    current = os.environ.get(name)
    if current != expected:
        pytest.skip(f"{name} != {expected!r}; skipping environment-bound test")


def assert_http_ok(
    url: str,
    timeout: float = 8.0,
    *,
    retries: int = 1,
    retry_interval_sec: float = 0.5,
) -> None:
    last_error: Exception | None = None
    for attempt in range(max(1, retries)):
        try:
            with urlopen(url, timeout=timeout) as response:  # nosec B310
                assert response.status == 200
                return
        except Exception as exc:  # pragma: no cover - retry path
            last_error = exc
            if attempt + 1 < max(1, retries):
                time.sleep(retry_interval_sec)
    if last_error is not None:
        raise last_error
