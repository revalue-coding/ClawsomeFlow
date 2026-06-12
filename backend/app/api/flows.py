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
from sqlalchemy.exc import IntegrityError

from app.api._auth import current_user
from app.api.errors import ApiError
from app.logging_setup import get_logger
from app.models import AgentKind, Flow, FlowSpec, iso_utc
from app.storage import StorageBackend, StorageVersionConflict, get_storage
from app.validators import FlowValidationError, validate_flow_against_db

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
    # True when spec.variables["csflow.easy_mode"] is "true" (省心模式).
    easy_mode: bool = False


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
# Template import/export models (portable Flow definitions)
#
# A "template" is a Flow stripped of instance-only bookkeeping (owner,
# timestamps) but — by design — KEEPS ``id`` + ``version`` so an external
# service can pull a Flow, edit it, and write it back to the *same* Flow
# (upsert by id, optimistic-locked by version). See API.md "Flows" section.
# ──────────────────────────────────────────────────────────────────────

# Bump only on breaking changes to the envelope shape.
TEMPLATE_SCHEMA_VERSION = "1"


class FlowTemplateEntry(_CamelModel):
    """One Flow inside an export/import envelope."""

    # ``id`` is optional on import: present → upsert by id; absent → create new.
    id: str | None = None
    name: str
    description: str = ""
    cleanup_team_on_finish: bool = True
    # ``version`` drives optimistic locking on write-back. Optional on import
    # (missing version falls back to force-overwrite of the current row).
    version: int | None = None
    spec: FlowSpec


class FlowTemplate(_CamelModel):
    """Single-Flow export envelope."""

    clawsomeflow_template: str = TEMPLATE_SCHEMA_VERSION
    kind: str = "flow"
    flow: FlowTemplateEntry


class FlowCollectionTemplate(_CamelModel):
    """Multi-Flow (bulk) export envelope."""

    clawsomeflow_template: str = TEMPLATE_SCHEMA_VERSION
    kind: str = "flowCollection"
    flows: list[FlowTemplateEntry]


class FlowImportPayload(_CamelModel):
    """Import body: accepts either a single ``flow`` or a ``flows`` array."""

    clawsomeflow_template: str | None = None
    flow: FlowTemplateEntry | None = None
    flows: list[FlowTemplateEntry] | None = None


class FlowImportItemResult(_CamelModel):
    """Per-Flow outcome of an import (bulk-friendly: never raises mid-batch)."""

    id: str | None = None
    name: str = ""
    action: str  # "created" | "updated" | "error"
    version: int | None = None
    warnings: list[FlowSaveWarning] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None


class FlowImportResponse(_CamelModel):
    results: list[FlowImportItemResult]
    created: int = 0
    updated: int = 0
    failed: int = 0


# ──────────────────────────────────────────────────────────────────────
# Mappers (DB row → response model)
# ──────────────────────────────────────────────────────────────────────


def _spec_easy_mode(flow: Flow) -> bool:
    try:
        variables = (flow.spec or {}).get("variables") or {}
        if not isinstance(variables, dict):
            return False
        return str(variables.get("csflow.easy_mode", "")).strip().lower() == "true"
    except Exception:
        return False


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
        easy_mode=_spec_easy_mode(flow),
    )


def _to_detail(flow: Flow) -> FlowDetail:
    # Always re-serialize through FlowSpec so nested keys stay camelCase on wire.
    spec_payload = _serialize_spec(flow)
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


def _serialize_spec(flow: Flow) -> dict[str, Any]:
    """Re-serialize the stored spec through FlowSpec so nested keys stay
    camelCase on the wire. Shared by detail + template responses."""
    return FlowSpec.model_validate(flow.spec).model_dump(mode="json", by_alias=True)


