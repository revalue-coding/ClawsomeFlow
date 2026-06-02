"""Server-mode storage entrypoint.

Future PostgreSQL-backed storage wiring must live in this module so local-mode
storage code stays stable.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.config import Config

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.storage import StorageBackend


def create_server_storage(_config: Config) -> "StorageBackend":
    """Build the server-mode storage backend.

    Currently intentionally unimplemented; local mode is the production path.
    """
    raise RuntimeError(
        "server mode is not ready: storage.kind='postgres' backend "
        "is not implemented yet"
    )


__all__ = ["create_server_storage"]
