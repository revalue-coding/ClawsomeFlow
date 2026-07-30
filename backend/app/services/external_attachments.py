"""Attachment storage for external-node task receipts (human channel).

A person answering an external task — through the public reply form
(``/api/external/reply/...``) or the WebUI card — may attach files (photos,
PDFs, spreadsheets…). They are persisted under the run's own state directory::

    ~/.clawsomeflow/.runs/{run_id}/attachments/{task_id}/{nonce}/<name>

so they live and die with the run directory (no separate GC), stay inside the
ClawsomeFlow data home (never in a worktree), and are keyed by the dispatch
nonce — a re-dispatched attempt writes to a fresh directory and can never
collide with a stale submission.

Limits are deliberately simple and documented in API.md: at most
:data:`MAX_ATTACHMENT_COUNT` files per receipt, each up to
:data:`MAX_ATTACHMENT_BYTES`. Filenames are sanitised to a safe basename
(path separators and control characters stripped; empty → ``attachment``)
and de-duplicated with a numeric suffix.

The saved records (``{"name", "path", "size"}``) travel two ways:

* structured, on the ``external_task_completed`` event payload (WebUI download
  links), and
* human/agent-readable, as a ``format_attachments_block`` section appended to
  the receipt summary — which the existing upstream-output passthrough hands
  to downstream agent prompts unchanged.
"""

from __future__ import annotations

import re
import unicodedata
from pathlib import Path
from typing import Any, BinaryIO

from app.logging_setup import get_logger
from app.paths import ensure_within_root, run_dir, validate_identifier

logger = get_logger("external_attachments")

#: Max files accepted per receipt submission.
MAX_ATTACHMENT_COUNT = 10
#: Max bytes per file (50 MB).
MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024

_COPY_CHUNK = 1024 * 1024
_MAX_NAME_LEN = 120


