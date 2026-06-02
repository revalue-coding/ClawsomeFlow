"""Flows CRUD router (per API.md "Flows" section).

Endpoints:
* ``GET    /api/flows``               — list (with q / pagination filters)
* ``POST   /api/flows``               — create
* ``GET    /api/flows/{flow_id}``     — fetch
* ``PUT    /api/flows/{flow_id}``     — update (optimistic locking via version)
* ``DELETE /api/flows/{flow_id}``     — delete (rejects if active runs)

Optimistic locking: client must send the current ``version``; server bumps it
on success. Mismatch returns ``409 VERSION_CONFLICT``.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Path, Query
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.logging_setup import get_logger
from app.models import AgentKind, Flow, FlowSpec, iso_utc
from app.storage import StorageBackend, get_storage
from app.validators import validate_flow_against_db

router = APIRouter(prefix="/flows", tags=["flows"])
logger = get_logger("api.flows")


# ──────────────────────────────────────────────────────────────────────
# Request / response models
# ──────────────────────────────────────────────────────────────────────


class _CamelModel(BaseModel):
    """API-facing model: serialises with camelCase aliases."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
    )


class FlowSummary(_CamelModel):
    id: str
    name: str
    description: str
    version: int
    owner_user: str
    updated_at: str
    # Distinct agent kinds present in this Flow's spec, in the order they
    # appear. Retained for API compatibility; the current list UI no longer
    # renders these as Flow-level badges (agent source is per-task).
    agent_kinds: list[str] = []
    # Leader summary so the list view can render "who summarises this Flow"
    # without re-fetching the full spec. Both fields are optional because
    # legacy rows may have been saved before the leader contract was
    # enforced; FE renders a dash in that case.
    leader_agent_id: str | None = None
    leader_kind: str | None = None


class FlowDetail(_CamelModel):
    id: str
    name: str
    description: str
    version: int
    cleanup_team_on_finish: bool
    spec: dict[str, Any]
    owner_user: str
    created_at: str
    updated_at: str


class FlowListResponse(_CamelModel):
    items: list[FlowSummary]
    total: int


class FlowCreatePayload(_CamelModel):
    name: str
    description: str = ""
    cleanup_team_on_finish: bool = True
    spec: FlowSpec


class FlowUpdatePayload(_CamelModel):
    version: int = Field(..., description="Current version (for optimistic locking)")
    name: str
    description: str = ""
    cleanup_team_on_finish: bool = True
    spec: FlowSpec


class FlowCreateResponse(_CamelModel):
    id: str
    version: int
    warnings: list["FlowSaveWarning"] = Field(default_factory=list)


class FlowSaveWarning(_CamelModel):
    code: str
    message: str
    details: dict[str, Any] = Field(default_factory=dict)


# ──────────────────────────────────────────────────────────────────────
# Mappers (DB row → response model)
# ──────────────────────────────────────────────────────────────────────


def _to_summary(flow: Flow) -> FlowSummary:
    # Cheapest path: read agent kinds + leader straight out of the JSON-
    # stored spec without re-validating through the strict FlowSpec model
    # (some legacy rows may not round-trip cleanly). Failure → empty fields.
    kinds: list[str] = []
    leader_id: str | None = None
    leader_kind: str | None = None
    try:
        raw_agents = (flow.spec or {}).get("agents") or []
        seen: set[str] = set()
        for a in raw_agents:
            if not isinstance(a, dict):
                continue
            k = a.get("kind")
            if isinstance(k, str) and k and k not in seen:
                seen.add(k)
                kinds.append(k)
            # Accept both camelCase (API/JSON in newer rows) and snake_case
            # (legacy DB rows written before the FE switched to camel).
            is_leader = bool(a.get("isLeader") or a.get("is_leader"))
            if is_leader and leader_id is None:
                lid = a.get("id")
                if isinstance(lid, str) and lid:
                    leader_id = lid
                    leader_kind = k if isinstance(k, str) else None
    except Exception:
        kinds = []
        leader_id = None
        leader_kind = None
    return FlowSummary(
        id=flow.id,
        name=flow.name,
        description=flow.description,
        version=flow.version,
        owner_user=flow.owner_user,
        updated_at=iso_utc(flow.updated_at),
        agent_kinds=kinds,
        leader_agent_id=leader_id,
        leader_kind=leader_kind,
    )


def _to_detail(flow: Flow) -> FlowDetail:
    # Always re-serialize through FlowSpec so nested keys stay camelCase on wire.
    spec_payload = FlowSpec.model_validate(flow.spec).model_dump(
        mode="json",
        by_alias=True,
    )
    return FlowDetail(
        id=flow.id,
        name=flow.name,
        description=flow.description,
        version=flow.version,
        cleanup_team_on_finish=flow.cleanup_team_on_finish,
        spec=spec_payload,
        owner_user=flow.owner_user,
        created_at=iso_utc(flow.created_at),
        updated_at=iso_utc(flow.updated_at),
    )


