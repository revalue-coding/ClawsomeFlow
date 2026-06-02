"""Deployment-mode capability policy.

This module is the single source of truth for behavior switches between
``local`` and ``server`` mode. Callers should consume these capability flags
instead of scattering ``if cfg.deployment_mode == ...`` checks throughout
business logic.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from app.config import Config, load_config

DeploymentMode = Literal["local", "server"]


@dataclass(frozen=True, slots=True)
class DeploymentCapabilities:
    """Feature toggles driven by deployment mode."""

    mode: DeploymentMode
    requires_request_identity_headers: bool
    allow_all_users_query: bool
    allow_native_directory_picker: bool
    auto_spawn_board_proxy: bool
    board_url_uses_localhost: bool


_LOCAL_CAPS = DeploymentCapabilities(
    mode="local",
    requires_request_identity_headers=False,
    allow_all_users_query=True,
    allow_native_directory_picker=True,
    auto_spawn_board_proxy=True,
    board_url_uses_localhost=True,
)

_SERVER_CAPS = DeploymentCapabilities(
    mode="server",
    requires_request_identity_headers=True,
    allow_all_users_query=False,
    allow_native_directory_picker=False,
    auto_spawn_board_proxy=False,
    board_url_uses_localhost=False,
)

_BY_MODE: dict[DeploymentMode, DeploymentCapabilities] = {
    "local": _LOCAL_CAPS,
    "server": _SERVER_CAPS,
}


def get_deployment_capabilities(config: Config | None = None) -> DeploymentCapabilities:
    """Return capability flags for the active deployment mode."""
    cfg = config or load_config()
    try:
        return _BY_MODE[cfg.deployment_mode]
    except KeyError as exc:  # pragma: no cover - defensive
        raise RuntimeError(f"unsupported deployment mode: {cfg.deployment_mode!r}") from exc


def is_local_mode(config: Config | None = None) -> bool:
    return get_deployment_capabilities(config).mode == "local"


def is_server_mode(config: Config | None = None) -> bool:
    return get_deployment_capabilities(config).mode == "server"


__all__ = [
    "DeploymentCapabilities",
    "DeploymentMode",
    "get_deployment_capabilities",
    "is_local_mode",
    "is_server_mode",
]
