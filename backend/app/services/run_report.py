"""Shared extraction of a Run's leader work report from its event stream.

The leader's final report is not a column on ``FlowRun``; it lives inside the
``run_terminal_execution_log`` RunEvent's ``worker_report_history`` (the entry
whose summary begins with the ``leader final reply:`` marker). This module is
the single backend implementation of that extraction, mirroring the WebUI's
``extractLeaderReply`` (frontend ``RunDetail.tsx``).

Consumers: the run-notify webhook payload enricher and the ``/runs/{id}/result``
API endpoint (→ MCP / CLI result queries). Keep it dependency-light so any
layer can import it.
"""

from __future__ import annotations

from typing import Any

_LEADER_REPLY_MARKER = "leader final reply:"


def extract_leader_report(events: list[Any]) -> str | None:
    """Leader's final report text from the ``run_terminal_execution_log`` event.

    Scans newest event first; within it, the latest report whose summary starts
    with the ``leader final reply:`` marker wins, else the last non-empty
    summary. Best-effort — returns None on any shape mismatch or if no terminal
    execution log has been emitted yet (i.e. the run is not finished).

    ``events`` items are duck-typed (``.type`` / ``.payload``) so both ORM
    ``RunEvent`` rows and plain snapshots work.
    """
    for ev in reversed(events):
        if getattr(ev, "type", None) != "run_terminal_execution_log":
            continue
        history = (getattr(ev, "payload", None) or {}).get("worker_report_history")
        if not isinstance(history, list):
            continue
        fallback: str | None = None
        for item in reversed(history):
            if not isinstance(item, dict):
                continue
            raw = str(item.get("summary") or "").strip()
            if not raw:
                continue
            if raw.lower().startswith(_LEADER_REPLY_MARKER):
                stripped = raw[len(_LEADER_REPLY_MARKER):].strip()
                return stripped or raw
            if fallback is None:
                fallback = raw
        if fallback:
            return fallback
    return None


__all__ = ["extract_leader_report"]