class AttachmentError(Exception):
    """Business error for attachment handling (maps to 400-class responses)."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(f"[{code}] {message}")
        self.code = code
        self.message = message


def sanitize_attachment_name(raw: str | None) -> str:
    """Reduce *raw* to a safe basename (never empty, never a path)."""
    name = unicodedata.normalize("NFC", str(raw or ""))
    # Basename only — strip any client-supplied directory part (both styles).
    name = name.replace("\\", "/").rsplit("/", 1)[-1]
    # Control chars are never legitimate in a filename.
    name = "".join(ch for ch in name if unicodedata.category(ch)[0] != "C")
    name = name.strip().strip(".")
    # Windows-reserved + separator-ish characters → underscore.
    name = re.sub(r'[<>:"|?*]', "_", name)
    if len(name) > _MAX_NAME_LEN:
        stem, dot, ext = name.rpartition(".")
        if dot and 0 < len(ext) <= 16:
            name = stem[: _MAX_NAME_LEN - len(ext) - 1].rstrip(".") + "." + ext
        else:
            name = name[:_MAX_NAME_LEN]
    return name or "attachment"


def attachments_dir(run_id: str, task_id: str, nonce: str) -> Path:
    """Per-(run, task, dispatch-attempt) attachment directory (not created)."""
    return ensure_within_root(
        run_dir(run_id),
        "attachments",
        validate_identifier(task_id, "task id"),
        validate_identifier(nonce, "dispatch nonce"),
    )


def save_attachments(
    *,
    run_id: str,
    task_id: str,
    nonce: str,
    files: list[tuple[str | None, BinaryIO]],
) -> list[dict[str, Any]]:
    """Persist uploaded *files*; returns ``[{"name","path","size"}, ...]``.

    Raises :class:`AttachmentError` on limit violations. Partial writes from a
    failed batch are removed so a retry starts clean (the directory is keyed by
    nonce, so no other submission can be racing it).
    """
    entries = [(name, fh) for name, fh in files if fh is not None]
    if not entries:
        return []
    if len(entries) > MAX_ATTACHMENT_COUNT:
        raise AttachmentError(
            "EXTERNAL_ATTACHMENTS_TOO_MANY",
            f"at most {MAX_ATTACHMENT_COUNT} attachments per submission",
        )
    target = attachments_dir(run_id, task_id, nonce)
    target.mkdir(parents=True, exist_ok=True)
    saved: list[dict[str, Any]] = []
    used_names: set[str] = set()
    written_paths: list[Path] = []
    try:
        for raw_name, fh in entries:
            name = _dedupe_name(sanitize_attachment_name(raw_name), used_names)
            used_names.add(name)
            dest = ensure_within_root(target, name)
            size = _copy_limited(fh, dest)
            written_paths.append(dest)
            saved.append({"name": name, "path": str(dest), "size": size})
    except Exception:
        for p in written_paths:
            try:
                p.unlink(missing_ok=True)
            except OSError:  # pragma: no cover — best-effort cleanup
                pass
        raise
    logger.info(
        "external_attachments_saved",
        run_id=run_id, task_id=task_id, nonce=nonce,
        count=len(saved), bytes=sum(int(s["size"]) for s in saved),
    )
    return saved


def resolve_attachment_path(
    run_id: str, task_id: str, nonce: str, name: str,
) -> Path | None:
    """Existing attachment file for a WebUI download, or ``None``.

    Every path component is re-validated so a crafted name can never escape
    the run's attachment directory.
    """
    try:
        safe_name = sanitize_attachment_name(name)
        if safe_name != name:
            return None
        path = ensure_within_root(
            attachments_dir(run_id, task_id, nonce), safe_name,
        )
    except ValueError:
        return None
    return path if path.is_file() else None


def format_attachments_block(
    attachments: list[dict[str, Any]], *, lang: str | None = None,
) -> str:
    """Fixed-shape summary section listing saved attachment paths.

    Appended to the receipt summary so downstream agent prompts (which receive
    the summary verbatim through upstream-output passthrough) see the local
    absolute paths to work with.
    """
    if not attachments:
        return ""
    zh = (lang or "").strip().lower() == "zh"
    header = "## 回执附件（本地绝对路径）" if zh else "## Receipt Attachments (local absolute paths)"
    lines = [header]
    for item in attachments:
        name = str(item.get("name") or "")
        path = str(item.get("path") or "")
        size = item.get("size")
        size_note = f" ({_human_size(size)})" if isinstance(size, int) else ""
        lines.append(f"- {name}{size_note}: {path}")
    return "\n".join(lines)


def _dedupe_name(name: str, used: set[str]) -> str:
    if name not in used:
        return name
    stem, dot, ext = name.rpartition(".")
    base, suffix = (stem, f".{ext}") if dot else (name, "")
    for i in range(1, 1000):
        candidate = f"{base}-{i}{suffix}"
        if candidate not in used:
            return candidate
    raise AttachmentError(  # pragma: no cover — needs 1000 duplicates
        "EXTERNAL_ATTACHMENTS_TOO_MANY", "could not derive a unique filename",
    )


def _copy_limited(fh: BinaryIO, dest: Path) -> int:
    """Stream *fh* to *dest*, enforcing the per-file size cap."""
    total = 0
    try:
        with dest.open("wb") as out:
            while True:
                chunk = fh.read(_COPY_CHUNK)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ATTACHMENT_BYTES:
                    raise AttachmentError(
                        "EXTERNAL_ATTACHMENT_TOO_LARGE",
                        f"attachment exceeds {MAX_ATTACHMENT_BYTES // (1024 * 1024)} MB",
                    )
                out.write(chunk)
    except AttachmentError:
        dest.unlink(missing_ok=True)
        raise
    return total


def _human_size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KB", "MB", "GB"):
        if value < 1024 or unit == "GB":
            return f"{value:.0f}{unit}" if unit == "B" else f"{value:.1f}{unit}"
        value /= 1024
    return f"{size}B"  # pragma: no cover — unreachable


__all__ = [
    "MAX_ATTACHMENT_BYTES",
    "MAX_ATTACHMENT_COUNT",
    "AttachmentError",
    "attachments_dir",
    "format_attachments_block",
    "resolve_attachment_path",
    "sanitize_attachment_name",
    "save_attachments",
]
