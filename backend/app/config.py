"""ClawsomeFlow configuration model and loader.

Public API:
* :class:`Config` — Pydantic model describing the full ``config.json`` shape.
* :class:`StorageConfig` — nested storage section.
* :func:`load_config` — load + cache the active configuration (created on first call).
* :func:`save_config` — atomically persist a modified config.
* :func:`reset_config_cache` — clear the cached singleton (used by tests).

ClawsomeFlow is a single-user local deployment (SQLite + in-process locks).
The former "server" deployment mode was removed; configs written by older
versions may still contain ``deployment_mode`` / ``broker`` / ``auth`` keys —
Pydantic ignores unknown keys, so those files keep loading unchanged.
Environment variable ``CSFLOW_HOME`` overrides the data root (see
:mod:`app.paths`).
"""

from __future__ import annotations

import getpass
import json
import os
from pathlib import Path
from typing import Literal, Optional

from pydantic import BaseModel, Field

from app import paths
from app.fileutil import atomic_write_json, file_locked


# Default port chosen to avoid common conflicts (8080 / 3000 / 5000 / 8000 / 8888).
DEFAULT_PORT = 17017
# ClawTeam Board port for the bundled local instance.
DEFAULT_CLAWTEAM_BOARD_PORT = 17018


class StorageConfig(BaseModel):
    """Backing store for ClawsomeFlow's own data (SQLite only).

    ``url`` is retained solely so historical config.json files that carried
    it keep loading; it is never read.
    """

    kind: Literal["sqlite"] = "sqlite"
    url: str | None = None


class Config(BaseModel):
    """Top-level ClawsomeFlow configuration."""

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

    # ── External execution nodes (/api/external surface) ────────────────
    # Credentials / callback URL stay opt-in (None/empty). The Host/bind
    # surface defaults open (credential-gated); see external_api_expose.

    external_api_expose: bool = Field(
        default=True,
        description=(
            "When True (the default) the /api/external/* prefix accepts "
            "remote callers and ``csflow serve`` binds 0.0.0.0. Safe by "
            "default: that surface is credential-gated (one-time ticket / "
            "pairing secret), and the guard middleware rejects remote "
            "source IPs on every other surface (main /api, /ws, SPA) — "
            "see app.api._api_guard. Set False via ``csflow external "
            "expose off`` for a full loopback-only lockdown."
        ),
    )
    external_callback_base_url: str | None = Field(
        default=None,
        description=(
            "Base URL remote executors should use to call back into this "
            "instance (e.g. 'http://my-host:17017'). Used to build the "
            "callback_url embedded in outbound external-task dispatches. "
            "When None, outbound packages carry a relative callback path only."
        ),
    )
    external_pair_tokens: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Named pairing credentials for the /api/external/delegate surface "
            "(name -> secret). A remote ClawsomeFlow that wants to delegate "
            "Flows to this instance must present one of these secrets; "
            "locally, a Flow's remote_csflow node references the credential "
            "for the REMOTE side by name via ExternalNodeConfig.pair_token_ref "
            "in external_remote_targets. Generated via 'csflow external "
            "pair-token' — opt-in, never auto-created at init/upgrade."
        ),
    )
    external_remote_targets: dict[str, str] = Field(
        default_factory=dict,
        description=(
            "Outbound pairing credentials (name -> secret) used when THIS "
            "instance delegates to a remote ClawsomeFlow: "
            "ExternalNodeConfig.pair_token_ref names an entry here. The "
            "secret is whatever the remote side generated into its "
            "external_pair_tokens. Kept out of Flow specs so specs stay "
            "shareable without leaking credentials."
        ),
    )

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
