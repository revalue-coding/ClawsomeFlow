"""Server-mode lock backend entrypoint.

Future Redis-based distributed locking should be implemented here so local-mode
locking stays isolated in :mod:`app.concurrency`.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.concurrency import LockBackend


def create_server_lock_backend(_config: Config) -> "LockBackend":
    """Build the server-mode lock backend."""
    raise RuntimeError(
        "server mode is not ready: Redis lock backend is not implemented yet"
    )


__all__ = ["create_server_lock_backend"]