def _ensure_owner(flow: Flow, user: str) -> None:
    if flow.owner_user != user:
        raise ApiError(
            "FORBIDDEN",
            "flow belongs to a different user",
            status_code=403,
        )


def _validate_flow_meta(*, description: str) -> None:
    if not (description or "").strip():
        raise ApiError(
            "INVALID_FLOW_DESCRIPTION",
            "flow overall goal (description) cannot be empty",
            status_code=400,
        )


def _collect_flow_save_warnings(spec: FlowSpec) -> list[FlowSaveWarning]:
    openclaw_agent_ids = [a.id for a in spec.agents if a.kind == AgentKind.openclaw]
    if not openclaw_agent_ids:
        return []
    try:
        from app.services.openclaw_agents import probe_runtime_running

        running, reason = probe_runtime_running()
    except Exception as exc:  # pragma: no cover - defensive guard
        logger.warning("flow_save_openclaw_probe_failed", error=str(exc))
        return []
    if running:
        return []
    return [
        FlowSaveWarning(
            code="OPENCLAW_RUNTIME_NOT_RUNNING",
            message=(
                "OpenClaw service is not running right now. "
                "The Flow has been saved, but OpenClaw tasks may fail until "
                "the runtime service is started."
            ),
            details={"reason": reason, "agentIds": openclaw_agent_ids},
        )
    ]


# ──────────────────────────────────────────────────────────────────────
# Routes
# ──────────────────────────────────────────────────────────────────────


# Annotated dependency aliases (FastAPI's preferred 0.95+ style; less ambiguous
# than ``= Depends(...)`` defaults when mixing Body / Path / Query).
#
# NB: ``Depends`` introspects the callable's signature so any kwargs become
# implicit endpoint params. We wrap ``get_storage`` in a zero-arg lambda so its
# ``config: Config | None = None`` parameter doesn't leak into the OpenAPI body
# schema (FastAPI would otherwise synthesise a "Body_*" wrapper).
def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]


@router.get("", response_model=FlowListResponse)
def list_flows(
    user: UserDep,
    storage: StorageDep,
    q: Annotated[str | None, Query()] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> FlowListResponse:
    flows, total = storage.flow_list(owner_user=user, q=q, limit=limit, offset=offset)
    return FlowListResponse(items=[_to_summary(f) for f in flows], total=total)


@router.post("", response_model=FlowCreateResponse, status_code=201)
def create_flow(
    payload: FlowCreatePayload,
    user: UserDep,
    storage: StorageDep,
) -> FlowCreateResponse:
    _validate_flow_meta(description=payload.description)
    # FlowSpec was already structurally validated by Pydantic (FlowAgent /
    # FlowTask field validators); now run the business invariants.
    validate_flow_against_db(payload.spec, storage)
    flow = Flow(
        name=payload.name,
        description=payload.description,
        # Product decision: always enable team cleanup after run finishes.
        # Ignore client-provided value for forward compatibility.
        cleanup_team_on_finish=True,
        owner_user=user,
    ).with_spec(payload.spec)
    saved = storage.flow_create(flow)
    warnings = _collect_flow_save_warnings(payload.spec)
    return FlowCreateResponse(id=saved.id, version=saved.version, warnings=warnings)


@router.get("/{flow_id}", response_model=FlowDetail)
def get_flow(
    flow_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> FlowDetail:
    flow = storage.flow_get(flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {flow_id!r} not found", status_code=404)
    _ensure_owner(flow, user)
    return _to_detail(flow)


@router.put("/{flow_id}", response_model=FlowCreateResponse)
def update_flow(
    flow_id: Annotated[str, Path()],
    payload: FlowUpdatePayload,
    user: UserDep,
    storage: StorageDep,
) -> FlowCreateResponse:
    existing = storage.flow_get(flow_id)
    if existing is None:
        raise ApiError("NOT_FOUND", f"flow {flow_id!r} not found", status_code=404)
    _ensure_owner(existing, user)
    _validate_flow_meta(description=payload.description)
    # Re-validate against DB (OpenClaw refs / repo paths might have changed).
    validate_flow_against_db(payload.spec, storage)
    existing.name = payload.name
    existing.description = payload.description
    # Product decision: always enable team cleanup after run finishes.
    existing.cleanup_team_on_finish = True
    existing.with_spec(payload.spec)
    updated = storage.flow_update(existing, expected_version=payload.version)
    warnings = _collect_flow_save_warnings(payload.spec)
    return FlowCreateResponse(id=updated.id, version=updated.version, warnings=warnings)


@router.delete("/{flow_id}", status_code=204)
def delete_flow(
    flow_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> None:
    flow = storage.flow_get(flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {flow_id!r} not found", status_code=404)
    _ensure_owner(flow, user)
    if storage.run_count_active_for_flow(flow_id) > 0:
        raise ApiError(
            "RUNS_IN_PROGRESS",
            f"flow {flow_id!r} has active runs; cannot delete",
            status_code=409,
        )
    storage.flow_delete(flow_id)
