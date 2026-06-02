"""Tests for :mod:`app.concurrency`."""

from __future__ import annotations

import asyncio

import pytest

from app.concurrency import LockManager, _AsyncioBackend, get_lock_manager


@pytest.mark.asyncio
async def test_local_backend_is_default() -> None:
    mgr = get_lock_manager()
    assert isinstance(mgr, LockManager)
    assert isinstance(mgr._backend, _AsyncioBackend)


@pytest.mark.asyncio
async def test_lock_serialises_concurrent_coroutines() -> None:
    mgr = LockManager(_AsyncioBackend())
    counter = {"n": 0}

    async def worker() -> None:
        for _ in range(50):
            async with mgr.lock("counter"):
                cur = counter["n"]
                await asyncio.sleep(0)  # context switch
                counter["n"] = cur + 1

    await asyncio.gather(*[worker() for _ in range(8)])
    assert counter["n"] == 50 * 8


@pytest.mark.asyncio
async def test_independent_keys_dont_block() -> None:
    mgr = LockManager(_AsyncioBackend())
    timings: dict[str, float] = {}

    async def hold(key: str, dur: float) -> None:
        async with mgr.lock(key):
            await asyncio.sleep(dur)
            timings[key] = dur

    # If keys block each other this would take ~0.4s; independent => ~0.2s.
    started = asyncio.get_event_loop().time()
    await asyncio.gather(hold("a", 0.2), hold("b", 0.2))
    elapsed = asyncio.get_event_loop().time() - started
    assert elapsed < 0.35, f"Keys appear to block each other: elapsed={elapsed}"


@pytest.mark.asyncio
async def test_timeout_raises() -> None:
    mgr = LockManager(_AsyncioBackend())

    async with mgr.lock("hot"):
        with pytest.raises(asyncio.TimeoutError):
            async with mgr.lock("hot", timeout=0.05):
                pass  # pragma: no cover - should not reach
