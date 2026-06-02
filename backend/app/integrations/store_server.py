"""HTTP client for external Agent Store Server.

This integration treats Store as an independent account domain:

- Catalog browsing can be anonymous.
- Owned/acquire endpoints require Store account bearer token.
- Manifest fetch for local load also uses Store account token.
"""

from __future__ import annotations

import base64
import os
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, ValidationError
from pydantic.alias_generators import to_camel

from app.integrations.github_store_repo import (
    StoreCatalogListing,
    StoreListingType,
    StorePricing,
    StoreSingleManifest,
    StoreTeamManifest,
)

_DEFAULT_TIMEOUT_SECONDS = 15.0
_DEFAULT_BASE_URL = "http://127.0.0.1:18181"


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="ignore",
    )


class StoreAuthAccount(_CamelModel):
    id: str
    display_name: str = ""
    email: str = ""
    avatar_url: str = ""


class StoreAuthLoginResult(_CamelModel):
    access_token: str
    token_type: str = "Bearer"
    expires_at: str | None = None
    account: StoreAuthAccount


class StoreAuthActionResult(_CamelModel):
    status: str = "ok"
    message: str = ""


class StoreOwnedListing(_CamelModel):
    listing_id: str
    type: StoreListingType
    title: str
    description: str = ""
    avatar_url: str = ""
    pricing: StorePricing
    acquired_via: str = "join"
    acquired_at: str = ""
    source_manifest_path: str = ""


class StoreAcquireResult(_CamelModel):
    owned: StoreOwnedListing
    order: dict[str, Any] | None = None


class _StoreWorkspaceBundleResponse(_CamelModel):
    listing_id: str
    listing_type: StoreListingType
    encoding: str
    payload_base64: str
    agent_paths: dict[str, str] = {}


@dataclass(frozen=True)
class StoreWorkspaceBundle:
    listing_id: str
    listing_type: StoreListingType
    bundle_blob: bytes
    agent_paths: dict[str, str]


StoreManifest = StoreSingleManifest | StoreTeamManifest


@dataclass(frozen=True)
class StoreServerSpec:
    base_url: str
    timeout_seconds: float = _DEFAULT_TIMEOUT_SECONDS