def _to_template_entry(flow: Flow) -> FlowTemplateEntry:
    """DB row → portable template entry (keeps id + version for write-back)."""
    return FlowTemplateEntry(
        id=flow.id,
        name=flow.name,
        description=flow.description,
        cleanup_team_on_finish=flow.cleanup_team_on_finish,
        version=flow.version,
        spec=_serialize_spec(flow),
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


# NB: ``/export`` and ``/import`` are static segments and MUST be declared
# before the dynamic ``/{flow_id}`` route below, otherwise FastAPI would match
# them as a flow_id == "export" / "import".
@router.get("/export", response_model=FlowCollectionTemplate)
def export_flows(
    user: UserDep,
    storage: StorageDep,
    ids: Annotated[str | None, Query(description="Comma-separated flow ids; omit for all")] = None,
) -> FlowCollectionTemplate:
    """Bulk export. With ``?ids=a,b`` export that subset (strict: any missing or
    non-owned id → 404); without ``ids`` export all of the caller's Flows."""
    if ids is not None:
        wanted = [s.strip() for s in ids.split(",") if s.strip()]
        entries: list[FlowTemplateEntry] = []
        for fid in wanted:
            flow = storage.flow_get(fid)
            if flow is None:
                raise ApiError("NOT_FOUND", f"flow {fid!r} not found", status_code=404)
            _ensure_owner(flow, user)
            entries.append(_to_template_entry(flow))
        return FlowCollectionTemplate(flows=entries)

    # No filter → page through all of the user's flows.
    collected: list[Flow] = []
    offset = 0
    page = 200
    while True:
        flows, total = storage.flow_list(owner_user=user, q=None, limit=page, offset=offset)
        collected.extend(flows)
        offset += len(flows)
        if not flows or offset >= total:
            break
    return FlowCollectionTemplate(flows=[_to_template_entry(f) for f in collected])


@router.get("/{flow_id}/export", response_model=FlowTemplate)
def export_flow(
    flow_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
) -> FlowTemplate:
    """Export a single Flow as a portable template (keeps id + version)."""
    flow = storage.flow_get(flow_id)
    if flow is None:
        raise ApiError("NOT_FOUND", f"flow {flow_id!r} not found", status_code=404)
    _ensure_owner(flow, user)
    return FlowTemplate(flow=_to_template_entry(flow))


def _import_one(
    entry: FlowTemplateEntry,
    *,
    user: str,
    storage: StorageBackend,
    overwrite: bool,
) -> FlowImportItemResult:
    """Upsert a single template entry. Never raises for per-Flow business
    errors — returns an ``action="error"`` result instead, so a bulk import
    completes every entry independently."""
    try:
        _validate_flow_meta(description=entry.description)
        validate_flow_against_db(entry.spec, storage)
    except ApiError as exc:
        # _validate_flow_meta → ApiError (e.g. INVALID_FLOW_DESCRIPTION).
        return FlowImportItemResult(
            id=entry.id, name=entry.name, action="error",
            error_code=exc.code, error_message=exc.message,
        )
    except FlowValidationError as exc:
        # validate_flow_against_db → FlowValidationError (e.g. INVALID_DAG,
        # missing leader, bad refs). Keep it per-item so one bad entry doesn't
        # abort a bulk import (the global handler would otherwise 400 the batch).
        return FlowImportItemResult(
            id=entry.id, name=entry.name, action="error",
            error_code=exc.code, error_message=exc.message,
        )

    warnings = _collect_flow_save_warnings(entry.spec)

    # ── Upsert by id ───────────────────────────────────────────────────
    if entry.id:
        existing = storage.flow_get(entry.id)
        if existing is not None:
            if existing.owner_user != user:
                return FlowImportItemResult(
                    id=entry.id, name=entry.name, action="error",
                    error_code="FORBIDDEN",
                    error_message="flow belongs to a different user",
                )
            # overwrite=True OR no version supplied → force using current
            # version (last-write-wins). Otherwise honour optimistic locking.
            if overwrite or entry.version is None:
                expected = existing.version
            else:
                expected = entry.version
            existing.name = entry.name
            existing.description = entry.description
            # Product decision (mirrors create/update): always enable cleanup.
            existing.cleanup_team_on_finish = True
            existing.with_spec(entry.spec)
            try:
                updated = storage.flow_update(existing, expected_version=expected)
            except StorageVersionConflict as exc:
                return FlowImportItemResult(
                    id=entry.id, name=entry.name, action="error",
                    error_code="VERSION_CONFLICT",
                    error_message=(
                        f"version conflict (sent {expected}, current "
                        f"{exc.actual}); re-export and retry or set overwrite=true"
                    ),
                )
            return FlowImportItemResult(
                id=updated.id, name=updated.name, action="updated",
                version=updated.version, warnings=warnings,
            )

        # id supplied but row gone → recreate with the original id so any
        # external references stay valid.
        flow = Flow(
            id=entry.id,
            name=entry.name,
            description=entry.description,
            cleanup_team_on_finish=True,
            owner_user=user,
        ).with_spec(entry.spec)
    else:
        # No id → brand-new Flow with a freshly generated id.
        flow = Flow(
            name=entry.name,
            description=entry.description,
            cleanup_team_on_finish=True,
            owner_user=user,
        ).with_spec(entry.spec)

    try:
        saved = storage.flow_create(flow)
    except IntegrityError:
        return FlowImportItemResult(
            id=entry.id, name=entry.name, action="error",
            error_code="DUPLICATE",
            error_message="flow id already exists",
        )
    return FlowImportItemResult(
        id=saved.id, name=saved.name, action="created",
        version=saved.version, warnings=warnings,
    )


@router.post("/import", response_model=FlowImportResponse)
def import_flows(
    payload: FlowImportPayload,
    user: UserDep,
    storage: StorageDep,
    overwrite: Annotated[
        bool, Query(description="Force write-back ignoring version (last-write-wins)")
    ] = False,
) -> FlowImportResponse:
    """Import (upsert) one or many Flow templates.

    Body must carry exactly one of ``flow`` (single) or ``flows`` (bulk).
    Per-Flow failures are reported in ``results`` rather than aborting the
    whole batch; only a malformed envelope returns ``400``.
    """
    if (payload.flow is None) == (payload.flows is None):
        raise ApiError(
            "INVALID_IMPORT_PAYLOAD",
            "exactly one of 'flow' or 'flows' must be provided",
            status_code=400,
        )
    entries = [payload.flow] if payload.flow is not None else list(payload.flows or [])
    results = [
        _import_one(e, user=user, storage=storage, overwrite=overwrite) for e in entries
    ]
    return FlowImportResponse(
        results=results,
        created=sum(1 for r in results if r.action == "created"),
        updated=sum(1 for r in results if r.action == "updated"),
        failed=sum(1 for r in results if r.action == "error"),
    )


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
    # A running Run snapshots the spec at start but finalize re-reads the Flow
    # (merge advice / cleanup / version), so editing mid-run could affect it.
    # Refuse any edit while a Run is active (mirrors delete_flow).
    if storage.run_count_active_for_flow(flow_id) > 0:
        raise ApiError(
            "RUNS_IN_PROGRESS",
            f"flow {flow_id!r} has active runs; cannot edit",
            status_code=409,
        )
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
    blocking_schedules = [
        s
        for s in storage.run_schedule_list(user=user)
        if any((it or {}).get("flow_id") == flow_id for it in (s.items or []))
    ]
    if blocking_schedules:
        raise ApiError(
            "FLOW_HAS_SCHEDULES",
            f"flow {flow_id!r} is referenced by {len(blocking_schedules)} scheduled "
            "task(s); remove them first",
            status_code=409,
            details={"schedule_names": [s.name or s.id for s in blocking_schedules]},
        )
    try:
        storage.flow_delete(flow_id)
    except IntegrityError as exc:
        # Defensive race guard: a run may become active between pre-check and
        # the actual delete transaction.
        raise ApiError(
            "RUNS_IN_PROGRESS",
            f"flow {flow_id!r} has active runs; cannot delete",
            status_code=409,
        ) from exc
