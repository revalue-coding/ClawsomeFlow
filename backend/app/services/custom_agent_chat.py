"""Tracked, killable custom-agent chat turns (simple one-shot subprocess).

The custom-agent chat dialog is deliberately minimal: one turn = one headless
subprocess built from the registry's ``headless_command`` template (or
``headless_resume_command`` from the second message of a session onward), with
``{message}`` substituted (appended when the placeholder is absent). stdout is
the reply; a non-zero exit code is an error. No token streaming — the SSE
endpoint mirrors the Hermes shape (``progress`` heartbeats + one final
``delta``), so the frontend loop is shared.

Fully isolated from the scheduler: no ClawTeam, no teams, no worktrees. The
subprocess runs in its own process group and is registered in
``subprocess_registry`` so stop / service shutdown can kill it.
"""

from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.logging_setup import get_logger
from app.models import CustomAgent
from app.services import custom_agents as reg
from app.services import subprocess_registry as _subproc_registry

logger = get_logger("services.custom_agent_chat")

# Hard ceiling on one chat turn (same generous tier as Hermes chat — a
# tool-heavy agentic turn can take hours).
_CHAT_TIMEOUT_SEC = 28800.0

_ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text or "")


@dataclass
class ChatJob:
    """Live state for one chat turn. Thread-safe via ``_lock``."""

    agent_id: str
    session_key: str
    started_at: float
    status: str = "running"  # running | done | error
    final_text: str = ""
    error: str = ""
    proc: subprocess.Popen[str] | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict[str, Any]:
        """JSON-serialisable view (same shape as the Hermes chat job)."""
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
            "custom_agent_chat_killed",
            session_key=session_key,
            agent_id=job.agent_id,
        )
    return signalled


def resolve_chat_workdir(agent: CustomAgent, override: str = "") -> str:
    """Absolute existing cwd for the chat subprocess (default: user home)."""
    raw = (override or "").strip() or (agent.chat_workdir or "").strip() or "~"
    path = Path(raw).expanduser()
    if not path.is_dir():
        raise reg.CustomAgentError(
            "INVALID_PAYLOAD", f"workdir {raw!r} is not an existing directory"
        )
    return str(path.resolve())


def start_chat(
    agent: CustomAgent,
    *,
    message: str,
    workdir: str,
    resume: bool,
    session_key: str,
) -> ChatJob:
    """Spawn a tracked one-shot chat turn for a registered custom agent."""
    if not agent.headless_command:
        raise reg.CustomAgentError(
            "CUSTOM_AGENT_CHAT_UNCONFIGURED",
            "this custom agent has no headless chat command configured",
            status_code=409,
        )
    template = (
        list(agent.headless_resume_command)
        if (resume and agent.headless_resume_command)
        else list(agent.headless_command)
    )
    # No PATH probe: an unstartable command surfaces as the subprocess' own
    # error on this turn ("chat failed to start: ..."), which is both more
    # accurate and the user's own responsibility.
    argv = reg.render_headless_argv(template, message)

    kill_chat(session_key)

    job = ChatJob(
        agent_id=agent.id, session_key=session_key, started_at=time.monotonic()
    )
    with _REG_LOCK:
        _JOBS[session_key] = job

    threading.Thread(
        target=_run_turn,
        args=(job, argv, workdir),
        name="custom-agent-chat-turn",
        daemon=True,
    ).start()
    logger.info(
        "custom_agent_chat_started",
        session_key=session_key,
        agent_id=agent.id,
        resume=resume,
        workdir=workdir,
        message_chars=len(message),
    )
    return job


def _run_turn(job: ChatJob, argv: list[str], workdir: str) -> None:
    """Run one turn, guaranteeing the job never wedges in ``running``."""
    try:
        _run_turn_impl(job, argv, workdir)
    except Exception as exc:  # noqa: BLE001 — last-resort guard for the thread
        if get_job(job.session_key) is not job:
            return
        _finish_job(
            job,
            status="error",
            error=f"custom agent chat failed to start: {exc}"[:1000],
        )
        logger.warning(
            "custom_agent_chat_finished",
            session_key=job.session_key,
            agent_id=job.agent_id,
            status="error",
            error=str(exc)[:240],
        )


def _run_turn_impl(job: ChatJob, argv: list[str], workdir: str) -> None:
    proc = subprocess.Popen(  # noqa: S603 — argv from the user's own registry row
        argv,
        cwd=workdir,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=os.environ.copy(),
        start_new_session=True,
    )
    job.proc = proc
    _subproc_registry.register(proc)
    try:
        out, err = proc.communicate(timeout=_CHAT_TIMEOUT_SEC)
        rc = proc.returncode or 0
    except subprocess.TimeoutExpired:
        _subproc_registry.kill_group(proc)
        proc.communicate()
        rc, out, err = -1, "", f"chat timed out after {int(_CHAT_TIMEOUT_SEC)}s"
    finally:
        _subproc_registry.unregister(proc)
        job.proc = None

    if get_job(job.session_key) is not job:
        return  # superseded / killed

    reply = _strip_ansi(out or "").strip()
    if rc == 0 and reply:
        _finish_job(job, status="done", final_text=reply)
        logger.info(
            "custom_agent_chat_finished",
            session_key=job.session_key,
            agent_id=job.agent_id,
            status="done",
            final_len=len(reply),
        )
        return
    detail = (_strip_ansi(err or "") or reply or "").strip()
    if rc == 0:
        detail = detail or "agent produced no reply on stdout"
    err_msg = (detail or f"agent exited with code {rc}")[:1000]
    _finish_job(job, status="error", error=err_msg)
    logger.warning(
        "custom_agent_chat_finished",
        session_key=job.session_key,
        agent_id=job.agent_id,
        status="error",
        exit_code=rc,
        error=err_msg[:240],
    )


def _finish_job(
    job: ChatJob, *, status: str, final_text: str = "", error: str = ""
) -> None:
    with job._lock:
        if job.status == "running":
            job.status = status
            job.final_text = final_text
            job.error = error


__all__ = [
    "ChatJob",
    "get_job",
    "kill_chat",
    "resolve_chat_workdir",
    "start_chat",
]
