"""Tracked, killable Hermes chat turns with reconnectable elapsed progress.

Wraps a single ``hermes -p <id> --yolo [-c] -z <msg>`` invocation per attempt
(the same semantics as :func:`hermes_agents.chat_once`) without session-export
polling. Progress exposed to the WebUI is **elapsed time only**; the client-side
:class:`~frontend PendingReply` bubble handles the 10s+ "still thinking" hint.

Each turn is a :class:`ChatJob` in an in-memory registry keyed by
``session_key``, powering ``GET /hermes/agents/{id}/chat/status`` reconnect after
a tab switch / refresh.
"""

from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Literal

from app.logging_setup import get_logger
from app.services import hermes_agents as ha
from app.services import subprocess_registry as _subproc_registry
from app.services.chat_retry import (
    CHAT_CONNECTION_RETRY_ATTEMPTS,
    CHAT_CONNECTION_RETRY_DELAYS_SEC,
    is_transient_connection_error,
)

logger = get_logger("services.hermes_chat")

# Hard ceiling on ONE chat turn — i.e. how long we wait for the agent to produce
# a single reply before killing the hermes subprocess. Long-running, tool-heavy
# turns can take hours, so this is generous (8h).
_CHAT_TIMEOUT_SEC = 28800.0

FinalSource = Literal["stdout", "session_export", "none", "none_after_tools", ""]

_NO_TEXT_REPLY_MARKER = "[[NO_TEXT_REPLY]]"


