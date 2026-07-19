"""Tests for :mod:`app.config`."""

from __future__ import annotations

import json

import pytest


from app import config as cfg_mod


class TestConfigDefaults:
    def test_creates_default_on_first_load(self) -> None:
        cfg = cfg_mod.load_config()
        assert cfg.csflow_port == cfg_mod.DEFAULT_PORT == 17017
        assert cfg.clawteam_board_port == cfg_mod.DEFAULT_CLAWTEAM_BOARD_PORT == 17018
        assert cfg.openclaw_gateway_url.startswith("http://127.0.0.1")

    def test_persists_default_to_disk(self) -> None:
        cfg_mod.load_config()
        from app import paths

        data = json.loads(paths.config_path().read_text())
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
    def test_storage_rejects_non_sqlite(self) -> None:
        with pytest.raises(ValueError):
            cfg_mod.StorageConfig(kind="postgres", url="postgres://x")

    def test_legacy_server_mode_keys_still_load(self) -> None:
        """Historical config.json files carried deployment_mode / broker /
        auth (and storage.url). The fields are gone but the files must keep
        loading — Pydantic ignores unknown keys (upgrade parity §3.7)."""
        cfg = cfg_mod.Config.model_validate({
            "deployment_mode": "local",
            "broker": None,
            "auth": None,
            "storage": {"kind": "sqlite", "url": None},
            "csflow_port": 17017,
        })
        assert cfg.csflow_port == 17017
        assert cfg.storage.kind == "sqlite"
        # Removed keys never round-trip back to disk.
        dumped = cfg.model_dump(mode="json", exclude_none=True)
        assert "deployment_mode" not in dumped
        assert "broker" not in dumped
        assert "auth" not in dumped


class TestPatchEnv:
    def test_injects_clawteam_user(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CLAWTEAM_USER", raising=False)
        cfg = cfg_mod.load_config()
        cfg_mod.patch_env_from_config(cfg)
        import os

        assert os.environ["CLAWTEAM_USER"] == cfg.default_user
