"""Tracked, killable single-agent chat turns with live step-level progress.

``hermes -p <id> --yolo [-c] -z <msg>`` (one-shot) prints ONLY the final answer
and — because ``hermes -z`` calls ``logging.disable(logging.CRITICAL)`` and
redirects stdout/stderr to devnull for the whole run — writes nothing to
``agent.log``. Intermediate state (tool calls, model turns) lands only in the
per-profile **session store**, which we read through the supported CLI
``hermes -p <id> sessions export --session-id <sid> -`` (one JSON object with a
``messages[]`` array plus ``tool_call_count`` / ``api_call_count`` / ``ended_at``
counters). ``--source cli`` isolates these chat sessions from gateway
(telegram/cron) ones.

This module wraps a turn in a :class:`ChatJob`:

* The ``hermes`` process is a tracked process group (``start_new_session=True``)
  registered in :mod:`app.services.subprocess_registry`, so it can be **killed**
  on reset / supersede (no runaway process, no ghost reply) and is cleaned up by
  the FastAPI lifespan shutdown.
* A poller thread discovers the session id and emits :class:`StepEvent`s while
  the turn runs.
* Job state (steps + status + final) is held in an in-memory registry keyed by
  ``session_key``, so the WebUI can **reconnect** after a tab switch / refresh
  (see ``GET /hermes/agents/{id}/chat/status``) instead of a bounded poll.

Only one turn per ``session_key`` runs at a time — :func:`start_chat` supersedes
(kills) any in-flight job for the same key first. In local single-user mode this
makes "the most-recently-active ``cli`` session" unambiguously *our* turn, which
is how the poller identifies the session id.
"""

from __future__ import annotations

import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, TypedDict

from app.logging_setup import get_logger
from app.services import hermes_agents as ha
from app.services import subprocess_registry as _subproc_registry

logger = get_logger("services.hermes_chat")

# Hard ceiling on a single turn; matches the historical chat timeout. The poll
# thread kills the process group if a turn exceeds this.
_CHAT_TIMEOUT_SEC = 1800.0
# Cadence of the session-export progress poll. hermes has no live ``--follow``
# stream for one-shot turns (``hermes -z`` suppresses logging and ``sessions``
# has no tail), so we poll ``sessions export`` for near-real-time progress. Each
# export spawns a short hermes subprocess; 1.5s is snappy yet fine for the single
# active turn at a time (kept tighter than a true push stream's 0s, looser than a
# busy-loop). Bump up if CPU from repeated CLI spawns becomes a concern.
_POLL_INTERVAL_SEC = 1.5
# Bound the retained step list so a pathological turn can't grow it unbounded.
_MAX_STEPS = 400


class StepEvent(TypedDict, total=False):
    """One progress entry surfaced to the UI (formatted client-side via i18n)."""

    kind: str  # "tool" | "info"
    name: str  # tool name (kind == "tool")
    seq: int  # monotonic index for client-side de-dup


@dataclass
class _Progress:
    tool_calls: int = 0
    api_calls: int = 0
    message_count: int = 0


