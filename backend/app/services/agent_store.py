"""Agent Store application service.

Store is managed by an external Store Server:

- Catalog is publicly browsable.
- Join/Purchase/Owned/Manifest depend on Store account token.
- Local install still happens in ClawsomeFlow via ``commit_agent``.
"""

from __future__ import annotations

import asyncio
import io
import os
import shutil
import tarfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

from app import paths
from app.config import Config, load_config
from app.integrations import openclaw_json as oj
from app.integrations.github_store_repo import (
    StoreAgentDefinition,
    StoreCatalogListing,
    StoreListingType,
    StorePricingMode,
    StoreTeamManifest,
)
from app.integrations.store_server import (
    StoreAcquireResult,
    StoreAuthActionResult,
    StoreAuthAccount,
    StoreAuthLoginResult,
    StoreOwnedListing,
    StoreServerClient,
    StoreServerError,
    StoreWorkspaceBundle,
    load_store_server_spec_from_env,
)
from app.logging_setup import get_logger
from app.services import openclaw_agents as openclaw_svc
from app.storage import StorageBackend, get_storage

logger = get_logger("svc.agent_store")

_DEFAULT_CATALOG_CACHE_TTL_SECONDS = 60
_CATALOG_CACHE: tuple[float, list[StoreCatalogListing]] | None = None
_CATALOG_CACHE_LOCK = asyncio.Lock()


class AgentStoreError(Exception):
    code: str
    status_code: int
    details: dict[str, Any]

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 400,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class OwnedListingView:
    listing_id: str
    type: StoreListingType
    title: str
    description: str
    avatar_url: str
    pricing_mode: StorePricingMode
    pricing_currency: str
    pricing_amount: float
    acquired_via: str
    acquired_at: datetime
    source_manifest_path: str


@dataclass(frozen=True)
class ListingLoadResult:
    listing_id: str
    listing_type: StoreListingType
    loaded_agent_ids: list[str]
    team_id: str | None


def _catalog_cache_ttl_seconds() -> int:
    raw = os.getenv("CSFLOW_AGENT_STORE_CATALOG_TTL_SECONDS", "").strip()
    if not raw:
        return _DEFAULT_CATALOG_CACHE_TTL_SECONDS
    try:
        value = int(raw)
    except ValueError:
        return _DEFAULT_CATALOG_CACHE_TTL_SECONDS
    return max(value, 0)


def _now_ts() -> float:
    return time.monotonic()


def _server_client() -> StoreServerClient:
    return StoreServerClient(load_store_server_spec_from_env())


def _map_store_server_error(exc: StoreServerError) -> AgentStoreError:
    return AgentStoreError(
        code=exc.code,
        message=exc.message,
        status_code=exc.status_code,
        details=exc.details,
    )


def _parse_remote_acquired_at(raw: str | None) -> datetime:
    if not raw:
        return datetime.now(timezone.utc)
    text = raw.strip()
    if not text:
        return datetime.now(timezone.utc)
    try:
        if text.endswith("Z"):
            text = text[:-1] + "+00:00"
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            return dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return datetime.now(timezone.utc)


def _to_owned_view(item: StoreOwnedListing) -> OwnedListingView:
    return OwnedListingView(
        listing_id=item.listing_id,
        type=item.type,
        title=item.title,
        description=item.description,
        avatar_url=item.avatar_url,
        pricing_mode=item.pricing.mode,
        pricing_currency=item.pricing.currency,
        pricing_amount=item.pricing.amount,
        acquired_via=item.acquired_via,
        acquired_at=_parse_remote_acquired_at(item.acquired_at),
        source_manifest_path=item.source_manifest_path,
    )


def _resolve_conflict_reason(*, agent_id: str, storage: StorageBackend, cfg: Config) -> dict[str, str] | None:
    if storage.openclaw_get(agent_id) is not None:
        return {
            "agentId": agent_id,
            "reasonCode": "AGENT_ID_EXISTS_DB",
            "message": f"agent id {agent_id!r} already exists in ClawsomeFlow registry",
        }
    if oj.find_agent(agent_id, cfg) is not None:
        return {
            "agentId": agent_id,
            "reasonCode": "AGENT_ID_EXISTS_RUNTIME",
            "message": f"agent id {agent_id!r} already exists in openclaw runtime",
        }
    try:
        agent_home = paths.agent_dir(agent_id)
        if agent_home.exists():
            # A leftover workspace dir without DB/runtime registration is an
            # orphan from previous failed install; auto-clean it so retry works.
            try:
                shutil.rmtree(agent_home)
                logger.warning(
                    "agent_store_orphan_workspace_removed",
                    agent_id=agent_id,
                    path=str(agent_home),
                )
                return None
            except Exception as exc:
                logger.warning(
                    "agent_store_orphan_workspace_remove_failed",
                    agent_id=agent_id,
                    path=str(agent_home),
                    error=str(exc),
                )
            return {
                "agentId": agent_id,
                "reasonCode": "AGENT_ID_EXISTS_LOCAL_DIR",
                "message": f"agent workspace directory already exists for id {agent_id!r}",
            }
    except ValueError:
        return {
            "agentId": agent_id,
            "reasonCode": "INVALID_AGENT_ID",
            "message": f"agent id {agent_id!r} is invalid",
        }
    return None


