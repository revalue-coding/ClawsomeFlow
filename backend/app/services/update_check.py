"""Detect whether a newer **stable** release of ClawsomeFlow is available.

This is intentionally lightweight and side-effect free:

* It queries the PyPI JSON API (``https://pypi.org/pypi/clawsomeflow/json``)
  over plain HTTP — it never invokes ``pip``. So it cannot trigger the
  ``externally-managed-environment`` error on locked-down systems; that error
  only comes from ``pip`` mutating the system interpreter. The real upgrade
  path stays inside the dedicated ``~/.clawsomeflow/.venv`` for the same
  reason.

* Channel policy (product decision): if the *currently installed* version is a
  pre-release (``aN``/``bN``/``rcN``), we do **not** advertise any update — beta
  users opted into the beta channel and follow it manually. Only a final
  release prompts, and only ever to the latest **stable** release.

Version parsing/ordering is delegated to the canonical helpers in
:mod:`app.upgrade` so there is a single source of truth for PEP 440 ordering.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

import httpx

from app import __version__, upgrade
from app.logging_setup import get_logger

logger = get_logger("svc.update_check")

PYPI_JSON_URL = "https://pypi.org/pypi/clawsomeflow/json"
UPGRADE_SCRIPT_URL = "https://clawsomeflow.com/upgrade.sh"

_FETCH_TIMEOUT_SECONDS = 3.0
_CACHE_TTL_SECONDS = 6 * 60 * 60  # 6h


@dataclass
class _Cache:
    value: str | None
    fetched_at: float


# Process-wide cache of the latest stable version string. ``value is None``
# means "the last fetch failed or found nothing" — still cached (briefly) so a
# flaky network doesn't hammer PyPI on every page load.
_cache: _Cache | None = None


def is_prerelease(version: str) -> bool:
    """Return True when *version* carries an ``aN``/``bN``/``rcN`` suffix."""
    m = upgrade._VERSION_RE.match(version)
    if not m:
        # Unparseable — treat conservatively as a prerelease so we never nudge
        # someone on an odd build toward a "downgrade".
        return True
    return any(tag in version for tag in ("a", "b", "rc"))


def _pick_latest_stable(release_keys: object) -> str | None:
    """Pick the greatest **stable** version from PyPI ``releases`` keys.

    We scan the keys ourselves rather than trusting ``info.version`` so the
    result is unambiguously the latest *final* release regardless of how PyPI
    reports the "latest" field.
    """
    if not isinstance(release_keys, dict):
        return None
    stable = [
        v
        for v in release_keys
        if isinstance(v, str)
        and upgrade._VERSION_RE.match(v)
        and not is_prerelease(v)
    ]
    if not stable:
        return None
    return max(stable, key=upgrade._key)


def _fetch_latest_stable_uncached() -> str | None:
    try:
        resp = httpx.get(PYPI_JSON_URL, timeout=_FETCH_TIMEOUT_SECONDS)
        resp.raise_for_status()
        data = resp.json()
    except Exception as exc:  # network / json / http — all non-fatal
        logger.warning("update_check_fetch_failed", error=str(exc))
        return None
    return _pick_latest_stable(data.get("releases") if isinstance(data, dict) else None)


def fetch_latest_stable(*, force: bool = False, now: float | None = None) -> str | None:
    """Return the latest stable version on PyPI, using a TTL cache.

    ``force`` bypasses the cache; ``now`` is injectable for tests.
    """
    global _cache
    current = time.monotonic() if now is None else now
    if not force and _cache is not None and (current - _cache.fetched_at) < _CACHE_TTL_SECONDS:
        return _cache.value
    value = _fetch_latest_stable_uncached()
    _cache = _Cache(value=value, fetched_at=current)
    return value


def reset_cache() -> None:
    """Clear the in-process cache (used by tests)."""
    global _cache
    _cache = None


@dataclass
class UpdateStatus:
    current_version: str
    latest_version: str | None
    update_available: bool
    is_prerelease: bool
    upgrade_script_url: str = UPGRADE_SCRIPT_URL


def compute_update_status(*, force: bool = False) -> UpdateStatus:
    """Resolve whether a newer stable release should be advertised.

    Pre-release installs short-circuit without any network call.
    """
    current = __version__
    if is_prerelease(current):
        return UpdateStatus(
            current_version=current,
            latest_version=None,
            update_available=False,
            is_prerelease=True,
        )
    latest = fetch_latest_stable(force=force)
    available = bool(latest) and upgrade._gt(latest, current)
    return UpdateStatus(
        current_version=current,
        latest_version=latest,
        update_available=available,
        is_prerelease=False,
    )