@dataclass
class ChatJob:
    """Live state for one chat turn. Thread-safe via ``_lock``."""

    agent_id: str
    session_key: str
    started_at: float
    status: str = "running"  # running | done | error
    final_text: str = ""
    error: str = ""
    hermes_session_id: str | None = None
    proc: subprocess.Popen[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable view for the SSE stream / status endpoint."""
        with self._lock:
            return {
                "status": self.status,
                "steps": [],
                "progress": {
                    "toolCalls": 0,
                    "apiCalls": 0,
                    "messageCount": 0,
                    "elapsedSec": round(time.monotonic() - self.started_at, 1),
                },
                "final": self.final_text,
                "error": self.error,
                "startedAtMono": self.started_at,
            }


_JOBS: dict[str, ChatJob] = {}
_REG_LOCK = threading.Lock()


def get_job(session_key: str) -> ChatJob | None:
    with _REG_LOCK:
        return _JOBS.get(session_key)


def kill_chat(session_key: str) -> bool:
    """Kill and forget any in-flight job for *session_key*. Idempotent."""
    with _REG_LOCK:
        job = _JOBS.pop(session_key, None)
    if job is None:
        return False
    signalled = False
    try:
        if job.proc is not None and job.proc.poll() is None:
            signalled = _subproc_registry.kill_group(job.proc)
    finally:
        if job.proc is not None:
            _subproc_registry.unregister(job.proc)
        with job._lock:
            if job.status == "running":
                job.status = "error"
                if not job.error:
                    job.error = "cancelled"
    if signalled:
        logger.info(
            "hermes_chat_killed",
            session_key=session_key,
            agent_id=job.agent_id,
        )
    return signalled


def start_chat(
    agent_id: str,
    *,
    message: str,
    workdir: str,
    resume: bool,
    session_key: str,
) -> ChatJob:
    """Spawn a tracked chat turn (``chat_once`` semantics, no session polling)."""
    aid = ha._validate_agent_id(agent_id)
    # Normalise to an ABSOLUTE path before it reaches subprocess.Popen(cwd=...).
    # A raw tilde path like "~" is NOT expanded by Popen and makes it raise
    # FileNotFoundError; because the spawn now runs inside the background
    # _run_turn thread, that exception would be swallowed and wedge the job in
    # "running" forever (regression vs 0.1.24, which spawned synchronously with
    # the expanded path). ``_existing_directory`` already expands + validates —
    # we just have to keep its result instead of discarding it.
    workdir = str(ha._existing_directory(workdir, field_name="workdir"))
    if ha.hermes_executable() is None:
        raise ha.HermesUnavailable("`hermes` CLI not found on PATH")

    kill_chat(session_key)

    job = ChatJob(agent_id=aid, session_key=session_key, started_at=time.monotonic())
    with _REG_LOCK:
        _JOBS[session_key] = job

    threading.Thread(
        target=_run_turn,
        args=(job, message, workdir, resume),
        name="hermes-chat-turn",
        daemon=True,
    ).start()
    logger.info(
        "hermes_chat_started",
        session_key=session_key,
        agent_id=aid,
        resume=resume,
        workdir=workdir,
        message_chars=len(message),
    )
    return job


def _spawn_hermes(
    agent_id: str,
    *,
    message: str,
    workdir: str,
    resume: bool,
) -> subprocess.Popen[str]:
    exe = ha.hermes_executable()
    assert exe is not None
    argv = [exe, "-p", agent_id, "--yolo", *(["-c"] if resume else []), "-z", message]
    return subprocess.Popen(  # noqa: S603 — args are constructed, not shell
        argv,
        cwd=str(workdir),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
        start_new_session=True,
    )


def _communicate(proc: subprocess.Popen[str]) -> tuple[int, str, str]:
    try:
        out, err = proc.communicate(timeout=_CHAT_TIMEOUT_SEC)
        return proc.returncode or 0, out or "", err or ""
    except subprocess.TimeoutExpired:
        _subproc_registry.kill_group(proc)
        proc.communicate()
        return -1, "", f"hermes chat timed out after {int(_CHAT_TIMEOUT_SEC)}s"


def _finish_job(
    job: ChatJob,
    *,
    status: str,
    final_text: str = "",
    error: str = "",
) -> None:
    with job._lock:
        if job.status == "running":
            job.status = status
            job.final_text = final_text
            job.error = error


def _preview_text(text: str, *, limit: int = 240) -> str:
    t = ha._strip_ansi(text or "").strip()
    return t if len(t) <= limit else t[:limit] + "…"


def _discover_session_id(agent_id: str) -> str | None:
    """Newest ``cli`` session for this profile = our just-finished turn.

    Safe because :func:`start_chat` supersedes concurrent turns and local mode is
    single-user, so only our process writes ``cli`` sessions.
    """
    try:
        rc, out, err = ha._run_hermes(
            ["-p", agent_id, "sessions", "list", "--source", "cli", "--limit", "5"]
        )
    except ha.HermesAgentError as exc:
        logger.warning("hermes_chat_session_list_failed", agent_id=agent_id, error=str(exc)[:240])
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
        rc, out, err = ha._run_hermes(
            ["-p", agent_id, "sessions", "export", "--session-id", session_id, "-"]
        )
    except ha.HermesAgentError as exc:
        logger.warning(
            "hermes_chat_session_export_error",
            agent_id=agent_id,
            hermes_session_id=session_id,
            error=str(exc)[:240],
        )
        return None
    if rc != 0 or not out.strip():
        return None
    try:
        obj = json.loads(out.strip())
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _tool_names(data: dict[str, Any]) -> list[str]:
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
    return n if isinstance(n, int) else len(_tool_names(data))


def _extract_assistant_text(export_data: dict[str, Any] | None) -> str:
    """Last non-empty assistant text from a ``sessions export`` payload."""
    if not export_data:
        return ""
    for msg in reversed(export_data.get("messages") or []):
        if not isinstance(msg, dict) or (msg.get("role") or "").lower() != "assistant":
            continue
        content = msg.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for block in content:
                if isinstance(block, dict):
                    t = block.get("text") or block.get("content")
                    if isinstance(t, str) and t.strip():
                        parts.append(t.strip())
            if parts:
                return "\n".join(parts)
    return ""


def _recover_empty_stdout(job: ChatJob) -> tuple[str, FinalSource]:
    """``rc==0`` but empty stdout: hermes printed nothing to the pipe (common for
    ``-c`` resume / tool-heavy turns where the answer lives only in the session
    store). Do a ONE-SHOT (no polling) ``sessions export`` to recover the final
    assistant text. Returns ``(final_text, source)``; ``source==""`` when nothing
    usable was found (caller then falls through to the error/retry path)."""
    sid = _discover_session_id(job.agent_id)
    if not sid:
        return "", ""
    job.hermes_session_id = sid
    data = _export_session(job.agent_id, sid)
    if data is None:
        return "", ""
    text = _extract_assistant_text(data)
    if text:
        return text, "session_export"
    if _count_tool_calls(data) > 0:
        # The agent worked (tool calls) but emitted no text — a legitimate
        # "no visible reply", NOT a failure. Marker is rendered specially by the UI.
        return _NO_TEXT_REPLY_MARKER, "none_after_tools"
    return "", ""


def _resolve_done(job: ChatJob, rc: int, out: str) -> tuple[str, FinalSource] | None:
    """Decide whether this hermes exit is a successful turn. Returns
    ``(final_text, source)`` on success, else ``None`` (error/retry path)."""
    if rc != 0:
        return None
    stdout_text = (out or "").strip()
    if stdout_text:
        return stdout_text, "stdout"
    text, source = _recover_empty_stdout(job)
    return (text, source) if source else None


def _run_turn(job: ChatJob, message: str, workdir: str, resume: bool) -> None:
    """Run one turn, guaranteeing the job never wedges in ``running``.

    The actual work lives in :func:`_run_turn_impl`; this wrapper guarantees
    that *any* unexpected exception (e.g. ``subprocess.Popen`` raising
    ``FileNotFoundError`` on a bad cwd) transitions the job to ``error`` instead
    of silently killing the thread and leaving the WebUI spinning forever.
    """
    try:
        _run_turn_impl(job, message, workdir, resume)
    except Exception as exc:  # noqa: BLE001 — last-resort guard for the thread
        if get_job(job.session_key) is not job:
            return
        _finish_job(
            job,
            status="error",
            error=f"hermes chat failed to start: {exc}"[:1000],
        )
        logger.warning(
            "hermes_chat_finished",
            session_key=job.session_key,
            agent_id=job.agent_id,
            status="error",
            error=str(exc)[:240],
        )


def _run_turn_impl(job: ChatJob, message: str, workdir: str, resume: bool) -> None:
    """Execute one chat turn with connection-error retries."""
    use_resume = resume
    last_detail = ""

    for attempt in range(CHAT_CONNECTION_RETRY_ATTEMPTS):
        if get_job(job.session_key) is not job:
            return
        with job._lock:
            if job.status != "running":
                return

        proc = _spawn_hermes(
            job.agent_id, message=message, workdir=workdir, resume=use_resume
        )
        job.proc = proc
        _subproc_registry.register(proc)
        rc, out, err = _communicate(proc)
        _subproc_registry.unregister(proc)
        job.proc = None

        if get_job(job.session_key) is not job:
            return

        done = _resolve_done(job, rc, out)
        if done is not None:
            final_text, source = done
            _finish_job(job, status="done", final_text=final_text)
            logger.info(
                "hermes_chat_finished",
                session_key=job.session_key,
                agent_id=job.agent_id,
                status="done",
                final_source=source,
                final_len=len(final_text),
                attempt=attempt + 1,
            )
            return

        detail = (ha._strip_ansi(err) or ha._strip_ansi(out)).strip()
        last_detail = detail

        if use_resume and rc != 0:
            logger.warning(
                "hermes_chat_resume_failed_fallback_fresh",
                session_key=job.session_key,
                agent_id=job.agent_id,
                error=detail[:500],
            )
            use_resume = False
            proc2 = _spawn_hermes(
                job.agent_id, message=message, workdir=workdir, resume=False
            )
            job.proc = proc2
            _subproc_registry.register(proc2)
            rc, out, err = _communicate(proc2)
            _subproc_registry.unregister(proc2)
            job.proc = None
            done = _resolve_done(job, rc, out)
            if done is not None:
                final_text, source = done
                _finish_job(job, status="done", final_text=final_text)
                logger.info(
                    "hermes_chat_finished",
                    session_key=job.session_key,
                    agent_id=job.agent_id,
                    status="done",
                    final_source=source,
                    final_len=len(final_text),
                    attempt=attempt + 1,
                    resume_fallback=True,
                )
                return
            detail = (ha._strip_ansi(err) or ha._strip_ansi(out)).strip()
            last_detail = detail

        if (
            attempt + 1 < CHAT_CONNECTION_RETRY_ATTEMPTS
            and is_transient_connection_error(detail)
        ):
            delay = CHAT_CONNECTION_RETRY_DELAYS_SEC[
                min(attempt, len(CHAT_CONNECTION_RETRY_DELAYS_SEC) - 1)
            ]
            logger.warning(
                "hermes_chat_connection_retry",
                session_key=job.session_key,
                agent_id=job.agent_id,
                attempt=attempt + 1,
                delay_sec=delay,
                error_preview=detail[:240],
            )
            time.sleep(delay)
            continue
        break

    err_msg = (last_detail or "hermes produced no reply")[:1000]
    _finish_job(job, status="error", error=err_msg)
    logger.warning(
        "hermes_chat_finished",
        session_key=job.session_key,
        agent_id=job.agent_id,
        status="error",
        error=err_msg[:240],
    )


__all__ = [
    "ChatJob",
    "start_chat",
    "kill_chat",
    "get_job",
    "_NO_TEXT_REPLY_MARKER",
]