def _filter_available_extra_skills(extra_skills: list[str], *, listing_id: str, agent_id: str) -> tuple[str, ...]:
    source_root = paths.skills_source_dir()
    if not source_root.exists():
        logger.warning(
            "agent_store_skills_source_missing",
            listing_id=listing_id,
            agent_id=agent_id,
            source_root=str(source_root),
        )
        return tuple()
    available: list[str] = []
    missing: list[str] = []
    seen: set[str] = set()
    for raw in extra_skills:
        rel = (raw or "").strip()
        if not rel or rel in seen:
            continue
        seen.add(rel)
        if (source_root / rel).is_dir():
            available.append(rel)
        else:
            missing.append(rel)
    if missing:
        logger.warning(
            "agent_store_missing_skills_skipped",
            listing_id=listing_id,
            agent_id=agent_id,
            missing=missing,
            source_root=str(source_root),
        )
    return tuple(available)


def _to_commit_input(defn: StoreAgentDefinition, *, listing_id: str) -> openclaw_svc.CommitInput:
    prompt = (defn.nl_prompt or "").strip()
    if not prompt:
        prompt = f"[installed from Agent Store listing {listing_id}]"
    return openclaw_svc.CommitInput(
        id=defn.id,
        name=defn.name,
        description=defn.description,
        identity=openclaw_svc.AgentIdentity(
            emoji=defn.identity_emoji,
            theme=defn.identity_theme,
        ),
        model=defn.model,
        nl_prompt=prompt,
        extra_skills=_filter_available_extra_skills(defn.extra_skills, listing_id=listing_id, agent_id=defn.id),
    )


def _bundle_source_root_for_agent(
    *,
    bundle: StoreWorkspaceBundle,
    listing_type: StoreListingType,
    agent_id: str,
) -> str:
    if agent_id in bundle.agent_paths:
        return bundle.agent_paths[agent_id].strip() or "."
    if listing_type == StoreListingType.single:
        if bundle.agent_paths:
            # Single listing fallback: use the first entry if id mapping is absent.
            first = next(iter(bundle.agent_paths.values()), ".")
            return str(first).strip() or "."
        return "."
    raise AgentStoreError(
        "STORE_BUNDLE_AGENT_PATH_MISSING",
        f"workspace bundle missing path mapping for agent {agent_id!r}",
        status_code=502,
        details={"agentId": agent_id, "knownMappings": bundle.agent_paths},
    )


def _overlay_workspace_from_bundle(
    *,
    bundle_blob: bytes,
    source_root: str,
    target_workspace: Path,
) -> None:
    src = PurePosixPath((source_root or ".").strip())
    if src.is_absolute() or ".." in src.parts:
        raise AgentStoreError(
            "STORE_BUNDLE_PATH_INVALID",
            "workspace bundle source path is invalid",
            status_code=502,
            details={"sourceRoot": source_root},
        )
    src_parts = tuple(p for p in src.parts if p not in {"", "."})
    target_root = target_workspace.resolve(strict=False)
    copied = 0
    with tarfile.open(fileobj=io.BytesIO(bundle_blob), mode="r:gz") as tf:
        for member in tf.getmembers():
            if not member.isfile():
                continue
            rel = PurePosixPath(member.name)
            if rel.is_absolute() or ".." in rel.parts:
                continue
            if src_parts:
                if len(rel.parts) <= len(src_parts):
                    continue
                if tuple(rel.parts[: len(src_parts)]) != src_parts:
                    continue
                write_rel = PurePosixPath(*rel.parts[len(src_parts) :])
            else:
                write_rel = rel
            if not write_rel.parts:
                continue
            dst = (target_root / Path(*write_rel.parts)).resolve(strict=False)
            if dst != target_root and target_root not in dst.parents:
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            fh = tf.extractfile(member)
            if fh is None:
                continue
            dst.write_bytes(fh.read())
            copied += 1
    if copied == 0:
        raise AgentStoreError(
            "STORE_BUNDLE_EMPTY_FOR_AGENT",
            "workspace bundle has no files for target agent path",
            status_code=502,
            details={"sourceRoot": source_root},
        )


