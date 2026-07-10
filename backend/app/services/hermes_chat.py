"""Tracked, killable Hermes chat turns with reconnectable elapsed progress.

Wraps a single ``hermes -p <id> chat --yolo -Q [--resume <sid>|-c] -q <msg>``
invocation per attempt. Progress exposed to the WebUI is **elapsed time only**;
the client-side :class:`~frontend PendingReply` bubble handles the 10s+
"still thinking" hint.

The authoritative final reply comes from a one-shot ``sessions export`` (scoped
to the current turn after the last user message). ``chat -Q`` stdout is only a
fallback — it often contains truncated tool previews (e.g. ``┊ review diff``)
rather than the agent's real answer.

Each turn is a :class:`ChatJob` in an in-memory registry keyed by
``session_key``, powering ``GET /hermes/agents/{id}/chat/status`` reconnect after
a tab switch / refresh.
"""

from __future__ import annotations

import json
import os
import re
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
_CHAT_SESSION_SOURCE = "csflow-web"
_SESSION_ID_RE = re.compile(r"(?im)^\s*session_id:\s*(?P<sid>\S+)\s*$")

# Lines that are ``chat -Q`` tool-preview / diff decoration rather than the
# agent's real answer. Used only by the stdout *salvage* path (see
# ``_salvage_stdout_reply``); kept deliberately narrow so genuine markdown
# (``## heading``, ``- bullet``) is never mistaken for noise.
_TOOL_PREVIEW_LINE_RE = re.compile(
    r"^\s*(?:"
    r"[┊│├└┌┐┘┏┓┗┛━┃╭╮╰╯╱╲▎▏]"  # box-drawing decoration
    r"|@@ .*@@"  # diff hunk header
    r"|(?:---|\+\+\+) [ab]/"  # diff file markers
    r"|[ab]/\S+\s*→\s*[ab]/\S+"  # diff path-change line
    r")"
)
# Minimum dense (whitespace-stripped) length for a salvaged stdout reply to be
# treated as a real answer rather than a couple of stray preview residues.
_MIN_SALVAGED_STDOUT_CHARS = 40


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
    resume_session_id: str | None = None,
) -> ChatJob:
    """Spawn a tracked chat turn."""
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
        args=(job, message, workdir, resume, resume_session_id),
        name="hermes-chat-turn",
        daemon=True,
    ).start()
    logger.info(
        "hermes_chat_started",
        session_key=session_key,
        agent_id=aid,
        resume=resume,
        resume_session_id=resume_session_id,
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
    resume_session_id: str | None = None,
) -> subprocess.Popen[str]:
    exe = ha.hermes_executable()
    assert exe is not None
    # Prefer an explicit Hermes session id when we have one; otherwise fall back
    # to ``-c`` (continue most-recent CLI session) for legacy chats that predate
    # persisted session bindings.
    resume_args: list[str] = []
    if resume_session_id:
        resume_args = ["--resume", resume_session_id]
    elif resume:
        resume_args = ["-c"]
    argv = [
        exe,
        "-p",
        agent_id,
        "chat",
        "--yolo",
        "-Q",
        *resume_args,
        "--source",
        _CHAT_SESSION_SOURCE,
        "-q",
        message,
    ]
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


def _parse_session_id_from_stderr(err: str) -> str | None:
    """Extract the machine-readable session id emitted by ``hermes chat -Q``."""
    match = _SESSION_ID_RE.search(ha._strip_ansi(err or ""))
    return match.group("sid") if match else None


def _ensure_job_session_id(job: ChatJob) -> None:
    """Bind ``job.hermes_session_id`` after a successful turn when missing.

    ``hermes chat -Q`` normally emits ``session_id: ...`` on stderr. The
    one-shot ``sessions list`` probe is retained as a compatibility fallback for
    older or unexpected Hermes output.
    """
    if job.hermes_session_id:
        return
    sid = _discover_session_id(job.agent_id)
    if sid:
        job.hermes_session_id = sid


def _discover_session_id(agent_id: str) -> str | None:
    """Newest session for this profile = our just-finished turn.

    Safe because :func:`start_chat` supersedes concurrent turns and local mode is
    single-user, so only our process writes WebUI sessions. Query the new
    ClawsomeFlow source first, then fall back to historical ``cli`` sessions for
    upgrade compatibility.
    """
    for source in (_CHAT_SESSION_SOURCE, "cli"):
        try:
            rc, out, err = ha._run_hermes(
                ["-p", agent_id, "sessions", "list", "--source", source, "--limit", "5"]
            )
        except ha.HermesAgentError as exc:
            logger.warning(
                "hermes_chat_session_list_failed",
                agent_id=agent_id,
                source=source,
                error=str(exc)[:240],
            )
            continue
        if rc != 0:
            continue
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


