"""Short-lived bearer tokens for skill→ClawsomeFlow callbacks.

Skills installed in OpenClaw run inside the agent's process and need to call
back into ClawsomeFlow's Internal API (``/api/internal/openclaw/*``).
Forwarding the user's session cookie is impractical; instead we mint a
**short-lived HMAC token** when we dispatch the skill, embed it in the
agent's prompt as ``$CSFLOW_TOKEN``, and verify it server-side on every
internal endpoint.

Properties:

* HMAC-SHA256 over a JSON header (``request_id`` + ``user`` + ``purpose`` +
  ``exp`` epoch seconds).
* TTL defaults to 5 minutes (covers most NL agent-creation round trips).
* Single secret (``Config.internal_token_secret``) — rotated on every
  ``csflow init --rotate-secret`` (Phase 9).
* Tokens are URL-safe base64 (``token = base64(header) + "." + base64(sig)``)
  so they paste cleanly into shell prompts.

Public API:

* :func:`mint_token` — issue a new token bound to a request.
* :func:`verify_token` — verify + decode (raises :class:`InvalidToken`).
* :class:`TokenClaims` — decoded payload.
* FastAPI dep helpers live in :mod:`app.api._auth_internal`.
"""

from __future__ import annotations

import base64
import hmac
import json
import secrets
import time
from dataclasses import dataclass
from hashlib import sha256

from app.config import Config, load_config


_DEFAULT_TTL_SEC = 300  # 5 minutes
_PURPOSE_AGENT_MGMT = "openclaw_agent_mgmt"


class InvalidToken(Exception):
    """Raised when a token fails to verify (signature, expiry, format)."""


@dataclass(frozen=True)
class TokenClaims:
    """Decoded + verified token payload."""

    request_id: str
    user: str
    purpose: str
    exp: int  # epoch seconds


# ──────────────────────────────────────────────────────────────────────
# Secret resolution
# ──────────────────────────────────────────────────────────────────────


def _secret(config: Config | None = None) -> bytes:
    cfg = config or load_config()
    secret = getattr(cfg, "internal_token_secret", None)
    if not secret:
        # Fallback: derive from default_user. Stable per-host but rotates
        # when Config.internal_token_secret is set explicitly. Logged once.
        secret = f"csflow:{cfg.default_user}:fallback-secret"
    return secret.encode("utf-8") if isinstance(secret, str) else secret


def ensure_secret_initialised(config: Config) -> Config:
    """Ensure the config has a strong random ``internal_token_secret``.

    Mutates a copy and returns it; callers should ``save_config(new_cfg)``.
    """
    if getattr(config, "internal_token_secret", None):
        return config
    new_cfg = config.model_copy(
        update={"internal_token_secret": secrets.token_urlsafe(48)},
    )
    return new_cfg


def ensure_api_token_initialised(config: Config) -> Config:
    """Ensure the config has a strong random ``api_token`` for the public /api
    guard (OpenClaw gateway paradigm: loopback bind + bearer token).

    Mutates a copy and returns it; callers should ``save_config(new_cfg)``.
    Idempotent: returns the same object untouched if already set.
    """
    if getattr(config, "api_token", None):
        return config
    return config.model_copy(update={"api_token": secrets.token_urlsafe(32)})


# ──────────────────────────────────────────────────────────────────────
# Encoding helpers
# ──────────────────────────────────────────────────────────────────────


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64d(data: str) -> bytes:
    pad = "=" * (-len(data) % 4)
    return base64.urlsafe_b64decode(data + pad)


def _sign(header: bytes, secret: bytes) -> str:
    sig = hmac.new(secret, header, sha256).digest()
    return _b64(sig)


# ──────────────────────────────────────────────────────────────────────
# Public surface
# ──────────────────────────────────────────────────────────────────────


def mint_token(
    *,
    request_id: str,
    user: str,
    purpose: str = _PURPOSE_AGENT_MGMT,
    ttl_seconds: int = _DEFAULT_TTL_SEC,
    config: Config | None = None,
) -> str:
    """Issue a token tying *request_id* + *user* + *purpose* together.

    The token is shaped ``<base64-header>.<base64-sig>`` — URL safe.
    """
    if ttl_seconds <= 0:
        raise ValueError("ttl_seconds must be positive")
    if not request_id or not user:
        raise ValueError("request_id and user are required")

    payload = {
        "request_id": request_id,
        "user": user,
        "purpose": purpose,
        "exp": int(time.time()) + ttl_seconds,
    }
    header = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
    signature = _sign(header, _secret(config))
    return f"{_b64(header)}.{signature}"


def verify_token(token: str, *, config: Config | None = None) -> TokenClaims:
    """Verify *token* and return its claims.

    Raises :class:`InvalidToken` on any failure.
    """
    if not token or "." not in token:
        raise InvalidToken("malformed token")
    header_b64, sig = token.split(".", 1)
    try:
        header_bytes = _b64d(header_b64)
    except Exception as exc:
        raise InvalidToken(f"header decode failed: {exc}") from exc
    expected_sig = _sign(header_bytes, _secret(config))
    if not hmac.compare_digest(expected_sig, sig):
        raise InvalidToken("bad signature")
    try:
        payload = json.loads(header_bytes.decode("utf-8"))
    except json.JSONDecodeError as exc:
        raise InvalidToken(f"header is not JSON: {exc}") from exc
    for k in ("request_id", "user", "purpose", "exp"):
        if k not in payload:
            raise InvalidToken(f"claim missing: {k}")
    if int(payload["exp"]) <= int(time.time()):
        raise InvalidToken("token expired")
    return TokenClaims(
        request_id=payload["request_id"],
        user=payload["user"],
        purpose=payload["purpose"],
        exp=int(payload["exp"]),
    )


__all__ = [
    "InvalidToken",
    "TokenClaims",
    "ensure_api_token_initialised",
    "ensure_secret_initialised",
    "mint_token",
    "verify_token",
]
