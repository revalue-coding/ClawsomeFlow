"""Operation-status query API (prefix ``/api``).

``GET /api/operations/{op_id}`` lets the frontend recover a long-running
create/install operation's outcome after a page refresh or tab close+reopen,
without polling. It layers four sources, first match wins:

1. **Registry** (:mod:`app.operations`) — authoritative while the process lives.
2. **Entity exists** — the created agent is now in storage → ``succeeded``
   (covers a terminal entry evicted past the cap, or lost to a restart).
3. **Service in-flight** — a create is still registering → ``running``.
4. Otherwise → ``not_found``.

Caveat (documented intentionally): OpenClaw's in-flight set does NOT span its
bootstrap, and the DB row is committed before bootstrap finishes. So after a
*backend restart mid-bootstrap*, layer 2 reports ``succeeded`` for an OpenClaw
agent whose bootstrap never committed. This is accepted under the "no
persistence / live-only" decision — the registry is authoritative while the
process runs, and the frontend treats entity-derived ``succeeded`` as "exists".
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.logging_setup import get_logger
from app.operations import get_op_registry
from app.services import hermes_agents as hermes_svc
from app.services import openclaw_agents as oc_svc
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/operations", tags=["operations"])
logger = get_logger("api.operations")


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


class OperationStatusResponse(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)

    op_id: str
    state: Literal["running", "succeeded", "failed", "not_found"]
    kind: str = ""
    detail: str = ""
    result: dict[str, Any] = Field(default_factory=dict)
    source: str = ""  # "registry" | "entity" | "in_flight" — observability only


def _entity_exists(kind: str, target: str, *, user: str, storage: StorageBackend) -> bool:
    """True iff the operation's target entity now exists and is owned by *user*."""
    if kind in ("hermes_create",):
        row = storage.hermes_get(target)
        return row is not None and (not row.created_by_user or row.created_by_user == user)
    if kind in ("openclaw_create", "openclaw_import"):
        row = storage.openclaw_get(target)
        return row is not None and (not row.created_by_user or row.created_by_user == user)
    # store_load yields multiple agents; success is not a single derivable entity,
    # so we rely on the registry for it (no entity fallback).
    return False


def _in_flight(kind: str, target: str) -> bool:
    if kind == "hermes_create":
        return hermes_svc.is_create_in_flight(target)
    if kind in ("openclaw_create", "openclaw_import"):
        return oc_svc.is_create_in_flight(target)
    return False


@router.get("/{op_id}", response_model=OperationStatusResponse)
async def get_operation_status(
    op_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> OperationStatusResponse:
    """Resolve an operation's current status via the 4-layer recovery above.

    ``async def`` deliberately: layer 3 reads OpenClaw's lock-free in-flight set,
    which is mutated on the event loop — keeping this handler on the loop avoids a
    cross-thread read from FastAPI's sync threadpool.
    """
    reg = get_op_registry()
    op = reg.get(op_id, user=user)
    if op is not None:
        return OperationStatusResponse(
            op_id=op_id, state=op.state, kind=op.kind,
            detail=op.detail, result=op.result, source="registry",
        )

    kind, _, target = op_id.partition(":")
    if target and _entity_exists(kind, target, user=user, storage=storage):
        return OperationStatusResponse(op_id=op_id, state="succeeded", kind=kind,
                                       detail="recovered", source="entity")
    if target and _in_flight(kind, target):
        return OperationStatusResponse(op_id=op_id, state="running", kind=kind, source="in_flight")
    return OperationStatusResponse(op_id=op_id, state="not_found", kind=kind)


__all__ = ["router", "OperationStatusResponse"]