def _message_text(content: Any) -> str:
    """Flatten a Hermes message ``content`` field to plain text."""
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict):
                t = block.get("text") or block.get("content")
                if isinstance(t, str) and t.strip():
                    parts.append(t.strip())
        return "\n".join(parts)
    return ""


def _extract_assistant_text(export_data: dict[str, Any] | None) -> str:
    """Last non-empty assistant text from the **current turn** of a session export.

    Scoped to messages after the last ``user`` row so a tool-only follow-up turn
    does not accidentally reuse a previous turn's reply (multi-turn resume).
    """
    if not export_data:
        return ""
    messages = export_data.get("messages") or []
    last_user = -1
    for i, msg in enumerate(messages):
        if isinstance(msg, dict) and (msg.get("role") or "").lower() == "user":
            last_user = i
    turn = messages[last_user + 1 :] if last_user >= 0 else messages
    for msg in reversed(turn):
        if not isinstance(msg, dict) or (msg.get("role") or "").lower() != "assistant":
            continue
        text = _message_text(msg.get("content"))
        if text:
            return text
    return ""


def _recover_from_session(
    job: ChatJob,
    *,
    preferred_session_id: str | None = None,
) -> tuple[str, FinalSource]:
    """ONE-SHOT ``sessions export`` for the authoritative final assistant text.

    Hermes ``chat -Q`` often dumps tool previews (e.g. ``┊ review diff``) — and
    sometimes truncates them mid-stream — onto stdout, while the real reply
    lives only in the session store. Prefer this over raw stdout whenever the
    session is identifiable. Returns ``(final_text, source)``; ``source==""``
    when nothing usable was found.
    """
    sid = preferred_session_id or _discover_session_id(job.agent_id)
    if not sid:
        return "", ""
    job.hermes_session_id = sid
    data = _export_session(job.agent_id, sid)
    if data is None:
        return "", ""
    text = _extract_assistant_text(data)
    if text:
        return text, "session_export"
    # No assistant text for the CURRENT turn. Decide whether this was a genuine
    # tool-only turn (→ NO_TEXT marker) or an empty turn (→ let the caller fall
    # back to stdout). Tool detection MUST be scoped to the current turn (after
    # the last user message): the session-level ``tool_call_count`` sums every
    # earlier turn, so on a long conversation it would mask a real reply that
    # is only present on stdout (market-strategist regression — a turn that
    # generated 2842 chars but was never persisted got mislabelled
    # "none_after_tools" purely because prior turns had used tools). Only trust
    # the session-level counter when the export carried no per-message data at
    # all (nothing else to go on).
    messages = data.get("messages") or []
    if messages:
        last_user = -1
        for i, msg in enumerate(messages):
            if isinstance(msg, dict) and (msg.get("role") or "").lower() == "user":
                last_user = i
        turn_msgs = messages[last_user + 1 :] if last_user >= 0 else messages
        turn_tool_count = 0
        for msg in turn_msgs:
            if not isinstance(msg, dict):
                continue
            turn_tool_count += len(msg.get("tool_calls") or [])
            if (msg.get("role") or "").lower() == "tool":
                turn_tool_count += 1
        if turn_tool_count > 0:
            return _NO_TEXT_REPLY_MARKER, "none_after_tools"
        return "", ""
    # Export payload set only ``tool_call_count`` without per-message rows.
    if _count_tool_calls(data) > 0:
        return _NO_TEXT_REPLY_MARKER, "none_after_tools"
    return "", ""


# Back-compat alias for tests / callers that still import the old name.
_recover_empty_stdout = _recover_from_session


def _salvage_stdout_reply(out: str) -> str:
    """Best-effort recovery of a real reply from ``chat -Q`` stdout.

    Used only when the session store has no assistant text for the current turn
    yet Hermes still generated one (it finished the turn but failed to persist
    the assistant row). Strips obvious tool-preview / diff decoration and
    returns the remainder; returns ``""`` when what's left is too thin to be a
    genuine answer, so the caller can keep the clean NO_TEXT marker instead.
    """
    cleaned = ha._strip_ansi(out or "")
    if not cleaned.strip():
        return ""
    kept = [ln for ln in cleaned.splitlines() if not _TOOL_PREVIEW_LINE_RE.match(ln)]
    result = "\n".join(kept).strip()
    if len(re.sub(r"\s+", "", result)) < _MIN_SALVAGED_STDOUT_CHARS:
        return ""
    return result