async def _load_catalog(*, force_refresh: bool = False) -> list[StoreCatalogListing]:
    global _CATALOG_CACHE
    ttl = _catalog_cache_ttl_seconds()
    now = _now_ts()
    if not force_refresh and _CATALOG_CACHE is not None:
        ts, cached = _CATALOG_CACHE
        if ttl > 0 and now - ts <= ttl:
            return cached
    async with _CATALOG_CACHE_LOCK:
        if not force_refresh and _CATALOG_CACHE is not None:
            ts, cached = _CATALOG_CACHE
            if ttl > 0 and now - ts <= ttl:
                return cached
        try:
            rows = await _server_client().list_catalog()
        except StoreServerError as exc:
            raise _map_store_server_error(exc) from exc
        _CATALOG_CACHE = (now, rows)
        return rows


async def list_catalog(
    *,
    listing_type: StoreListingType | None = None,
    force_refresh: bool = False,
) -> list[StoreCatalogListing]:
    rows = await _load_catalog(force_refresh=force_refresh)
    if listing_type is None:
        return rows
    return [row for row in rows if row.type == listing_type]


async def get_listing(listing_id: str) -> StoreCatalogListing:
    items = await list_catalog()
    for item in items:
        if item.listing_id == listing_id:
            return item
    # Newly imported listings might not be visible in in-memory cache yet.
    refreshed = await list_catalog(force_refresh=True)
    for item in refreshed:
        if item.listing_id == listing_id:
            return item
    raise AgentStoreError(
        "STORE_LISTING_NOT_FOUND",
        f"store listing {listing_id!r} not found",
        status_code=404,
    )


def ensure_store_token(store_token: str | None) -> str:
    token = (store_token or "").strip()
    if token:
        return token
    raise AgentStoreError(
        "STORE_AUTH_REQUIRED",
        "store account login is required",
        status_code=401,
    )


async def store_login(*, email: str, password: str) -> StoreAuthLoginResult:
    if not email.strip() or not password.strip():
        raise AgentStoreError(
            "STORE_LOGIN_INVALID_INPUT",
            "email and password are required",
            status_code=400,
        )
    try:
        return await _server_client().login(username=email.strip(), password=password)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def store_register(*, email: str, password: str, display_name: str = "") -> StoreAuthActionResult:
    if not email.strip() or not password.strip():
        raise AgentStoreError(
            "STORE_REGISTER_INVALID_INPUT",
            "email and password are required",
            status_code=400,
        )
    try:
        return await _server_client().register(
            email=email.strip(),
            password=password,
            display_name=display_name.strip(),
        )
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def store_verify_email(*, email: str, code: str) -> StoreAuthActionResult:
    if not email.strip() or not code.strip():
        raise AgentStoreError(
            "STORE_VERIFY_INVALID_INPUT",
            "email and code are required",
            status_code=400,
        )
    try:
        return await _server_client().verify_email(email=email.strip(), code=code.strip())
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def store_forgot_password(*, email: str) -> StoreAuthActionResult:
    if not email.strip():
        raise AgentStoreError(
            "STORE_FORGOT_INVALID_INPUT",
            "email is required",
            status_code=400,
        )
    try:
        return await _server_client().forgot_password(email=email.strip())
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def store_reset_password(*, email: str, code: str, new_password: str) -> StoreAuthActionResult:
    if not email.strip() or not code.strip() or not new_password.strip():
        raise AgentStoreError(
            "STORE_RESET_INVALID_INPUT",
            "email, code and new password are required",
            status_code=400,
        )
    try:
        return await _server_client().reset_password(
            email=email.strip(),
            code=code.strip(),
            new_password=new_password,
        )
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def store_me(*, store_token: str) -> StoreAuthAccount:
    token = ensure_store_token(store_token)
    try:
        return await _server_client().me(access_token=token)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def list_owned_listings(*, store_token: str) -> list[OwnedListingView]:
    token = ensure_store_token(store_token)
    try:
        rows = await _server_client().list_owned(access_token=token)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc
    out = [_to_owned_view(item) for item in rows]
    out.sort(key=lambda item: item.acquired_at, reverse=True)
    return out