@dataclass
class ChatJob:
    """Live state for one chat turn. Thread-safe via ``_lock``."""

    agent_id: str
    session_key: str
    proc: subprocess.Popen
    started_at: float
    status: str = "running"  # running | done | error
    hermes_session_id: str | None = None
    steps: list[StepEvent] = field(default_factory=list)
    progress: _Progress = field(default_factory=_Progress)
    final_text: str = ""
    error: str = ""
    _seq: int = 0
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def _append_step(self, kind: str, *, name: str = "") -> None:
        with self._lock:
            self._seq += 1
            step: StepEvent = {"kind": kind, "seq": self._seq}
            if name:
                step["name"] = name
            self.steps.append(step)
            if len(self.steps) > _MAX_STEPS:
                del self.steps[: len(self.steps) - _MAX_STEPS]

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable view for the SSE stream / status endpoint."""
        with self._lock:
            return {
                "status": self.status,
                "steps": [dict(s) for s in self.steps],
                "progress": {
                    "toolCalls": self.progress.tool_calls,
                    "apiCalls": self.progress.api_calls,
                    "messageCount": self.progress.message_count,
                    "elapsedSec": round(time.monotonic() - self.started_at, 1),
                },
                "final": self.final_text,
                "error": self.error,
                "startedAtMono": self.started_at,
            }


# ── Registry ──────────────────────────────────────────────────────────
_JOBS: dict[str, ChatJob] = {}
_REG_LOCK = threading.Lock()


def get_job(session_key: str) -> ChatJob | None:
    with _REG_LOCK:
        return _JOBS.get(session_key)


def kill_chat(session_key: str) -> bool:
    """Kill and forget any in-flight job for *session_key*. Idempotent.

    Returns whether a live process was signalled. Used by reset and by
    :func:`start_chat`'s supersede path so a reset never leaves a runaway
    ``hermes`` process that would later append a ghost reply.
    """
    with _REG_LOCK:
        job = _JOBS.pop(session_key, None)
    if job is None:
        return False
    signalled = False
    try:
        if job.proc.poll() is None:
            signalled = _subproc_registry.kill_group(job.proc)
    finally:
        _subproc_registry.unregister(job.proc)
        with job._lock:
            if job.status == "running":
                job.status = "error"
                if not job.error:
                    job.error = "cancelled"
    if signalled:
        logger.info("hermes_chat_killed", session_key=session_key, agent_id=job.agent_id)
    return signalled


def start_chat(
    agent_id: str,
    *,
    message: str,
    workdir: str,
    resume: bool,
    session_key: str,
) -> ChatJob:
    """Spawn a tracked chat turn and start its progress poller.

    Mirrors the historical ``chat_once`` invocation (``hermes -p <id> --yolo
    [-c] -z <msg>``) but as a killable, observable :class:`ChatJob`.
    """
    aid = ha._validate_agent_id(agent_id)
    wd = ha._existing_directory(workdir, field_name="workdir")
    exe = ha.hermes_executable()
    if exe is None:
        raise ha.HermesUnavailable("`hermes` CLI not found on PATH")

    # Supersede any in-flight turn for this conversation before starting a new
    # one (prevents two hermes processes writing the same profile session).
    kill_chat(session_key)

    argv = [exe, "-p", aid, "--yolo", *(["-c"] if resume else [])]
    argv.extend(["-z", message])
    proc = subprocess.Popen(  # noqa: S603 — args are constructed, not shell
        argv,
        cwd=str(wd),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
        start_new_session=True,  # own process group → killable as a unit
    )
    _subproc_registry.register(proc)
    job = ChatJob(
        agent_id=aid, session_key=session_key, proc=proc, started_at=time.monotonic()
    )
    with _REG_LOCK:
        _JOBS[session_key] = job

    # Drain stdout/stderr in a dedicated thread so a large final answer can't
    # fill the pipe and deadlock the process before it exits.
    io_done = threading.Event()
    io_result: dict[str, Any] = {}

    def _io() -> None:
        try:
            out, err = proc.communicate()
            io_result["out"] = out or ""
            io_result["err"] = err or ""
            io_result["rc"] = proc.returncode
        except Exception as exc:  # pragma: no cover - defensive
            io_result["err"] = str(exc)
            io_result["rc"] = -1
        finally:
            io_done.set()

    threading.Thread(target=_io, name="hermes-chat-io", daemon=True).start()
    threading.Thread(
        target=_run_poller,
        args=(job, message, io_done, io_result),
        name="hermes-chat-poll",
        daemon=True,
    ).start()
    logger.info("hermes_chat_started", session_key=session_key, agent_id=aid, resume=resume)
    return job


# ── Poller ────────────────────────────────────────────────────────────


def _run_poller(
    job: ChatJob, message: str, io_done: threading.Event, io_result: dict[str, Any]
) -> None:
    seen_tools = 0  # number of tool calls already surfaced as steps
    locked_sid = False
    while not io_done.wait(timeout=_POLL_INTERVAL_SEC):
        # Watchdog: kill a turn that overruns the hard ceiling.
        if time.monotonic() - job.started_at > _CHAT_TIMEOUT_SEC:
            logger.warning("hermes_chat_timeout", session_key=job.session_key)
            kill_chat(job.session_key)
            io_done.wait(timeout=5.0)
            break
        if job.hermes_session_id is None:
            sid = _discover_session_id(job.agent_id)
            if sid:
                job.hermes_session_id = sid
        if job.hermes_session_id:
            data = _export_session(job.agent_id, job.hermes_session_id)
            if data is not None:
                locked_sid = True
                seen_tools = _apply_progress(job, data, seen_tools)
            elif not locked_sid:
                # The session we picked may have been wrong (a stale top row);
                # allow re-discovery on the next tick until an export succeeds.
                job.hermes_session_id = None

    # Process has exited (or was killed). Finalise.
    rc = io_result.get("rc", -1)
    out = io_result.get("out", "")
    err = io_result.get("err", "")
    # One last export so the final tool/turn lands in the step trail.
    if job.hermes_session_id:
        data = _export_session(job.agent_id, job.hermes_session_id)
        if data is not None:
            _apply_progress(job, data, _count_tool_calls(data))
    _subproc_registry.unregister(job.proc)

    with job._lock:
        if job.status == "running":
            if rc == 0 and (out or "").strip():
                job.status = "done"
                job.final_text = out.strip()
            else:
                job.status = "error"
                detail = (ha._strip_ansi(err) or ha._strip_ansi(out)).strip()
                job.error = detail[:1000] or f"hermes exited with code {rc}"
    logger.info(
        "hermes_chat_finished",
        session_key=job.session_key,
        status=job.status,
        tool_calls=job.progress.tool_calls,
    )


def _discover_session_id(agent_id: str) -> str | None:
    """Newest ``cli`` session for this profile = our active turn.

    Safe because :func:`start_chat` supersedes concurrent turns and local mode
    is single-user, so only our process writes ``cli`` sessions right now.
    """
    try:
        rc, out, _err = ha._run_hermes(
            ["-p", agent_id, "sessions", "list", "--source", "cli", "--limit", "5"]
        )
    except ha.HermesAgentError:
        return None
    if rc != 0:
        return None
    for raw in ha._strip_ansi(out).splitlines():
        line = raw.strip()
        if not line:
            continue
        low = line.lower()
        if low.startswith("preview") or set(line) <= set("─-—│| "):
            continue  # header / separator
        token = line.split()[-1]  # ID is the last column
        if "_" in token:  # e.g. 20260617_185542_a98875
            return token
    return None


def _export_session(agent_id: str, session_id: str) -> dict[str, Any] | None:
    try:
        rc, out, _err = ha._run_hermes(
            ["-p", agent_id, "sessions", "export", "--session-id", session_id, "-"]
        )
    except ha.HermesAgentError:
        return None
    if rc != 0 or not out.strip():
        return None
    import json

    try:
        obj = json.loads(out.strip())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_names(data: dict[str, Any]) -> list[str]:
    """Ordered tool-call names from the exported ``messages[]``."""
    names: list[str] = []
    for msg in data.get("messages") or []:
        if not isinstance(msg, dict):
            continue
        for tc in msg.get("tool_calls") or []:
            if isinstance(tc, dict):
                fn = tc.get("function") or {}
                name = (fn.get("name") if isinstance(fn, dict) else None) or tc.get("name")
                if name:
                    names.append(str(name))
    return names


def _count_tool_calls(data: dict[str, Any]) -> int:
    n = data.get("tool_call_count")
    if isinstance(n, int):
        return n
    return len(_tool_names(data))


def _apply_progress(job: ChatJob, data: dict[str, Any], seen_tools: int) -> int:
    """Update counters and append a step per newly-seen tool call. Returns the
    new ``seen_tools`` watermark."""
    names = _tool_names(data)
    for name in names[seen_tools:]:
        job._append_step("tool", name=name)
    with job._lock:
        job.progress.tool_calls = (
            data.get("tool_call_count")
            if isinstance(data.get("tool_call_count"), int)
            else len(names)
        )
        ac = data.get("api_call_count")
        if isinstance(ac, int):
            job.progress.api_calls = ac
        mc = data.get("message_count")
        if isinstance(mc, int):
            job.progress.message_count = mc
    return max(seen_tools, len(names))


__all__ = ["ChatJob", "StepEvent", "start_chat", "kill_chat", "get_job"]
