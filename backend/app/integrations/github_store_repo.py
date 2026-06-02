"""GitHub private-repo source for Agent Store listings.

Contract (v1)
-------------
This module defines the canonical Store data contract and reads it from a
private GitHub repository via the GitHub Contents API.

Expected repository layout (paths are configurable, shown as defaults):

    catalog/index.json
    catalog/listings/<listing-id>.json

``catalog/index.json`` example:

{
  "schemaVersion": 1,
  "listings": [
    {
      "listingId": "single-research-assistant",
      "type": "single",
      "title": "Research Assistant",
      "description": "Web research and summarisation",
      "avatarUrl": "https://...",
      "manifestPath": "catalog/listings/single-research-assistant.json",
      "pricing": { "mode": "free", "currency": "USD", "amount": 0 }
    }
  ]
}

Single manifest example:

{
  "schemaVersion": 1,
  "listingId": "single-research-assistant",
  "type": "single",
  "agent": {
    "id": "research_assistant",
    "name": "Research Assistant",
    "description": "Find facts and produce concise summaries",
    "avatarUrl": "https://...",
    "identityEmoji": "🔎",
    "identityTheme": "Rigorous, objective, concise",
    "nlPrompt": "You are a research assistant..."
  }
}

Team manifest example:

{
  "schemaVersion": 1,
  "listingId": "team-growth-starter",
  "type": "team",
  "teamName": "Growth Starter Team",
  "agents": [ ...same agent schema... ]
}
"""

from __future__ import annotations

import base64
import json
import os
from dataclasses import dataclass
from enum import Enum
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from pydantic.alias_generators import to_camel


_DEFAULT_GITHUB_REF = "main"
_DEFAULT_CATALOG_PATH = "catalog/index.json"
_DEFAULT_HTTP_TIMEOUT_SECONDS = 15.0
_DEFAULT_GITHUB_OWNER = "revalue-coding"
_DEFAULT_GITHUB_REPO = "ClawsomeFlow-AgentStore"


class StoreListingType(str, Enum):
    single = "single"
    team = "team"


class StorePricingMode(str, Enum):
    free = "free"
    paid = "paid"


class _CamelModel(BaseModel):
    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=to_camel,
        extra="forbid",
    )


class StorePricing(_CamelModel):
    mode: StorePricingMode
    currency: str = "USD"
    amount: float = 0.0

    @model_validator(mode="after")
    def _validate_amount(self) -> "StorePricing":
        if self.amount < 0:
            raise ValueError("pricing.amount must be >= 0")
        if self.mode == StorePricingMode.free and self.amount != 0:
            raise ValueError("free listings must use amount = 0")
        if self.mode == StorePricingMode.paid and self.amount <= 0:
            raise ValueError("paid listings must use amount > 0")
        return self


class StoreAgentDefinition(_CamelModel):
    id: str
    name: str
    description: str = ""
    avatar_url: str = ""
    identity_emoji: str | None = None
    identity_theme: str | None = None
    model: str | None = None
    nl_prompt: str = ""
    extra_skills: list[str] = Field(default_factory=list)


class StoreListingPreviewAgent(_CamelModel):
    id: str
    name: str
    avatar_url: str = ""


class StoreCatalogListing(_CamelModel):
    listing_id: str
    type: StoreListingType
    title: str
    description: str = ""
    avatar_url: str = ""
    manifest_path: str
    pricing: StorePricing
    preview_agents: list[StoreListingPreviewAgent] = Field(default_factory=list)


class StoreCatalog(_CamelModel):
    schema_version: int = 1
    listings: list[StoreCatalogListing]

    @model_validator(mode="after")
    def _validate_unique_listing_id(self) -> "StoreCatalog":
        seen: set[str] = set()
        for item in self.listings:
            if item.listing_id in seen:
                raise ValueError(f"duplicate listingId in catalog: {item.listing_id}")
            seen.add(item.listing_id)
        return self


class StoreSingleManifest(_CamelModel):
    schema_version: int = 1
    listing_id: str
    type: StoreListingType = StoreListingType.single
    agent: StoreAgentDefinition

    @model_validator(mode="after")
    def _ensure_single(self) -> "StoreSingleManifest":
        if self.type != StoreListingType.single:
            raise ValueError("single manifest must use type=single")
        return self


