"""Profiles API — thin wrapper over ``clawteam profile`` CLI.

Per plan §11.5 / API.md "Profiles": ClawsomeFlow does **not** persist
profiles itself; it just proxies the underlying ``clawteam profile``
state so the front-end has a uniform place to consume it. ``set`` /
``remove`` aren't exposed in MVP — users still create profiles with the
``clawteam profile wizard`` (interactive) which we document in the UI.

Endpoints:
* ``GET  /api/profiles``                — list all profiles
* ``GET  /api/profiles/{name}``         — show one profile
* ``POST /api/profiles/{name}/test``    — non-interactive smoke test;
  body: ``{"prompt"?: str, "cwd"?: str}``; reply: stdout/stderr + ok flag.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Body, Depends, Path
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from app.api._auth import current_user
from app.api.errors import ApiError
from app.integrations.clawteam_cli import (
    CliInvocationError,
    get_clawteam_cli,
)

router = APIRouter(prefix="/profiles", tags=["profiles"])


class _CamelModel(BaseModel):
    model_config = ConfigDict(populate_by_name=True, alias_generator=to_camel)


UserDep = Annotated[str, Depends(current_user)]


class ProfileSummary(_CamelModel):
    name: str
    agent: str | None = None
    model: str | None = None
    base_url: str | None = None
    description: str | None = None


class ProfileListResponse(_CamelModel):
    items: list[ProfileSummary]


class ProfileDetail(_CamelModel):
    """Full profile dict from ``clawteam profile show``.

    The schema isn't fixed by ClawTeam (different agent kinds use
    different keys); we expose the raw dict so the UI can render whatever
    the user actually configured.
    """

    name: str
    raw: dict[str, Any] = Field(default_factory=dict)


class ProfileTestPayload(_CamelModel):
    prompt: str | None = Field(
        default=None,
        description=(
            "Custom smoke prompt. Leave blank to use ClawTeam's default "
            "(`Reply with exactly CLAWTEAM_PROFILE_OK`)."
        ),
    )
    cwd: str | None = None


class ProfileTestResponse(_CamelModel):
    success: bool
    output: str
    name: str


class ProfileSetPayload(_CamelModel):
    """Mirror of ``clawteam profile set`` flags.

    All fields optional — the CLI treats omitted flags as "leave as-is"
    when updating an existing profile, and as "unset" for new profiles.
    Lists (``envs`` / ``envMaps`` / ``args``) are passed through as
    repeated CLI options.
    """

    agent: str | None = Field(
        default=None,
        description="Default agent CLI name (claude/codex/cursor/gemini/kimi/nanobot/...)",
    )
    description: str | None = None
    command: str | None = Field(
        default=None,
        description="Exact command string (e.g. 'kimi --config-file ~/.kimi/config.toml')",
    )
    model: str | None = None
    base_url: str | None = None
    base_url_env: str | None = Field(
        default=None,
        description="Destination env var for base URL injection",
    )
    api_key_env: str | None = Field(
        default=None,
        description="Source env var holding the API key",
    )
    api_key_target_env: str | None = Field(
        default=None,
        description="Destination env var receiving the resolved API key",
    )
    envs: list[str] = Field(
        default_factory=list,
        description="Static env assignments in KEY=VALUE form",
    )
    env_maps: list[str] = Field(
        default_factory=list,
        description="Runtime env mappings in DEST=SOURCE_ENV form",
    )
    args: list[str] = Field(
        default_factory=list,
        description="Extra arguments appended to the agent command",
    )


# ──────────────────────────────────────────────────────────────────────


def _summarise(name: str, raw: dict[str, Any]) -> ProfileSummary:
    return ProfileSummary(
        name=name,
        agent=raw.get("agent"),
        model=raw.get("model"),
        base_url=raw.get("base_url"),
        description=raw.get("description"),
    )


@router.get("", response_model=ProfileListResponse)
async def list_profiles(_user: UserDep) -> ProfileListResponse:
    cli = get_clawteam_cli()
    profiles = await cli.profile_list()
    items = [_summarise(name, raw) for name, raw in profiles.items()]
    items.sort(key=lambda p: p.name)
    return ProfileListResponse(items=items)


@router.get("/{name}", response_model=ProfileDetail)
async def show_profile(
    name: Annotated[str, Path()],
    _user: UserDep,
) -> ProfileDetail:
    cli = get_clawteam_cli()
    try:
        raw = await cli.profile_show(name)
    except CliInvocationError as exc:
        # Distinguish "not found" from real errors when possible.
        if "not found" in (exc.stderr or "").lower() or exc.exit_code == 1:
            raise ApiError(
                "PROFILE_NOT_FOUND",
                f"profile {name!r} not found",
                status_code=404,
            ) from exc
        raise ApiError(
            "PROFILE_SHOW_FAILED",
            f"clawteam profile show failed: {exc.stderr.strip()[:300]}",
            status_code=502,
        ) from exc
    return ProfileDetail(name=name, raw=raw)


@router.post("/{name}/test", response_model=ProfileTestResponse)
async def test_profile(
    name: Annotated[str, Path()],
    _user: UserDep,
    payload: Annotated[ProfileTestPayload, Body()] = ProfileTestPayload(),
) -> ProfileTestResponse:
    cli = get_clawteam_cli()
    ok, output = await cli.profile_test(
        name, prompt=payload.prompt, cwd=payload.cwd,
    )
    return ProfileTestResponse(success=ok, output=output, name=name)


def _validate_kv_list(items: list[str], *, label: str) -> None:
    """Reject env / env-map entries that aren't ``KEY=VALUE``-shaped."""
    for s in items:
        if "=" not in s or not s.split("=", 1)[0].strip():
            raise ApiError(
                "INVALID_PROFILE_FIELD",
                f"{label} entry must be KEY=VALUE form (got {s!r})",
                status_code=400,
            )