class StoreServerError(Exception):
    code: str
    status_code: int
    details: dict[str, Any]

    def __init__(
        self,
        code: str,
        message: str,
        *,
        status_code: int = 502,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status_code = status_code
        self.details = details or {}
        self.message = message


def load_store_server_spec_from_env() -> StoreServerSpec:
    base_url = os.getenv("CSFLOW_AGENT_STORE_SERVER_BASE_URL", _DEFAULT_BASE_URL).strip()
    timeout_raw = os.getenv("CSFLOW_AGENT_STORE_SERVER_TIMEOUT_SECONDS", "").strip()
    if not base_url:
        base_url = _DEFAULT_BASE_URL
    timeout = _DEFAULT_TIMEOUT_SECONDS
    if timeout_raw:
        try:
            timeout = max(float(timeout_raw), 1.0)
        except ValueError:
            timeout = _DEFAULT_TIMEOUT_SECONDS
    return StoreServerSpec(base_url=base_url.rstrip("/"), timeout_seconds=timeout)


class StoreServerClient:
    def __init__(self, spec: StoreServerSpec) -> None:
        self._spec = spec

    async def list_catalog(self, *, listing_type: StoreListingType | None = None) -> list[StoreCatalogListing]:
        params: dict[str, str] = {}
        if listing_type is not None:
            params["type"] = listing_type.value
        payload = await self._request_json("GET", "/catalog", params=params)
        items = self._extract_items(payload, key="listings")
        out: list[StoreCatalogListing] = []
        for item in items:
            try:
                out.append(StoreCatalogListing.model_validate(item))
            except ValidationError as exc:
                raise StoreServerError(
                    "STORE_SERVER_SCHEMA_INVALID",
                    "store catalog item schema validation failed",
                    details={"error": str(exc), "item": item},
                ) from exc
        return out

    async def fetch_manifest(
        self,
        listing_id: str,
        *,
        access_token: str,
    ) -> StoreManifest:
        payload = await self._request_json(
            "GET",
            f"/listings/{quote(listing_id, safe='')}/manifest",
            access_token=access_token,
        )
        manifest_type = str(payload.get("type") or "").strip().lower()
        try:
            if manifest_type == StoreListingType.team.value:
                return StoreTeamManifest.model_validate(payload)
            return StoreSingleManifest.model_validate(payload)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store manifest schema validation failed",
                details={"listing_id": listing_id, "error": str(exc)},
            ) from exc

    async def fetch_workspace_bundle(
        self,
        listing_id: str,
        *,
        access_token: str,
    ) -> StoreWorkspaceBundle:
        payload = await self._request_json(
            "GET",
            f"/listings/{quote(listing_id, safe='')}/workspace-bundle",
            access_token=access_token,
        )
        try:
            parsed = _StoreWorkspaceBundleResponse.model_validate(payload)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store workspace bundle schema validation failed",
                details={"listing_id": listing_id, "error": str(exc)},
            ) from exc
        if parsed.encoding != "tar+gzip+base64":
            raise StoreServerError(
                "STORE_SERVER_BAD_RESPONSE",
                "unsupported workspace bundle encoding",
                details={"listing_id": listing_id, "encoding": parsed.encoding},
            )
        try:
            blob = base64.b64decode(parsed.payload_base64.encode("ascii"), validate=True)
        except Exception as exc:
            raise StoreServerError(
                "STORE_SERVER_BAD_RESPONSE",
                "workspace bundle payload is not valid base64",
                details={"listing_id": listing_id},
            ) from exc
        return StoreWorkspaceBundle(
            listing_id=parsed.listing_id,
            listing_type=parsed.listing_type,
            bundle_blob=blob,
            agent_paths={str(k): str(v) for k, v in parsed.agent_paths.items()},
        )

    async def login(self, *, username: str, password: str) -> StoreAuthLoginResult:
        payload = await self._request_json(
            "POST",
            "/auth/login",
            body={"email": username, "password": password},
        )
        normalized = dict(payload)
        if "accessToken" not in normalized and "access_token" in normalized:
            normalized["accessToken"] = normalized["access_token"]
        if "tokenType" not in normalized and "token_type" in normalized:
            normalized["tokenType"] = normalized["token_type"]
        if "expiresAt" not in normalized and "expires_at" in normalized:
            normalized["expiresAt"] = normalized["expires_at"]
        if "account" not in normalized and "user" in normalized:
            normalized["account"] = normalized["user"]
        if "account" not in normalized and isinstance(normalized.get("data"), dict):
            data = normalized["data"]
            if "account" in data and isinstance(data["account"], dict):
                normalized["account"] = data["account"]
        if "accessToken" not in normalized and "token" in normalized:
            normalized["accessToken"] = normalized["token"]
        try:
            return StoreAuthLoginResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store auth login response schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def register(
        self,
        *,
        email: str,
        password: str,
        display_name: str = "",
    ) -> StoreAuthActionResult:
        payload = await self._request_json(
            "POST",
            "/auth/register",
            body={"email": email, "password": password, "displayName": display_name},
        )
        normalized = dict(payload)
        if "status" not in normalized:
            normalized["status"] = "ok"
        if "message" not in normalized:
            normalized["message"] = str(payload.get("detail") or "")
        try:
            return StoreAuthActionResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store auth register response schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def verify_email(self, *, email: str, code: str) -> StoreAuthActionResult:
        payload = await self._request_json(
            "POST",
            "/auth/verify-email",
            body={"email": email, "code": code},
        )
        normalized = dict(payload)
        if "status" not in normalized:
            normalized["status"] = "ok"
        if "message" not in normalized:
            normalized["message"] = str(payload.get("detail") or "")
        try:
            return StoreAuthActionResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store auth verify response schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def forgot_password(self, *, email: str) -> StoreAuthActionResult:
        payload = await self._request_json(
            "POST",
            "/auth/forgot-password",
            body={"email": email},
        )
        normalized = dict(payload)
        if "status" not in normalized:
            normalized["status"] = "ok"
        if "message" not in normalized:
            normalized["message"] = str(payload.get("detail") or "")
        try:
            return StoreAuthActionResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store forgot-password response schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def reset_password(
        self,
        *,
        email: str,
        code: str,
        new_password: str,
    ) -> StoreAuthActionResult:
        payload = await self._request_json(
            "POST",
            "/auth/reset-password",
            body={"email": email, "code": code, "newPassword": new_password},
        )
        normalized = dict(payload)
        if "status" not in normalized:
            normalized["status"] = "ok"
        if "message" not in normalized:
            normalized["message"] = str(payload.get("detail") or "")
        try:
            return StoreAuthActionResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store reset-password response schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def me(self, *, access_token: str) -> StoreAuthAccount:
        payload = await self._request_json("GET", "/auth/me", access_token=access_token)
        normalized = dict(payload)
        if "account" in normalized and isinstance(normalized["account"], dict):
            normalized = normalized["account"]
        try:
            return StoreAuthAccount.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store auth profile schema invalid",
                details={"error": str(exc)},
            ) from exc

    async def list_owned(self, *, access_token: str) -> list[StoreOwnedListing]:
        payload = await self._request_json("GET", "/owned", access_token=access_token)
        items = self._extract_items(payload, key="items")
        out: list[StoreOwnedListing] = []
        for item in items:
            try:
                out.append(StoreOwnedListing.model_validate(item))
            except ValidationError as exc:
                raise StoreServerError(
                    "STORE_SERVER_SCHEMA_INVALID",
                    "store owned listing schema validation failed",
                    details={"error": str(exc), "item": item},
                ) from exc
        return out

    async def join_listing(
        self,
        listing_id: str,
        *,
        access_token: str,
    ) -> StoreAcquireResult:
        payload = await self._request_json(
            "POST",
            f"/listings/{quote(listing_id, safe='')}/join",
            access_token=access_token,
        )
        return self._parse_acquire_result(payload, listing_id=listing_id)

    async def purchase_listing(
        self,
        listing_id: str,
        *,
        access_token: str,
    ) -> StoreAcquireResult:
        payload = await self._request_json(
            "POST",
            f"/listings/{quote(listing_id, safe='')}/purchase",
            access_token=access_token,
        )
        return self._parse_acquire_result(payload, listing_id=listing_id)

    def _parse_acquire_result(self, payload: dict[str, Any], *, listing_id: str) -> StoreAcquireResult:
        normalized = dict(payload)
        if "owned" not in normalized and "item" in normalized:
            normalized["owned"] = normalized["item"]
        try:
            return StoreAcquireResult.model_validate(normalized)
        except ValidationError as exc:
            raise StoreServerError(
                "STORE_SERVER_SCHEMA_INVALID",
                "store acquire response schema invalid",
                details={"listing_id": listing_id, "error": str(exc)},
            ) from exc

    def _extract_items(self, payload: dict[str, Any], *, key: str) -> list[dict[str, Any]]:
        value: Any
        if isinstance(payload.get("items"), list):
            value = payload.get("items")
        elif isinstance(payload.get(key), list):
            value = payload.get(key)
        elif isinstance(payload, list):
            value = payload
        else:
            raise StoreServerError(
                "STORE_SERVER_BAD_RESPONSE",
                "store server response does not contain listing array",
                details={"response_keys": list(payload.keys())},
            )
        out: list[dict[str, Any]] = []
        for item in value:
            if isinstance(item, dict):
                out.append(item)
        return out

    async def _request_json(
        self,
        method: str,
        path: str,
        *,
        access_token: str | None = None,
        params: dict[str, str] | None = None,
        body: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        headers = {"Accept": "application/json"}
        if access_token:
            headers["Authorization"] = f"Bearer {access_token}"
        timeout = httpx.Timeout(self._spec.timeout_seconds, read=self._spec.timeout_seconds)
        url = f"{self._spec.base_url}{path}"
        try:
            async with httpx.AsyncClient(timeout=timeout) as client:
                resp = await client.request(method, url, params=params, json=body, headers=headers)
        except httpx.ReadTimeout as exc:
            raise StoreServerError(
                "STORE_SERVER_TIMEOUT",
                "store server request timed out",
                status_code=504,
                details={"path": path},
            ) from exc
        except httpx.HTTPError as exc:
            raise StoreServerError(
                "STORE_SERVER_UNREACHABLE",
                f"failed to request store server: {exc}",
                status_code=502,
                details={"path": path},
            ) from exc
        payload: dict[str, Any] = {}
        if resp.content:
            try:
                parsed = resp.json()
            except ValueError as exc:
                raise StoreServerError(
                    "STORE_SERVER_BAD_RESPONSE",
                    "store server response is not JSON",
                    details={"path": path, "status_code": resp.status_code},
                ) from exc
            if isinstance(parsed, dict):
                payload = parsed
            else:
                raise StoreServerError(
                    "STORE_SERVER_BAD_RESPONSE",
                    "store server response root must be object",
                    details={"path": path, "status_code": resp.status_code},
                )
        if resp.status_code >= 400:
            validation_detail = payload.get("detail")
            if resp.status_code == 422 and validation_detail is not None:
                raise StoreServerError(
                    "STORE_SERVER_VALIDATION_ERROR",
                    "store server request validation failed",
                    status_code=resp.status_code,
                    details={"path": path, "validation": validation_detail},
                )
            code = str(payload.get("error") or "").strip() or (
                "STORE_AUTH_REQUIRED" if resp.status_code == 401 else "STORE_SERVER_HTTP_ERROR"
            )
            message = str(payload.get("message") or "").strip() or f"store server HTTP {resp.status_code}"
            details = payload.get("details")
            return_details = details if isinstance(details, dict) else {"path": path}
            raise StoreServerError(
                code,
                message,
                status_code=resp.status_code,
                details=return_details,
            )
        return payload


__all__ = [
    "StoreAcquireResult",
    "StoreAuthActionResult",
    "StoreAuthAccount",
    "StoreAuthLoginResult",
    "StoreManifest",
    "StoreOwnedListing",
    "StoreServerClient",
    "StoreServerError",
    "StoreServerSpec",
    "StoreWorkspaceBundle",
    "load_store_server_spec_from_env",
]