class StoreTeamManifest(_CamelModel):
    schema_version: int = 1
    listing_id: str
    type: StoreListingType = StoreListingType.team
    team_name: str
    agents: list[StoreAgentDefinition]

    @model_validator(mode="after")
    def _ensure_team(self) -> "StoreTeamManifest":
        if self.type != StoreListingType.team:
            raise ValueError("team manifest must use type=team")
        if not self.team_name.strip():
            raise ValueError("teamName must not be empty")
        if not self.agents:
            raise ValueError("team manifest must include at least one agent")
        seen: set[str] = set()
        for item in self.agents:
            if item.id in seen:
                raise ValueError(f"duplicate agent id in team manifest: {item.id}")
            seen.add(item.id)
        return self


StoreManifest = StoreSingleManifest | StoreTeamManifest


@dataclass(frozen=True)
class GithubStoreRepoSpec:
    owner: str
    repo: str
    ref: str
    token: str
    catalog_path: str


class GithubStoreRepoError(Exception):
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


def load_store_repo_spec_from_env() -> GithubStoreRepoSpec:
    """Load GitHub private-repo access config from env vars.

    Optional with defaults:
    - CSFLOW_AGENT_STORE_GITHUB_OWNER (default: revalue-coding)
    - CSFLOW_AGENT_STORE_GITHUB_REPO (default: ClawsomeFlow-AgentStore)
    - CSFLOW_AGENT_STORE_GITHUB_TOKEN

    Optional:
    - CSFLOW_AGENT_STORE_GITHUB_REF (default: main)
    - CSFLOW_AGENT_STORE_CATALOG_PATH (default: catalog/index.json)
    """
    owner = os.getenv("CSFLOW_AGENT_STORE_GITHUB_OWNER", _DEFAULT_GITHUB_OWNER).strip()
    repo = os.getenv("CSFLOW_AGENT_STORE_GITHUB_REPO", _DEFAULT_GITHUB_REPO).strip()
    token = os.getenv("CSFLOW_AGENT_STORE_GITHUB_TOKEN", "").strip()
    ref = os.getenv("CSFLOW_AGENT_STORE_GITHUB_REF", _DEFAULT_GITHUB_REF).strip()
    catalog_path = os.getenv("CSFLOW_AGENT_STORE_CATALOG_PATH", _DEFAULT_CATALOG_PATH).strip()
    if not owner:
        owner = _DEFAULT_GITHUB_OWNER
    if not repo:
        repo = _DEFAULT_GITHUB_REPO
    if not token:
        raise GithubStoreRepoError(
            "STORE_REPO_NOT_CONFIGURED",
            "agent store GitHub repository is not configured",
            status_code=503,
            details={
                "required_env": [
                    "CSFLOW_AGENT_STORE_GITHUB_TOKEN",
                ],
            },
        )
    if not ref:
        ref = _DEFAULT_GITHUB_REF
    if not catalog_path:
        catalog_path = _DEFAULT_CATALOG_PATH
    return GithubStoreRepoSpec(
        owner=owner,
        repo=repo,
        ref=ref,
        token=token,
        catalog_path=catalog_path,
    )


