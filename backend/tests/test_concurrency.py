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


@pytest.mark.asyncio
async def test_lock_key_evicted_after_release() -> None:
    """Keys are per-run/per-repo; entries must not accumulate forever."""
    backend = _AsyncioBackend()
    mgr = LockManager(backend)

    for i in range(100):
        async with mgr.lock(f"team_spawn:csflow-run-{i}"):
            pass

    assert backend._locks == {}
    assert dict(backend._refs) == {}


@pytest.mark.asyncio
async def test_lock_key_not_evicted_while_waiter_queued() -> None:
    backend = _AsyncioBackend()
    mgr = LockManager(backend)
    order: list[str] = []

    async def second() -> None:
        async with mgr.lock("k"):
            order.append("second")

    async with mgr.lock("k"):
        task = asyncio.create_task(second())
        await asyncio.sleep(0.05)  # let the waiter queue up
        assert "k" in backend._locks
        order.append("first")
    await task
    assert order == ["first", "second"]
    assert backend._locks == {}


@pytest.mark.asyncio
async def test_lock_usable_after_timeout_failure() -> None:
    """A timed-out waiter must never leave the key stuck/locked."""
    mgr = LockManager(_AsyncioBackend())

    async with mgr.lock("hot"):
        with pytest.raises(asyncio.TimeoutError):
            async with mgr.lock("hot", timeout=0.05):
                pass  # pragma: no cover

    # Holder released; the key must be immediately acquirable again.
    async with mgr.lock("hot", timeout=0.5):
        pass


@pytest.mark.asyncio
async def test_acquire_race_with_timeout_releases_lock() -> None:
    """If acquire succeeds in the same instant the timeout cancels it, the
    lock must be released (not orphaned in a locked state)."""
    backend = _AsyncioBackend()
    lock = asyncio.Lock()
    await lock.acquire()

    async def _release_after(delay: float) -> None:
        await asyncio.sleep(delay)
        lock.release()

    releaser = asyncio.create_task(_release_after(0.05))
    # Whichever way the race resolves, the invariant is: after the call
    # returns/raises, the lock is either held by us (success) or free.
    try:
        await backend._acquire_with_timeout(lock, timeout=0.05)
        acquired = True
    except asyncio.TimeoutError:
        acquired = False
    await releaser
    if acquired:
        lock.release()
    assert not lock.locked()


@pytest.mark.asyncio
async def test_waiter_cancellation_does_not_orphan_lock() -> None:
    """Cancelling a queued waiter must not block later acquires."""
    mgr = LockManager(_AsyncioBackend())
    entered = asyncio.Event()

    async def holder() -> None:
        async with mgr.lock("k", timeout=5):
            entered.set()
            await asyncio.sleep(0.2)

    async def waiter() -> None:
        async with mgr.lock("k", timeout=5):
            pass  # pragma: no cover — cancelled while queued

    hold_task = asyncio.create_task(holder())
    await entered.wait()
    wait_task = asyncio.create_task(waiter())
    await asyncio.sleep(0.05)
    wait_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await wait_task
    await hold_task
    # Lock must be free again for a fresh acquire.
    async with mgr.lock("k", timeout=0.5):
        pass


@pytest.mark.asyncio
async def test_lock_stress_many_keys_and_waiters() -> None:
    """Concurrency smoke: 20 keys × 10 workers, exclusive counters intact."""
    backend = _AsyncioBackend()
    mgr = LockManager(backend)
    counters = {f"key-{k}": 0 for k in range(20)}

    async def worker(key: str) -> None:
        for _ in range(10):
            async with mgr.lock(key, timeout=10):
                cur = counters[key]
                await asyncio.sleep(0)
                counters[key] = cur + 1

    await asyncio.gather(*[
        worker(f"key-{k}") for k in range(20) for _ in range(10)
    ])
    assert all(v == 100 for v in counters.values())
    assert backend._locks == {}
