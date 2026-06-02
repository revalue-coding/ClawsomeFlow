"""Tests for :mod:`app.config`."""

from __future__ import annotations

import json

import pytest

from app import config as cfg_mod
from app.concurrency import get_lock_manager, reset_lock_manager
from app.storage import get_storage, reset_storage


class TestConfigDefaults:
    def test_creates_default_on_first_load(self) -> None:
        cfg = cfg_mod.load_config()
        assert cfg.deployment_mode == "local"
        assert cfg.csflow_port == cfg_mod.DEFAULT_PORT == 17017
        assert cfg.clawteam_board_port == cfg_mod.DEFAULT_CLAWTEAM_BOARD_PORT == 17018
        assert cfg.openclaw_gateway_url.startswith("http://127.0.0.1")

    def test_persists_default_to_disk(self) -> None:
        cfg_mod.load_config()
        from app import paths

        data = json.loads(paths.config_path().read_text())
        assert data["deployment_mode"] == "local"
        assert data["csflow_port"] == 17017


class TestConfigPersistence:
    def test_save_then_reload(self) -> None:
        cfg = cfg_mod.load_config()
        cfg.csflow_port = 19999
        cfg_mod.save_config(cfg)

        cfg_mod.reset_config_cache()
        loaded = cfg_mod.load_config()
        assert loaded.csflow_port == 19999

    def test_force_reload(self) -> None:
        cfg1 = cfg_mod.load_config()
        cfg1.csflow_port = 22222
        cfg_mod.save_config(cfg1)

        # Without force_reload, the cached singleton is returned (and reflects save above).
        cfg2 = cfg_mod.load_config()
        assert cfg2.csflow_port == 22222

        cfg3 = cfg_mod.load_config(force_reload=True)
        assert cfg3.csflow_port == 22222


class TestConfigValidation:
    def test_local_mode_requires_sqlite(self) -> None:
        with pytest.raises(ValueError, match="local mode requires storage.kind == 'sqlite'"):
            cfg_mod.Config(
                deployment_mode="local",
                storage=cfg_mod.StorageConfig(kind="postgres", url="postgres://x"),
            )

    def test_local_mode_rejects_broker(self) -> None:
        with pytest.raises(ValueError, match="local mode does not use 'broker'"):
            cfg_mod.Config(
                deployment_mode="local",
                broker=cfg_mod.BrokerConfig(kind="redis", url="redis://localhost:6379"),
            )

    def test_local_mode_rejects_auth(self) -> None:
        with pytest.raises(ValueError, match="local mode does not use 'auth'"):
            cfg_mod.Config(
                deployment_mode="local",
                auth=cfg_mod.AuthConfig(kind="oauth2", issuer="https://issuer.example"),
            )

    def test_server_mode_requires_broker(self) -> None:
        with pytest.raises(ValueError, match="broker"):
            cfg_mod.Config(
                deployment_mode="server",
                storage=cfg_mod.StorageConfig(kind="postgres", url="postgres://x"),
            )

    def test_server_mode_requires_postgres(self) -> None:
        with pytest.raises(ValueError, match="postgres"):
            cfg_mod.Config(
                deployment_mode="server",
                broker=cfg_mod.BrokerConfig(kind="redis", url="redis://r"),
            )

    def test_storage_postgres_requires_url(self) -> None:
        with pytest.raises(ValueError, match="storage.url"):
            cfg_mod.StorageConfig(kind="postgres", url=None)

    def test_server_mode_storage_backend_fail_fast(self) -> None:
        reset_storage()
        cfg = cfg_mod.Config(
            deployment_mode="server",
            broker=cfg_mod.BrokerConfig(kind="redis", url="redis://localhost:6379"),
            storage=cfg_mod.StorageConfig(kind="postgres", url="postgres://localhost/x"),
        )
        with pytest.raises(RuntimeError, match="storage.kind='postgres'"):
            get_storage(cfg)
        reset_storage()

    def test_server_mode_lock_backend_fail_fast(self) -> None:
        reset_lock_manager()
        cfg = cfg_mod.Config(
            deployment_mode="server",
            broker=cfg_mod.BrokerConfig(kind="redis", url="redis://localhost:6379"),
            storage=cfg_mod.StorageConfig(kind="postgres", url="postgres://localhost/x"),
        )
        with pytest.raises(RuntimeError, match="Redis lock backend"):
            get_lock_manager(cfg)
        reset_lock_manager()


class TestPatchEnv:
    def test_injects_clawteam_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAWTEAM_USER", raising=False)
        cfg = cfg_mod.load_config()
        cfg_mod.patch_env_from_config(cfg)
        import os

        assert os.environ["CLAWTEAM_USER"] == cfg.default_user
