"""ExternalNodeSession — the scheduler-side bypass for AgentKind.external.

An external execution node (human / webhook / remote ClawsomeFlow) has no
local process at all:

* ``_do_spawn`` / ``_do_resume`` / ``_do_shutdown`` are no-ops — the session
  moves Absent → Idle instantly, there is no tmux window, no worktree, and
  none of the spawn anti-loop invariants apply (nothing is spawned).
* ``_do_dispatch`` issues the one-time receipt ticket, persists the
  ``external_task_dispatched`` RunEvent (the WebUI todo card + the ticket
  validity record) and performs the channel-specific outbound notification
  (see :func:`app.services.external_tasks.dispatch_external_task`). An
  outbound failure raises, which the base class converts into a failed
  :class:`DispatchOutcome` — the controller leaves the task ``pending`` and
  retries next tick with a fresh nonce.

Completion never flows through this session: the /api/external receipt
endpoint (or the WebUI human path) writes ``task_update completed`` +
``mailbox_send`` directly, the controller mirrors it from the next snapshot
and calls ``mark_idle`` exactly as for any other worker.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from app.models import FlowAgent
from app.scheduler.sessions.base import WorkerSession

# Builds (message_text, structured_package) for one task id; bound to the
# RunController (it owns the DispatchContext composition).
PackageProvider = Callable[[str], Awaitable[dict[str, Any]]]


class ExternalNodeSession(WorkerSession):
    """No-process session driving an external executor via ticket + receipt."""

    def __init__(
        self,
        *,
        agent: FlowAgent,
        team_name: str,
        run_id: str,
        storage: Any,
        package_provider: PackageProvider,
    ) -> None:
        super().__init__(agent=agent, team_name=team_name, run_id=run_id)
        self._storage = storage
        self._package_provider = package_provider

    async def _do_spawn(self) -> None:
        # Nothing to bring up — external executors are always "reachable".
        return

    async def _do_dispatch(self, *, message: str, task_id: str) -> None:
        from app.services.external_tasks import dispatch_external_task

        package = await self._package_provider(task_id)
        await dispatch_external_task(
            storage=self._storage,
            run_id=self.run_id,
            team_name=self.team_name,
            agent=self.agent,
            task_id=task_id,
            message=message,
            package=package,
        )

    async def _do_resume(self) -> None:
        # No process to resume; a "crashed" external session (failure retry)
        # comes back through spawn(), which is a no-op anyway.
        return

    async def _do_shutdown(self) -> None:
        # Nothing to stop. Outstanding tickets die naturally: the receipt
        # endpoint rejects completions once the run is no longer active.
        return


__all__ = ["ExternalNodeSession"]
