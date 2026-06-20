"""Idempotent ClawTeam spawn timing defaults in ``~/.clawteam/config.json``.

ClawTeam's tmux spawn path calls ``_confirm_workspace_trust_if_prompted`` with
``spawn_ready_timeout`` from this file (via ``load_config()``, **not** env vars).
When the value is the stock **30s**, a spawn with no trust dialog wastes the full
window **while holding ClawsomeFlow's repo locks** — the stable ~30s spawn delay.

We lower it to :data:`DEFAULT_SPAWN_READY_TIMEOUT_SEC` on init/upgrade so
upgrade-only users converge with fresh deploys (DEV.md upgrade-parity).

Measured trust-dialog handling (tmux, fresh worktree, send Enter once):
* Claude 2.1.x — ~1.3s end-to-end
* Codex 0.139 — typically ~0.8–1.4s; cold start can exceed 2s (rare)
* Gemini — varies; ClawTeam trust handling is best-effort

Best-effort: never raises; a failed write must not block deploy/upgrade.
"""

from __future__ import annotations

import json
from pathlib import Path

from app.logging_setup import get_logger

logger = get_logger("integrations.clawteam_spawn_config")

DEFAULT_SPAWN_READY_TIMEOUT_SEC = 2.0


def clawteam_config_path() -> Path:
    return Path.home() / ".clawteam" / "config.json"


def ensure_spawn_ready_timeout(
    *,
    seconds: float = DEFAULT_SPAWN_READY_TIMEOUT_SEC,
    force: bool = False,
) -> bool:
    """Ensure ``spawn_ready_timeout`` in ClawTeam's config file.

    Writes when the key is missing, still at the stock 30s default, or differs
    from *seconds* (converge upgrade/fresh installs). Preserves all other keys.

    Returns True if the file was created or modified. Never raises.
    ``force`` writes even when the current value already matches (tests).
    """
    try:
        path = clawteam_config_path()
        data: dict = {}
        if path.exists():
            try:
                loaded = json.loads(path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    data = loaded
                else:
                    logger.warning(
                        "clawteam_config_not_object",
                        path=str(path),
                    )
                    return False
            except (json.JSONDecodeError, OSError) as exc:
                logger.warning(
                    "clawteam_config_unreadable",
                    path=str(path),
                    error=str(exc),
                )
                return False

        current = data.get("spawn_ready_timeout")
        if not force and current is not None:
            try:
                if abs(float(current) - float(seconds)) < 1e-9:
                    return False
            except (TypeError, ValueError):
                pass

        data["spawn_ready_timeout"] = float(seconds)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
        logger.info(
            "clawteam_spawn_ready_timeout_set",
            path=str(path),
            seconds=seconds,
            previous=current,
        )
        return True
    except Exception as exc:  # pragma: no cover - defensive; never block init
        logger.warning("clawteam_spawn_ready_timeout_failed", error=str(exc))
        return False


__all__ = [
    "DEFAULT_SPAWN_READY_TIMEOUT_SEC",
    "clawteam_config_path",
    "ensure_spawn_ready_timeout",
]
