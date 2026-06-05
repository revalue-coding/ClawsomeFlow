from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from tests.common.e2e_resources import E2EResourceTracker


@dataclass
class _FakeResponse:
    status_code: int
    _payload: dict[str, Any] | None = None

    def json(self) -> dict[str, Any]:
        return self._payload or {}


class _FakeClient:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []
        self.run_status = "running"

    def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("POST", url))
        return _FakeResponse(202)

    def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("GET", url))
        if "/runs/" in url:
            return _FakeResponse(200, {"status": self.run_status})
        return _FakeResponse(200, {})

    def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.calls.append(("DELETE", url))
        return _FakeResponse(204)


def test_e2e_resource_tracker_cleans_runs_before_flows() -> None:
    client = _FakeClient()
    tracker = E2EResourceTracker(
        flow_ids=["flow-1"],
        run_ids=["run-1"],
        agent_ids=["agent-1"],
    )
    client.run_status = "aborted"

    tracker.cleanup(client, timeout_sec=1.0)

    methods = [method for method, _ in client.calls]
    assert methods.index("POST") < methods.index("DELETE")
    assert ("POST", "/api/runs/run-1/abort") in client.calls
    assert ("DELETE", "/api/flows/flow-1") in client.calls
    assert ("DELETE", "/api/openclaw/agents/agent-1") in client.calls