def _resolve_done(
    job: ChatJob,
    rc: int,
    out: str,
    *,
    preferred_session_id: str | None = None,
) -> tuple[str, FinalSource] | None:
    """Decide whether this hermes exit is a successful turn. Returns
    ``(final_text, source)`` on success, else ``None`` (error/retry path).

    Ordering:
    1. Session export assistant text — authoritative when present.
    2. If export reports a tool-only turn (no assistant text), salvage a genuine
       reply from stdout before accepting the bare NO_TEXT marker, so a reply
       Hermes generated but failed to persist is never silently swallowed
       (market-strategist regression).
    3. Otherwise fall back to raw stdout (the reply channel for a plain turn).
    """
    if rc != 0:
        return None
    text, source = _recover_from_session(
        job, preferred_session_id=preferred_session_id
    )
    if source == "session_export":
        return text, source
    if source == "none_after_tools":
        salvaged = _salvage_stdout_reply(out)
        if salvaged:
            _bind_session_id(job, preferred_session_id)
            return salvaged, "stdout"
        return text, source
    stdout_text = (out or "").strip()
    if stdout_text:
        _bind_session_id(job, preferred_session_id)
        return stdout_text, "stdout"
    return None


def _bind_session_id(job: ChatJob, preferred_session_id: str | None) -> None:
    """Ensure the job carries a Hermes session id without clobbering a good one."""
    job.hermes_session_id = (
        job.hermes_session_id
        or preferred_session_id
        or _discover_session_id(job.agent_id)
    )


def _run_turn(
    job: ChatJob,
    message: str,
    workdir: str,
    resume: bool,
    resume_session_id: str | None = None,
) -> None:
    """Run one turn, guaranteeing the job never wedges in ``running``.

    The actual work lives in :func:`_run_turn_impl`; this wrapper guarantees
    that *any* unexpected exception (e.g. ``subprocess.Popen`` raising
    ``FileNotFoundError`` on a bad cwd) transitions the job to ``error`` instead
    of silently killing the thread and leaving the WebUI spinning forever.
    """
    try:
        _run_turn_impl(job, message, workdir, resume, resume_session_id)
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


def _run_turn_impl(
    job: ChatJob,
    message: str,
    workdir: str,
    resume: bool,
    resume_session_id: str | None = None,
) -> None:
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
            job.agent_id,
            message=message,
            workdir=workdir,
            resume=use_resume,
            resume_session_id=resume_session_id if use_resume else None,
        )
        job.proc = proc
        _subproc_registry.register(proc)
        rc, out, err = _communicate(proc)
        _subproc_registry.unregister(proc)
        job.proc = None

        if get_job(job.session_key) is not job:
            return

        stderr_session_id = _parse_session_id_from_stderr(err)
        done = _resolve_done(
            job,
            rc,
            out,
            preferred_session_id=stderr_session_id
            or (resume_session_id if use_resume else None),
        )
        if done is not None:
            final_text, source = done
            _ensure_job_session_id(job)
            _finish_job(job, status="done", final_text=final_text)
            logger.info(
                "hermes_chat_finished",
                session_key=job.session_key,
                agent_id=job.agent_id,
                status="done",
                final_source=source,
                final_len=len(final_text),
                hermes_session_id=job.hermes_session_id,
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
                job.agent_id,
                message=message,
                workdir=workdir,
                resume=False,
                resume_session_id=None,
            )
            job.proc = proc2
            _subproc_registry.register(proc2)
            rc, out, err = _communicate(proc2)
            _subproc_registry.unregister(proc2)
            job.proc = None
            stderr_session_id = _parse_session_id_from_stderr(err)
            done = _resolve_done(job, rc, out, preferred_session_id=stderr_session_id)
            if done is not None:
                final_text, source = done
                _ensure_job_session_id(job)
                _finish_job(job, status="done", final_text=final_text)
                logger.info(
                    "hermes_chat_finished",
                    session_key=job.session_key,
                    agent_id=job.agent_id,
                    status="done",
                    final_source=source,
                    final_len=len(final_text),
                    hermes_session_id=job.hermes_session_id,
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
            use_resume = resume
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