class GithubStoreRepoClient:
    """Read Agent Store catalog/manifests from GitHub private repo."""

    def __init__(
        self,
        spec: GithubStoreRepoSpec,
        *,
        timeout_seconds: float = _DEFAULT_HTTP_TIMEOUT_SECONDS,
    ) -> None:
        self._spec = spec
        self._timeout_seconds = max(timeout_seconds, 1.0)

    async def fetch_catalog(self) -> StoreCatalog:
        payload = await self._fetch_json_file(self._spec.catalog_path)
        try:
            return StoreCatalog.model_validate(payload)
        except ValidationError as exc:
            raise GithubStoreRepoError(
                "STORE_REPO_SCHEMA_INVALID",
                "catalog schema validation failed",
                details={"path": self._spec.catalog_path, "error": str(exc)},
            ) from exc

    async def fetch_manifest(
        self,
        listing: StoreCatalogListing,
    ) -> StoreManifest:
        payload = await self._fetch_json_file(listing.manifest_path)
        try:
            if listing.type == StoreListingType.single:
                manifest = StoreSingleManifest.model_validate(payload)
            else:
                manifest = StoreTeamManifest.model_validate(payload)
        except ValidationError as exc:
            raise GithubStoreRepoError(
                "STORE_REPO_SCHEMA_INVALID",
                "listing manifest schema validation failed",
                details={"path": listing.manifest_path, "error": str(exc)},
            ) from exc
        if manifest.listing_id != listing.listing_id:
            raise GithubStoreRepoError(
                "STORE_REPO_SCHEMA_INVALID",
                "listing id mismatch between catalog and manifest",
                details={
                    "catalog_listing_id": listing.listing_id,
                    "manifest_listing_id": manifest.listing_id,
                    "manifest_path": listing.manifest_path,
                },
            )
        return manifest

    async def _fetch_json_file(self, path: str) -> dict[str, Any]:
        url = (
            f"https://api.github.com/repos/{self._spec.owner}/"
            f"{self._spec.repo}/contents/{path}"
        )
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self._spec.token}",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "clawsomeflow-agent-store",
        }
        timeout = httpx.Timeout(self._timeout_seconds, read=self._timeout_seconds)
        async with httpx.AsyncClient(timeout=timeout) as client:
            try:
                response = await client.get(url, params={"ref": self._spec.ref}, headers=headers)
            except httpx.HTTPError as exc:
                raise GithubStoreRepoError(
                    "STORE_REPO_HTTP_ERROR",
                    f"failed to fetch store file from GitHub: {exc}",
                    details={"path": path},
                ) from exc
        if response.status_code in (401, 403):
            raise GithubStoreRepoError(
                "STORE_REPO_AUTH_FAILED",
                "GitHub authentication failed for store repository",
                status_code=502,
                details={"path": path, "status_code": response.status_code},
            )
        if response.status_code == 404:
            raise GithubStoreRepoError(
                "STORE_REPO_FILE_NOT_FOUND",
                "store file not found in GitHub repository",
                status_code=404,
                details={"path": path},
            )
        if response.status_code >= 400:
            text = response.text[:500]
            raise GithubStoreRepoError(
                "STORE_REPO_HTTP_ERROR",
                f"GitHub returned HTTP {response.status_code}",
                details={"path": path, "response": text},
            )
        try:
            payload = response.json()
        except json.JSONDecodeError as exc:
            raise GithubStoreRepoError(
                "STORE_REPO_BAD_RESPONSE",
                "GitHub response is not valid JSON",
                details={"path": path},
            ) from exc
        content = payload.get("content")
        encoding = payload.get("encoding")
        if not isinstance(content, str) or encoding != "base64":
            raise GithubStoreRepoError(
                "STORE_REPO_BAD_RESPONSE",
                "GitHub file response missing base64 content",
                details={"path": path},
            )
        try:
            decoded = base64.b64decode(content)
        except ValueError as exc:
            raise GithubStoreRepoError(
                "STORE_REPO_BAD_RESPONSE",
                "failed to decode GitHub file base64 content",
                details={"path": path},
            ) from exc
        try:
            obj = json.loads(decoded.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise GithubStoreRepoError(
                "STORE_REPO_BAD_RESPONSE",
                "store file is not valid UTF-8 JSON",
                details={"path": path},
            ) from exc
        if not isinstance(obj, dict):
            raise GithubStoreRepoError(
                "STORE_REPO_SCHEMA_INVALID",
                "store file root must be a JSON object",
                details={"path": path},
            )
        return obj


__all__ = [
    "GithubStoreRepoClient",
    "GithubStoreRepoError",
    "GithubStoreRepoSpec",
    "StoreAgentDefinition",
    "StoreCatalog",
    "StoreCatalogListing",
    "StoreListingPreviewAgent",
    "StoreListingType",
    "StoreManifest",
    "StorePricing",
    "StorePricingMode",
    "StoreSingleManifest",
    "StoreTeamManifest",
    "load_store_repo_spec_from_env",
]
