"""ClawsomeFlow configuration model and loader.

Public API:
* :class:`Config` — Pydantic model describing the full ``config.json`` shape.
* :class:`StorageConfig` / :class:`BrokerConfig` / :class:`AuthConfig` — nested.
* :func:`load_config` — load + cache the active configuration (created on first call).
* :func:`save_config` — atomically persist a modified config.
* :func:`reset_config_cache` — clear the cached singleton (used by tests).

The default config matches the **local mode** described in plan §11.1 and is
created on first read. Environment variable ``CSFLOW_HOME`` overrides the
data root (see :mod:`app.paths`).
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from app import paths
from app.fileutil import atomic_write_json, file_locked


# Default port chosen to avoid common conflicts (8080 / 3000 / 5000 / 8000 / 8888).
DEFAULT_PORT = 17017
# ClawTeam Board port for the bundled local instance.
DEFAULT_CLAWTEAM_BOARD_PORT = 17018


class StorageConfig(BaseModel):
    """Backing store for ClawsomeFlow's own data."""

    kind: Literal["sqlite", "postgres"] = "sqlite"
    url: str | None = None  # required when kind == "postgres"

    @model_validator(mode="after")
    def _validate(self) -> "StorageConfig":
        if self.kind == "postgres" and not self.url:
            raise ValueError("storage.url is required when storage.kind == 'postgres'")
        return self


class BrokerConfig(BaseModel):
    """Optional message broker (server mode only)."""

    kind: Literal["redis"]
    url: str


class AuthConfig(BaseModel):
    """Authentication (server mode). ``None`` means local OS-user auth."""

    kind: Literal["oauth2"]
    issuer: str
    client_id: str | None = None
    audience: str | None = None


class Config(BaseModel):
    """Top-level ClawsomeFlow configuration."""

    deployment_mode: Literal["local", "server"] = "local"
    csflow_port: int = DEFAULT_PORT
    clawteam_board_port: int = DEFAULT_CLAWTEAM_BOARD_PORT
    default_user: str = Field(default_factory=lambda: getpass.getuser() or "csflow")

    # Paths to other tools' data dirs (rarely need to be overridden)
    clawteam_data_dir: str | None = None  # default ~/.clawteam (resolved by clawteam itself)
    openclaw_home: str = "~/.openclaw"
    openclaw_gateway_url: str = "http://127.0.0.1:18789"

    # Whether the WebUI may check PyPI for a newer stable release and offer a
    # one-click upgrade. Disable to suppress the outbound version check.
    update_check_enabled: bool = True

    # DEPRECATED (kept only so old config.json still loads): the run webhook
    # is now configured per-Flow in spec.variables[csflow.notify_webhooks],
    # which also supports multiple channels. These global fields are no longer
    # read by app.services.run_notify. Safe to leave set; they are simply
    # ignored. See app/api/flows.py notify-webhooks endpoints.
    notify_webhook_url: str | None = None
    notify_webhook_format: str | None = None

    storage: StorageConfig = Field(default_factory=StorageConfig)
    broker: BrokerConfig | None = None
    auth: AuthConfig | None = None

    internal_token_secret: str | None = Field(
        default=None,
        description=(
            "HMAC secret used to mint short-lived bearer tokens for the "
            "skill→Internal API loopback. Initialised on first init "
            "(see app.integrations.internal_token.ensure_secret_initialised)."
        ),
    )

    api_token: str | None = Field(
        default=None,
        description=(
            "Long-lived bearer token guarding the public /api surface "
            "(OpenClaw gateway paradigm: loopback bind + token). Auto-generated "
            "at init and stored only in the private ~/.clawsomeflow/config.json "
            "(never committed). When None the API guard is a full no-op, so dev "
            "and tests are unaffected. See app.api._api_guard and "
            "app.integrations.internal_token.ensure_api_token_initialised."
        ),
    )

    @model_validator(mode="after")
    def _validate_mode(self) -> "Config":
        if self.deployment_mode == "local":
            if self.storage.kind != "sqlite":
                raise ValueError("local mode requires storage.kind == 'sqlite'")
            if self.broker is not None:
                raise ValueError("local mode does not use 'broker'")
            if self.auth is not None:
                raise ValueError("local mode does not use 'auth'")
        if self.deployment_mode == "server":
            if self.broker is None:
                raise ValueError("server mode requires 'broker' to be configured")
            if self.storage.kind != "postgres":
                raise ValueError("server mode requires storage.kind == 'postgres'")
        return self

    @property
    def openclaw_home_path(self) -> Path:
        """Resolve openclaw_home to an absolute Path."""
        return Path(self.openclaw_home).expanduser()


# ──────────────────────────────────────────────────────────────────────
# Loading / persisting
# ──────────────────────────────────────────────────────────────────────

_cached: Config | None = None


def load_config(*, force_reload: bool = False) -> Config:
    """Load the active configuration, creating defaults on first call.

    The result is cached in-process; pass ``force_reload=True`` (or call
    :func:`reset_config_cache`) to discard the cache.
    """
    global _cached
    if _cached is not None and not force_reload:
        return _cached

    path = paths.config_path()
    if path.exists():
        with path.open(encoding="utf-8") as fh:
            data = json.load(fh)
        cfg = Config.model_validate(data)
    else:
        cfg = Config()
        save_config(cfg)
    _cached = cfg
    return cfg


def save_config(config: Config) -> None:
    """Atomically persist *config* to ``config.json`` (with file lock)."""
    path = paths.config_path()
    with file_locked(path):
        atomic_write_json(path, config.model_dump(mode="json", exclude_none=True))
    # Refresh cache so subsequent reads see the change.
    global _cached
    _cached = config


def reset_config_cache() -> None:
    """Clear the cached config singleton (used by tests)."""
    global _cached
    _cached = None


def patch_env_from_config(config: Optional[Config] = None) -> None:
    """Export ``CLAWTEAM_USER`` (and friends) to env from config.

    All ClawTeam CLI/MCP calls expect this to be set; centralising the
    injection here ensures consistency.
    """
    cfg = config or load_config()
    os.environ.setdefault("CLAWTEAM_USER", cfg.default_user)
    if cfg.clawteam_data_dir:
        os.environ.setdefault("CLAWTEAM_DATA_DIR", cfg.clawteam_data_dir)