@router.post("/{name}", response_model=ProfileDetail)
async def set_profile(
    name: Annotated[str, Path()],
    _user: UserDep,
    payload: Annotated[ProfileSetPayload, Body()],
) -> ProfileDetail:
    """Create or update a profile (proxy for ``clawteam profile set``).

    The handler validates KV-shaped fields client-side so the CLI
    invocation doesn't have to spend a roundtrip to reject malformed
    input. Successful invocations return the freshly re-read profile.
    """
    if not name or not name.strip():
        raise ApiError("INVALID_PROFILE_NAME", "profile name is required",
                       status_code=400)
    _validate_kv_list(payload.envs, label="env")
    _validate_kv_list(payload.env_maps, label="env-map")
    cli = get_clawteam_cli()
    try:
        raw = await cli.profile_set(
            name,
            agent=payload.agent,
            description=payload.description,
            command=payload.command,
            model=payload.model,
            base_url=payload.base_url,
            base_url_env=payload.base_url_env,
            api_key_env=payload.api_key_env,
            api_key_target_env=payload.api_key_target_env,
            envs=payload.envs,
            env_maps=payload.env_maps,
            args=payload.args,
        )
    except CliInvocationError as exc:
        raise ApiError(
            "PROFILE_SET_FAILED",
            f"clawteam profile set failed: {exc.stderr.strip()[:300] or exc}",
            status_code=502,
        ) from exc
    return ProfileDetail(name=name, raw=raw)


@router.delete("/{name}", status_code=204)
async def remove_profile(
    name: Annotated[str, Path()],
    _user: UserDep,
) -> None:
    cli = get_clawteam_cli()
    try:
        await cli.profile_remove(name)
    except CliInvocationError as exc:
        msg = (exc.stderr or "").lower()
        if "not found" in msg or exc.exit_code == 1:
            raise ApiError(
                "PROFILE_NOT_FOUND",
                f"profile {name!r} not found",
                status_code=404,
            ) from exc
        raise ApiError(
            "PROFILE_REMOVE_FAILED",
            f"clawteam profile remove failed: {exc.stderr.strip()[:300] or exc}",
            status_code=502,
        ) from exc