async def join_listing(
    listing_id: str,
    *,
    store_token: str,
) -> StoreAcquireResult:
    token = ensure_store_token(store_token)
    try:
        return await _server_client().join_listing(listing_id, access_token=token)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def purchase_listing(
    listing_id: str,
    *,
    store_token: str,
) -> StoreAcquireResult:
    token = ensure_store_token(store_token)
    try:
        return await _server_client().purchase_listing(listing_id, access_token=token)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc


async def _load_single_agent(
    *,
    listing: StoreCatalogListing,
    agent: StoreAgentDefinition,
    bundle: StoreWorkspaceBundle,
    user: str,
    storage: StorageBackend,
    cfg: Config,
) -> ListingLoadResult:
    conflict = _resolve_conflict_reason(agent_id=agent.id, storage=storage, cfg=cfg)
    if conflict is not None:
        raise AgentStoreError(
            "STORE_LOAD_CONFLICT",
            "cannot load listing due to local agent id conflict",
            status_code=409,
            details={"conflicts": [conflict]},
        )
    committed_agent_id: str | None = None
    try:
        created = await openclaw_svc.commit_agent(
            _to_commit_input(agent, listing_id=listing.listing_id),
            user=user,
            storage=storage,
            config=cfg,
        )
        committed_agent_id = created.id
        source_root = _bundle_source_root_for_agent(
            bundle=bundle,
            listing_type=StoreListingType.single,
            agent_id=agent.id,
        )
        _overlay_workspace_from_bundle(
            bundle_blob=bundle.bundle_blob,
            source_root=source_root,
            target_workspace=Path(created.workspace_path),
        )
        # Re-apply common AGENTS rules and standard skills dynamically.
        openclaw_svc.reinstall_skills(created.id, storage=storage, config=cfg)
    except openclaw_svc.OpenclawAgentError as exc:
        raise AgentStoreError(
            exc.code,
            exc.message,
            status_code=exc.status_code,
            details=exc.details,
        ) from exc
    except AgentStoreError:
        if committed_agent_id:
            try:
                await openclaw_svc.delete_agent(
                    committed_agent_id,
                    mode="purge",
                    storage=storage,
                    config=cfg,
                )
            except Exception:
                logger.warning("agent_store_single_rollback_failed", agent_id=committed_agent_id)
        raise
    except Exception as exc:
        if committed_agent_id:
            try:
                await openclaw_svc.delete_agent(
                    committed_agent_id,
                    mode="purge",
                    storage=storage,
                    config=cfg,
                )
            except Exception:
                logger.warning("agent_store_single_rollback_failed", agent_id=committed_agent_id)
        raise AgentStoreError(
            "STORE_WORKSPACE_COPY_FAILED",
            "failed to restore agent workspace from store bundle",
            status_code=500,
            details={"listingId": listing.listing_id, "agentId": agent.id, "error": str(exc)},
        ) from exc
    return ListingLoadResult(
        listing_id=listing.listing_id,
        listing_type=StoreListingType.single,
        loaded_agent_ids=[agent.id],
        team_id=None,
    )


async def _load_team_listing(
    *,
    listing: StoreCatalogListing,
    manifest: StoreTeamManifest,
    bundle: StoreWorkspaceBundle,
    user: str,
    storage: StorageBackend,
    cfg: Config,
) -> ListingLoadResult:
    conflicts: list[dict[str, str]] = []
    for item in manifest.agents:
        conflict = _resolve_conflict_reason(agent_id=item.id, storage=storage, cfg=cfg)
        if conflict is not None:
            conflicts.append(conflict)
    if conflicts:
        raise AgentStoreError(
            "STORE_TEAM_LOAD_CONFLICT",
            "team listing has conflicting agent ids in local environment",
            status_code=409,
            details={"conflicts": conflicts},
        )
    team = openclaw_svc.create_team(manifest.team_name, user=user, storage=storage, config=cfg)
    created_agent_ids: list[str] = []
    try:
        for item in manifest.agents:
            created = await openclaw_svc.commit_agent(
                _to_commit_input(item, listing_id=listing.listing_id),
                user=user,
                team_id=team.id,
                storage=storage,
                config=cfg,
            )
            created_agent_ids.append(created.id)
            source_root = _bundle_source_root_for_agent(
                bundle=bundle,
                listing_type=StoreListingType.team,
                agent_id=item.id,
            )
            _overlay_workspace_from_bundle(
                bundle_blob=bundle.bundle_blob,
                source_root=source_root,
                target_workspace=Path(created.workspace_path),
            )
            openclaw_svc.reinstall_skills(created.id, storage=storage, config=cfg)
    except openclaw_svc.OpenclawAgentError as exc:
        for aid in reversed(created_agent_ids):
            try:
                await openclaw_svc.delete_agent(
                    aid,
                    mode="purge",
                    storage=storage,
                    config=cfg,
                )
            except Exception:
                logger.warning("agent_store_team_rollback_failed", agent_id=aid, listing_id=listing.listing_id)
        raise AgentStoreError(
            "STORE_TEAM_LOAD_FAILED",
            exc.message,
            status_code=exc.status_code,
            details={"failed_agent_id": getattr(exc, "agent_id", None), **exc.details},
        ) from exc
    except AgentStoreError:
        for aid in reversed(created_agent_ids):
            try:
                await openclaw_svc.delete_agent(
                    aid,
                    mode="purge",
                    storage=storage,
                    config=cfg,
                )
            except Exception:
                logger.warning("agent_store_team_rollback_failed", agent_id=aid, listing_id=listing.listing_id)
        raise
    except Exception as exc:
        for aid in reversed(created_agent_ids):
            try:
                await openclaw_svc.delete_agent(
                    aid,
                    mode="purge",
                    storage=storage,
                    config=cfg,
                )
            except Exception:
                logger.warning("agent_store_team_rollback_failed", agent_id=aid, listing_id=listing.listing_id)
        raise AgentStoreError(
            "STORE_WORKSPACE_COPY_FAILED",
            "failed to restore team workspace from store bundle",
            status_code=500,
            details={"listingId": listing.listing_id, "error": str(exc)},
        ) from exc
    return ListingLoadResult(
        listing_id=listing.listing_id,
        listing_type=StoreListingType.team,
        loaded_agent_ids=created_agent_ids,
        team_id=team.id,
    )


