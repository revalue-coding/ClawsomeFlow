"""Track and tear down resources created by L2 e2e smoke tests."""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any, Protocol

_TERMINAL_RUN_STATUSES = frozenset(
    {
        "completed",
        "completed_with_conflicts",
        "complaint_failed",
        "failed",
        "aborted",
    }
)


class _HttpClient(Protocol):
    def post(self, url: str, **kwargs: Any) -> Any: ...
    def delete(self, url: str, **kwargs: Any) -> Any: ...
    def get(self, url: str, **kwargs: Any) -> Any: ...


@dataclass
class E2EResourceTracker:
    """Best-effort cleanup for flows/runs/agents created during e2e tests."""

    flow_ids: list[str] = field(default_factory=list)
    run_ids: list[str] = field(default_factory=list)
    agent_ids: list[str] = field(default_factory=list)

    def track_flow(self, flow_id: str) -> str:
        self.flow_ids.append(flow_id)
        return flow_id

    def track_run(self, run_id: str) -> str:
        self.run_ids.append(run_id)
        return run_id

    def track_agent(self, agent_id: str) -> str:
        self.agent_ids.append(agent_id)
        return agent_id

    def cleanup(self, client: _HttpClient, *, timeout_sec: float = 45.0) -> None:
        for run_id in reversed(self.run_ids):
            self._abort_run(client, run_id, timeout_sec=timeout_sec)
        for flow_id in reversed(self.flow_ids):
            self._delete_flow(client, flow_id, timeout_sec=timeout_sec)
        for agent_id in reversed(self.agent_ids):
            self._delete_agent(client, agent_id)

    def _abort_run(
        self,
        client: _HttpClient,
        run_id: str,
        *,
        timeout_sec: float,
    ) -> None:
        try:
            client.post(f"/api/runs/{run_id}/abort")
        except Exception:
            return
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                resp = client.get(f"/api/runs/{run_id}")
            except Exception:
                return
            if resp.status_code != 200:
                return
            status = resp.json().get("status")
            if status in _TERMINAL_RUN_STATUSES:
                return
            time.sleep(0.5)

    def _delete_flow(
        self,
        client: _HttpClient,
        flow_id: str,
        *,
        timeout_sec: float,
    ) -> None:
        deadline = time.monotonic() + timeout_sec
        while time.monotonic() < deadline:
            try:
                resp = client.delete(f"/api/flows/{flow_id}")
            except Exception:
                return
            if resp.status_code in {204, 404}:
                return
            if resp.status_code == 409:
                time.sleep(0.5)
                continue
            return

    def _delete_agent(self, client: _HttpClient, agent_id: str) -> None:
        try:
            client.delete(f"/api/openclaw/agents/{agent_id}")
        except Exception:
            return
