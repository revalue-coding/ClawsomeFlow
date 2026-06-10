"""The OpenClaw agent-settings cache is touched by sync (threadpool) handlers,
so its accessors must be lock-guarded — concurrent put/invalidate must never
raise "dictionary changed size during iteration"."""

from __future__ import annotations

import threading

from app.api import openclaw_agents as mod


class _FakePayload:
    def model_copy(self, deep: bool = False):  # noqa: ARG002 - mimic pydantic
        return self


def test_settings_cache_concurrent_put_invalidate_is_safe() -> None:
    stop = threading.Event()
    errors: list[BaseException] = []

    def writer() -> None:
        i = 0
        try:
            while not stop.is_set():
                mod._settings_cache_put(
                    user=f"u{i % 8}", agent_id="a", payload=_FakePayload(),
                )
                i += 1
        except BaseException as e:  # noqa: BLE001
            errors.append(e)

    th = threading.Thread(target=writer)
    th.start()
    try:
        for _ in range(3000):
            mod._settings_cache_invalidate(agent_id="a")  # iterates the dict
    except BaseException as e:  # noqa: BLE001
        errors.append(e)
    finally:
        stop.set()
        th.join()

    assert not errors, f"concurrent access raised: {errors[0]!r}"
