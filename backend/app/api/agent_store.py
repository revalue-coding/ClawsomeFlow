"""Agent Store API (Store account is isolated from local app account)."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Header, Path, Query
from pydantic import BaseModel, ConfigDict
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.config import load_config
from app.integrations.github_store_repo import StoreListingType
from app.models import iso_utc
from app.services import agent_store as svc
from app.storage import StorageBackend, get_storage

router = APIRouter(prefix="/agent-store", tags=["agent-store"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


def _storage_dep() -> StorageBackend:
    return get_storage()


UserDep = Annotated[str, Depends(current_user)]
StorageDep = Annotated[StorageBackend, Depends(_storage_dep)]
StoreTokenDep = Annotated[str | None, Header(alias="X-CSFLOW-Store-Token")]


class StorePricingView(_CamelModel):
    mode: str
    currency: str
    amount: float


class StorePreviewAgentView(_CamelModel):
    id: str
    name: str
    avatar_url: str = ""


class StoreCatalogItemView(_CamelModel):
    listing_id: str
    type: str
    title: str
    description: str
    avatar_url: str
    manifest_path: str
    pricing: StorePricingView
    preview_agents: list[StorePreviewAgentView]


class StoreCatalogListResponse(_CamelModel):
    items: list[StoreCatalogItemView]
    total: int


class StoreAccountView(_CamelModel):
    id: str
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""


class StoreLoginRequest(_CamelModel):
    email: str
    password: str


class StoreLoginResponse(_CamelModel):
    access_token: str
    token_type: str = "Bearer"
    expires_at: str | None = None
    account: StoreAccountView


class StoreRegisterRequest(_CamelModel):
    email: str
    password: str
    display_name: str = ""


class StoreVerifyEmailRequest(_CamelModel):
    email: str
    code: str


class StoreForgotPasswordRequest(_CamelModel):
    email: str


class StoreResetPasswordRequest(_CamelModel):
    email: str
    code: str
    new_password: str


class StoreAuthActionResponse(_CamelModel):
    status: str
    message: str = ""


class StoreOwnedItemView(_CamelModel):
    listing_id: str
    type: str
    title: str
    description: str
    avatar_url: str
    pricing: StorePricingView
    acquired_via: str
    acquired_at: str
    source_manifest_path: str


class StoreOwnedListResponse(_CamelModel):
    items: list[StoreOwnedItemView]
    total: int


class StoreAcquireResponse(_CamelModel):
    owned: StoreOwnedItemView
    order: dict[str, Any] | None = None


class StoreLoadResponse(_CamelModel):
    listing_id: str
    listing_type: str
    loaded_agent_ids: list[str]
    team_id: str | None = None


def _raise_store_error(exc: svc.AgentStoreError) -> None:
    raise ApiError(
        exc.code,
        exc.message,
        status_code=exc.status_code,
        details=exc.details,
    ) from exc


def _require_store_token(store_token: str | None) -> str:
    try:
        return svc.ensure_store_token(store_token)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)


def _to_catalog_item(item: Any) -> StoreCatalogItemView:
    return StoreCatalogItemView(
        listing_id=item.listing_id,
        type=item.type.value if hasattr(item.type, "value") else str(item.type),
        title=item.title,
        description=item.description,
        avatar_url=item.avatar_url,
        manifest_path=item.manifest_path,
        pricing=StorePricingView(
            mode=item.pricing.mode.value if hasattr(item.pricing.mode, "value") else str(item.pricing.mode),
            currency=item.pricing.currency,
            amount=item.pricing.amount,
        ),
        preview_agents=[
            StorePreviewAgentView(id=a.id, name=a.name, avatar_url=a.avatar_url)
            for a in item.preview_agents
        ],
    )


def _to_owned_item(item: Any) -> StoreOwnedItemView:
    acquired_at_raw = getattr(item, "acquired_at", "")
    acquired_at = acquired_at_raw
    if hasattr(acquired_at_raw, "tzinfo"):
        acquired_at = iso_utc(acquired_at_raw)
    return StoreOwnedItemView(
        listing_id=item.listing_id,
        type=item.type.value if hasattr(item.type, "value") else str(item.type),
        title=item.title,
        description=item.description,
        avatar_url=item.avatar_url,
        pricing=StorePricingView(
            mode=item.pricing_mode.value if hasattr(item, "pricing_mode") else item.pricing.mode.value,
            currency=item.pricing_currency if hasattr(item, "pricing_currency") else item.pricing.currency,
            amount=item.pricing_amount if hasattr(item, "pricing_amount") else item.pricing.amount,
        ),
        acquired_via=item.acquired_via.value if hasattr(getattr(item, "acquired_via", None), "value") else str(item.acquired_via),
        acquired_at=str(acquired_at),
        source_manifest_path=item.source_manifest_path,
    )


@router.get("/catalog", response_model=StoreCatalogListResponse)
async def list_store_catalog(
    listing_type: Annotated[str | None, Query(alias="type")] = None,
) -> StoreCatalogListResponse:
    type_filter: StoreListingType | None = None
    if listing_type is not None:
        raw = listing_type.strip().lower()
        if raw:
            if raw not in {"single", "team"}:
                raise ApiError(
                    "INVALID_LISTING_TYPE",
                    "type must be 'single' or 'team'",
                    status_code=400,
                )
            type_filter = StoreListingType(raw)
    try:
        items = await svc.list_catalog(listing_type=type_filter)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    views = [_to_catalog_item(item) for item in items]
    return StoreCatalogListResponse(items=views, total=len(views))


@router.post("/auth/login", response_model=StoreLoginResponse)
async def login_store_account(payload: StoreLoginRequest) -> StoreLoginResponse:
    try:
        result = await svc.store_login(email=payload.email, password=payload.password)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreLoginResponse(
        access_token=result.access_token,
        token_type=result.token_type,
        expires_at=result.expires_at,
        account=StoreAccountView(
            id=result.account.id,
            display_name=result.account.display_name,
            email=result.account.email,
            avatar_url=result.account.avatar_url,
        ),
    )


@router.post("/auth/register", response_model=StoreAuthActionResponse)
async def register_store_account(payload: StoreRegisterRequest) -> StoreAuthActionResponse:
    try:
        result = await svc.store_register(
            email=payload.email,
            password=payload.password,
            display_name=payload.display_name,
        )
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAuthActionResponse(status=result.status, message=result.message)


@router.post("/auth/verify-email", response_model=StoreAuthActionResponse)
async def verify_store_account_email(payload: StoreVerifyEmailRequest) -> StoreAuthActionResponse:
    try:
        result = await svc.store_verify_email(email=payload.email, code=payload.code)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAuthActionResponse(status=result.status, message=result.message)


@router.post("/auth/forgot-password", response_model=StoreAuthActionResponse)
async def forgot_store_account_password(payload: StoreForgotPasswordRequest) -> StoreAuthActionResponse:
    try:
        result = await svc.store_forgot_password(email=payload.email)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAuthActionResponse(status=result.status, message=result.message)


@router.post("/auth/reset-password", response_model=StoreAuthActionResponse)
async def reset_store_account_password(payload: StoreResetPasswordRequest) -> StoreAuthActionResponse:
    try:
        result = await svc.store_reset_password(
            email=payload.email,
            code=payload.code,
            new_password=payload.new_password,
        )
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAuthActionResponse(status=result.status, message=result.message)


@router.get("/auth/me", response_model=StoreAccountView)
async def me_store_account(store_token: StoreTokenDep) -> StoreAccountView:
    token = _require_store_token(store_token)
    try:
        result = await svc.store_me(store_token=token)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAccountView(
        id=result.id,
        display_name=result.display_name,
        email=result.email,
        avatar_url=result.avatar_url,
    )


@router.get("/owned", response_model=StoreOwnedListResponse)
async def list_store_owned(store_token: StoreTokenDep) -> StoreOwnedListResponse:
    token = _require_store_token(store_token)
    try:
        items = await svc.list_owned_listings(store_token=token)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    views = [_to_owned_item(item) for item in items]
    return StoreOwnedListResponse(items=views, total=len(views))


@router.post("/listings/{listing_id}/join", response_model=StoreAcquireResponse)
async def join_store_listing(
    listing_id: Annotated[str, Path()],
    store_token: StoreTokenDep,
) -> StoreAcquireResponse:
    token = _require_store_token(store_token)
    try:
        out = await svc.join_listing(listing_id, store_token=token)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAcquireResponse(owned=_to_owned_item(out.owned), order=out.order)


@router.post("/listings/{listing_id}/purchase", response_model=StoreAcquireResponse)
async def purchase_store_listing(
    listing_id: Annotated[str, Path()],
    store_token: StoreTokenDep,
) -> StoreAcquireResponse:
    token = _require_store_token(store_token)
    try:
        out = await svc.purchase_listing(listing_id, store_token=token)
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreAcquireResponse(owned=_to_owned_item(out.owned), order=out.order)


@router.post("/listings/{listing_id}/load", response_model=StoreLoadResponse)
async def load_store_listing_to_local(
    listing_id: Annotated[str, Path()],
    user: UserDep,
    storage: StorageDep,
    store_token: StoreTokenDep,
) -> StoreLoadResponse:
    token = _require_store_token(store_token)
    try:
        out = await svc.load_owned_listing_to_local(
            listing_id,
            store_token=token,
            user=user,
            storage=storage,
            config=load_config(),
        )
    except svc.AgentStoreError as exc:
        _raise_store_error(exc)
    return StoreLoadResponse(
        listing_id=out.listing_id,
        listing_type=out.listing_type.value,
        loaded_agent_ids=out.loaded_agent_ids,
        team_id=out.team_id,
    )

