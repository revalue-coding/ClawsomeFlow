"""Tests for deployment-mode capability policy and factory routing."""

from __future__ import annotations

from types import SimpleNamespace

from app import config as cfg_mod
from app.concurrency import _AsyncioBackend, get_lock_manager, reset_lock_manager
from app.deployment import get_deployment_capabilities
from app.storage import get_storage, reset_storage


def test_local_capabilities() -> None:
    caps = get_deployment_capabilities(cfg_mod.Config())
    assert caps.mode == "local"
    assert caps.requires_request_identity_headers is False
    assert caps.allow_all_users_query is True
    assert caps.allow_native_directory_picker is True
    assert caps.auto_spawn_board_proxy is True
    assert caps.board_url_uses_localhost is True


def test_server_capabilities() -> None:
    cfg = cfg_mod.Config(
        deployment_mode="server",
        broker=cfg_mod.BrokerConfig(kind="redis", url="redis://localhost:6379"),
        storage=cfg_mod.StorageConfig(kind="postgres", url="postgres://localhost/x"),
    )
    caps = get_deployment_capabilities(cfg)
    assert caps.mode == "server"
    assert caps.requires_request_identity_headers is True
    assert caps.allow_all_users_query is False
    assert caps.allow_native_directory_picker is False
    assert caps.auto_spawn_board_proxy is False
    assert caps.board_url_uses_localhost is False


def test_local_storage_factory_does_not_call_server_factory(monkeypatch) -> None:
    reset_storage()
    called = {"server": False}

    def _boom(_config):
        called["server"] = True
        raise AssertionError("local mode should not call server storage factory")

    monkeypatch.setattr("app.storage.server.create_server_storage", _boom)
    storage = get_storage(SimpleNamespace(deployment_mode="local"))
    assert storage.__class__.__name__ == "SqliteStorage"
    assert called["server"] is False
    reset_storage()


def test_server_storage_factory_routes_to_server_factory(monkeypatch) -> None:
    reset_storage()
    called: dict[str, object] = {}

    class _FakeStorage:
        def init_schema(self) -> None:
            called["init_schema"] = True

        def close(self) -> None:
            called["close"] = True

    fake = _FakeStorage()

    def _fake_factory(config):
        called["mode"] = config.deployment_mode
        return fake

    monkeypatch.setattr("app.storage.server.create_server_storage", _fake_factory)
    resolved = get_storage(SimpleNamespace(deployment_mode="server"))
    assert resolved is fake
    assert called["mode"] == "server"
    assert called["init_schema"] is True
    reset_storage()
    assert called["close"] is True


def test_local_lock_factory_does_not_call_server_factory(monkeypatch) -> None:
    reset_lock_manager()
    called = {"server": False}

    def _boom(_config):
        called["server"] = True
        raise AssertionError("local mode should not call server lock factory")

    monkeypatch.setattr("app.concurrency_server.create_server_lock_backend", _boom)
    manager = get_lock_manager(SimpleNamespace(deployment_mode="local"))
    assert isinstance(manager._backend, _AsyncioBackend)
    assert called["server"] is False
    reset_lock_manager()


def test_server_lock_factory_routes_to_server_factory(monkeypatch) -> None:
    reset_lock_manager()
    called: dict[str, object] = {}

    class _FakeBackend:
        async def acquire(self, key: str, timeout: float) -> None:  # pragma: no cover - not called
            return None

        async def release(self, key: str) -> None:  # pragma: no cover - not called
            return None

    fake = _FakeBackend()

    def _fake_factory(config):
        called["mode"] = config.deployment_mode
        return fake

    monkeypatch.setattr("app.concurrency_server.create_server_lock_backend", _fake_factory)
    manager = get_lock_manager(SimpleNamespace(deployment_mode="server"))
    assert manager._backend is fake
    assert called["mode"] == "server"
    reset_lock_manager()