async def load_owned_listing_to_local(
    listing_id: str,
    *,
    store_token: str,
    user: str,
    storage: StorageBackend | None = None,
    config: Config | None = None,
) -> ListingLoadResult:
    token = ensure_store_token(store_token)
    cfg = config or load_config()
    storage = storage or get_storage(cfg)

    owned = await list_owned_listings(store_token=token)
    if not any(item.listing_id == listing_id for item in owned):
        raise AgentStoreError(
            "STORE_LISTING_NOT_OWNED",
            f"listing {listing_id!r} is not owned by current store account",
            status_code=403,
        )

    listing = await get_listing(listing_id)
    try:
        manifest = await _server_client().fetch_manifest(listing_id, access_token=token)
        bundle = await _server_client().fetch_workspace_bundle(listing_id, access_token=token)
    except StoreServerError as exc:
        raise _map_store_server_error(exc) from exc
    if manifest.listing_id != listing.listing_id:
        raise AgentStoreError(
            "STORE_SERVER_SCHEMA_INVALID",
            "manifest listing id mismatch",
            status_code=502,
            details={
                "catalog_listing_id": listing.listing_id,
                "manifest_listing_id": manifest.listing_id,
            },
        )
    if bundle.listing_id != listing.listing_id:
        raise AgentStoreError(
            "STORE_SERVER_SCHEMA_INVALID",
            "workspace bundle listing id mismatch",
            status_code=502,
            details={
                "catalog_listing_id": listing.listing_id,
                "bundle_listing_id": bundle.listing_id,
            },
        )
    if bundle.listing_type != listing.type:
        raise AgentStoreError(
            "STORE_SERVER_SCHEMA_INVALID",
            "workspace bundle listing type mismatch",
            status_code=502,
            details={
                "catalog_listing_type": listing.type.value,
                "bundle_listing_type": bundle.listing_type.value,
            },
        )
    if listing.type == StoreListingType.single:
        assert hasattr(manifest, "agent")
        return await _load_single_agent(
            listing=listing,
            agent=manifest.agent,  # type: ignore[attr-defined]
            bundle=bundle,
            user=user,
            storage=storage,
            cfg=cfg,
        )
    assert isinstance(manifest, StoreTeamManifest)
    return await _load_team_listing(
        listing=listing,
        manifest=manifest,
        bundle=bundle,
        user=user,
        storage=storage,
        cfg=cfg,
    )


def clear_catalog_cache() -> None:
    global _CATALOG_CACHE
    _CATALOG_CACHE = None


__all__ = [
    "AgentStoreError",
    "ListingLoadResult",
    "OwnedListingView",
    "clear_catalog_cache",
    "ensure_store_token",
    "get_listing",
    "join_listing",
    "list_catalog",
    "list_owned_listings",
    "load_owned_listing_to_local",
    "purchase_listing",
    "store_forgot_password",
    "store_login",
    "store_register",
    "store_reset_password",
    "store_me",
    "store_verify_email",
]
