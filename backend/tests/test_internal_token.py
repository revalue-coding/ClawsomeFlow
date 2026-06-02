"""Tests for short-lived skill→Internal API token system."""

from __future__ import annotations

import time

import pytest

from app.config import load_config, save_config
from app.integrations import internal_token as it


@pytest.fixture
def configured_secret() -> None:
    """Persist a known secret so test results are reproducible."""
    cfg = load_config()
    cfg = cfg.model_copy(update={"internal_token_secret": "test-secret-shhh"})
    save_config(cfg)


def test_mint_then_verify_round_trip(configured_secret) -> None:
    tok = it.mint_token(request_id="req-1", user="alice")
    claims = it.verify_token(tok)
    assert claims.request_id == "req-1"
    assert claims.user == "alice"
    assert claims.purpose == "openclaw_agent_mgmt"
    assert claims.exp > int(time.time())


def test_invalid_signature_rejected(configured_secret) -> None:
    tok = it.mint_token(request_id="r", user="u")
    head, sig = tok.split(".")
    tampered = f"{head}.{'A' * len(sig)}"
    with pytest.raises(it.InvalidToken):
        it.verify_token(tampered)


def test_malformed_token_rejected(configured_secret) -> None:
    with pytest.raises(it.InvalidToken):
        it.verify_token("garbage")
    with pytest.raises(it.InvalidToken):
        it.verify_token("")


def test_expired_token_rejected(configured_secret) -> None:
    tok = it.mint_token(request_id="r", user="u", ttl_seconds=1)
    time.sleep(2.1)
    with pytest.raises(it.InvalidToken):
        it.verify_token(tok)


def test_secret_change_invalidates_token(configured_secret) -> None:
    tok = it.mint_token(request_id="r", user="u")
    cfg = load_config()
    cfg = cfg.model_copy(update={"internal_token_secret": "different-secret"})
    save_config(cfg)
    with pytest.raises(it.InvalidToken):
        it.verify_token(tok)


def test_mint_requires_request_id_and_user(configured_secret) -> None:
    with pytest.raises(ValueError):
        it.mint_token(request_id="", user="u")
    with pytest.raises(ValueError):
        it.mint_token(request_id="r", user="")


def test_mint_requires_positive_ttl(configured_secret) -> None:
    with pytest.raises(ValueError):
        it.mint_token(request_id="r", user="u", ttl_seconds=0)


def test_ensure_secret_initialised_creates_when_missing() -> None:
    cfg = load_config()
    cfg = cfg.model_copy(update={"internal_token_secret": None})
    new_cfg = it.ensure_secret_initialised(cfg)
    assert new_cfg.internal_token_secret
    assert new_cfg.internal_token_secret != cfg.internal_token_secret


def test_ensure_secret_initialised_idempotent() -> None:
    cfg = load_config()
    cfg = cfg.model_copy(update={"internal_token_secret": "preset"})
    new_cfg = it.ensure_secret_initialised(cfg)
    assert new_cfg is cfg  # short-circuit when already set
