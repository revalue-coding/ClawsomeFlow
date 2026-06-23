"""Shared retry helpers for direct-agent chat turns (Hermes / OpenClaw)."""

from __future__ import annotations

# Transient network/DNS failures worth retrying a whole chat turn.
_TRANSIENT_CONNECTION_MARKERS = (
    "name or service not known",
    "temporary failure in name resolution",
    "nodename nor servname",
    "failed to resolve",
    "connection refused",
    "connection reset",
    "connection aborted",
    "network is unreachable",
    "no route to host",
    "timed out",
    "timeout",
    "errno -2",
    "errno -3",
    "errno -5",
    "getaddrinfo failed",
)

CHAT_CONNECTION_RETRY_ATTEMPTS = 3
CHAT_CONNECTION_RETRY_DELAYS_SEC = (1.0, 2.0, 4.0)


def is_transient_connection_error(detail: str) -> bool:
    """Return whether *detail* looks like a retryable DNS/connectivity failure."""
    text = (detail or "").lower()
    return any(marker in text for marker in _TRANSIENT_CONNECTION_MARKERS)


__all__ = [
    "CHAT_CONNECTION_RETRY_ATTEMPTS",
    "CHAT_CONNECTION_RETRY_DELAYS_SEC",
    "is_transient_connection_error",
]
