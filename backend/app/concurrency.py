"""Lock manager abstraction (local asyncio + future Redis).

Public API:
* :class:`LockManager` — acquire a named async lock; ``async with mgr.lock(key)``.
* :func:`get_lock_manager` — lazy singleton (mode resolved from :class:`Config`).
* :func:`reset_lock_manager` — used by tests.

Lock-key naming convention (DEV.md §8):
* ``openclaw_json`` — global, edits to ``~/.openclaw/openclaw.json``.
* ``clawteam_main_repo:{repo_path}`` — protect ``clawteam spawn`` against
  concurrent ``git worktree add`` on the same main repo (covers both TUI
  agents using ``Flow.repo`` and OpenClaw agents using their own main repo).
* ``team_spawn:{team_name}`` — serialise per-team spawn calls to avoid
  tmux session/window creation races.
* ``run:{run_id}:owner`` — server-mode RunController master election
  (Redis SETNX + TTL; not used in local mode).
"""

from __future__ import annotations

import asyncio
import time
from collections import defaultdict
from contextlib import asynccontextmanager
from typing import AsyncIterator, Callable, Protocol

from app import logging_setup
from app.config import Config, load_config


class LockBackend(Protocol):
    """Backend-agnostic lock interface used by :class:`LockManager`."""

    async def acquire(self, key: str, timeout: float) -> None: ...
    async def release(self, key: str) -> None: ...


class _AsyncioBackend:
    """In-process backend using :class:`asyncio.Lock`."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

    async def acquire(self, key: str, timeout: float) -> None:
        lock = self._locks[key]
        if timeout > 0:
            await asyncio.wait_for(lock.acquire(), timeout=timeout)
        else:
            await lock.acquire()

    async def release(self, key: str) -> None:
        # Defensive: defaultdict guarantees the lock exists.
        lock = self._locks.get(key)
        if lock and lock.locked():
            lock.release()


class _RedisBackend:  # pragma: no cover — server mode, exercised in integration tests
    """Server-mode backend using Redis SETNX + TTL.

    Stub implementation; wired in Phase 9 when server mode lands.
    Documented here so the shape is clear from day one.
    """

    def __init__(self, url: str) -> None:
        self._url = url
        self._client = None  # lazy connect

    async def acquire(self, key: str, timeout: float) -> None:
        raise NotImplementedError("Redis lock backend is not yet implemented (P1).")

    async def release(self, key: str) -> None:
        raise NotImplementedError("Redis lock backend is not yet implemented (P1).")


class LockManager:
    """Acquire / release named locks; backend chosen by deployment mode."""

    def __init__(self, backend: LockBackend):
        self._backend = backend

    @asynccontextmanager
    async def lock(self, key: str, *, timeout: float = 30.0) -> AsyncIterator[None]:
        """Acquire *key* (raising on timeout). Logs wait time on release path."""
        started = time.monotonic()
        await self._backend.acquire(key, timeout)
        wait_ms = (time.monotonic() - started) * 1000
        logging_setup.lock_acquired(key=key, wait_ms=wait_ms)
        try:
            yield
        finally:
            await self._backend.release(key)


# ──────────────────────────────────────────────────────────────────────
# Singleton
# ──────────────────────────────────────────────────────────────────────

_singleton: LockManager | None = None


def _create_local_lock_backend(_config: Config) -> LockBackend:
    return _AsyncioBackend()


def _create_server_lock_backend(config: Config) -> LockBackend:
    from app.concurrency_server import create_server_lock_backend  # server-only module
    return create_server_lock_backend(config)


_BACKEND_FACTORY_BY_MODE: dict[str, Callable[[Config], LockBackend]] = {
    "local": _create_local_lock_backend,
    "server": _create_server_lock_backend,
}


def get_lock_manager(config: Config | None = None) -> LockManager:
    """Return the process-wide :class:`LockManager`, creating it on demand."""
    global _singleton
    if _singleton is not None:
        return _singleton
    cfg = config or load_config()
    factory = _BACKEND_FACTORY_BY_MODE.get(cfg.deployment_mode)
    if factory is None:  # pragma: no cover - defensive
        raise RuntimeError(f"unsupported deployment mode: {cfg.deployment_mode!r}")
    backend = factory(cfg)
    _singleton = LockManager(backend)
    return _singleton


def reset_lock_manager() -> None:
    """Drop the cached singleton (used by tests)."""
    global _singleton
    _singleton = None


__all__ = [
    "LockBackend",
    "LockManager",
    "get_lock_manager",
    "reset_lock_manager",
]
