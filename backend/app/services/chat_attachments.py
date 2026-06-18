"""Shared attachment helpers for single-agent chat endpoints.

This module keeps all file-upload constraints in one place for OpenClaw/Hermes:

* bounded upload size/count
* filename sanitisation and path-traversal protection
* controlled storage under ``.csflow-chat-uploads/``
* path-injection prompt construction
"""

from __future__ import annotations

import os
import re
import secrets
import time
from dataclasses import dataclass
from pathlib import Path

_UPLOAD_DIRNAME = ".csflow-chat-uploads"
_DEFAULT_MAX_ATTACHMENT_COUNT = 8
_DEFAULT_MAX_ATTACHMENT_SIZE_BYTES = 10 * 1024 * 1024
_DEFAULT_MAX_ATTACHMENT_TOTAL_BYTES = 30 * 1024 * 1024

_SAFE_FILENAME_RE = re.compile(r"[^A-Za-z0-9._-]+")

_ALLOWED_SUFFIXES = {
    ".txt",
    ".md",
    ".markdown",
    ".json",
    ".yaml",
    ".yml",
    ".toml",
    ".ini",
    ".cfg",
    ".conf",
    ".csv",
    ".tsv",
    ".log",
    ".py",
    ".js",
    ".jsx",
    ".ts",
    ".tsx",
    ".c",
    ".cc",
    ".cpp",
    ".h",
    ".hpp",
    ".java",
    ".go",
    ".rs",
    ".sh",
    ".bash",
    ".zsh",
    ".sql",
    ".xml",
    ".html",
    ".css",
    ".pdf",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".webp",
    ".bmp",
    ".svg",
    ".mp3",
    ".wav",
    ".ogg",
    ".mp4",
    ".mov",
    ".zip",
    ".gz",
    ".tgz",
    ".tar",
}
_ALLOWED_MIME_PREFIXES = ("text/", "image/", "audio/", "video/")
_ALLOWED_MIME_EXACT = {
    "application/pdf",
    "application/json",
    "application/xml",
    "application/yaml",
    "application/x-yaml",
    "application/zip",
    "application/x-gzip",
    "application/gzip",
    "application/x-tar",
    "application/octet-stream",
}

def _int_env(name: str, default: int, *, minimum: int = 1) -> int:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        return default
    return max(value, minimum)


MAX_ATTACHMENT_COUNT = _int_env(
    "CSFLOW_CHAT_MAX_ATTACHMENT_COUNT",
    _DEFAULT_MAX_ATTACHMENT_COUNT,
)
MAX_ATTACHMENT_SIZE_BYTES = _int_env(
    "CSFLOW_CHAT_MAX_ATTACHMENT_BYTES",
    _DEFAULT_MAX_ATTACHMENT_SIZE_BYTES,
)
MAX_ATTACHMENT_TOTAL_BYTES = _int_env(
    "CSFLOW_CHAT_MAX_TOTAL_ATTACHMENT_BYTES",
    _DEFAULT_MAX_ATTACHMENT_TOTAL_BYTES,
)


@dataclass(frozen=True)
class StoredAttachment:
    id: str
    name: str
    mime_type: str
    size_bytes: int
    absolute_path: str
    relative_path: str


def _normalise_mime(mime_type: str | None) -> str:
    return (mime_type or "").strip().lower()


def sanitize_filename(raw_name: str) -> str:
    base = Path((raw_name or "").strip()).name
    if not base:
        raise ValueError("filename is required")
    stem = Path(base).stem
    suffix = Path(base).suffix.lower()
    safe_stem = _SAFE_FILENAME_RE.sub("_", stem).strip("._-")
    if not safe_stem:
        safe_stem = "file"
    safe_suffix = _SAFE_FILENAME_RE.sub("", suffix).strip()
    if safe_suffix and not safe_suffix.startswith("."):
        safe_suffix = f".{safe_suffix}"
    return f"{safe_stem}{safe_suffix}"


def _is_allowed_mime(mime_type: str) -> bool:
    if not mime_type:
        return True
    if mime_type in _ALLOWED_MIME_EXACT:
        return True
    return any(mime_type.startswith(prefix) for prefix in _ALLOWED_MIME_PREFIXES)


def _is_allowed_suffix(filename: str) -> bool:
    return Path(filename).suffix.lower() in _ALLOWED_SUFFIXES


def validate_name_and_type(*, filename: str, mime_type: str) -> None:
    if not _is_allowed_suffix(filename):
        raise ValueError("file type is not allowed")
    if not _is_allowed_mime(mime_type):
        raise ValueError("mime type is not allowed")


def upload_root_for(base_dir: Path, *, create: bool) -> Path:
    root = base_dir.expanduser().resolve(strict=False)
    if not root.exists() or not root.is_dir():
        raise ValueError(f"upload base directory does not exist: {root}")
    uploads = root / _UPLOAD_DIRNAME
    if create:
        uploads.mkdir(parents=True, exist_ok=True)
    return uploads


def _ensure_within(parent: Path, child: Path) -> Path:
    parent_resolved = parent.expanduser().resolve(strict=False)
    child_resolved = child.expanduser().resolve(strict=False)
    try:
        child_resolved.relative_to(parent_resolved)
    except ValueError as exc:
        raise ValueError("attachment path escapes allowed upload root") from exc
    return child_resolved


def store_upload_bytes(
    *,
    base_dir: Path,
    raw_filename: str,
    mime_type: str,
    content: bytes,
) -> StoredAttachment:
    if not content:
        raise ValueError("uploaded file is empty")
    if len(content) > MAX_ATTACHMENT_SIZE_BYTES:
        raise ValueError("uploaded file exceeds size limit")
    safe_name = sanitize_filename(raw_filename)
    normal_mime = _normalise_mime(mime_type)
    validate_name_and_type(filename=safe_name, mime_type=normal_mime)

    root = base_dir.expanduser().resolve(strict=False)
    uploads = upload_root_for(root, create=True).resolve(strict=False)

    stamp = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    nonce = secrets.token_hex(4)
    stored_name = f"{stamp}-{nonce}-{safe_name}"
    target = uploads / stored_name
    target.write_bytes(content)
    try:
        os.chmod(target, 0o600)
    except OSError:
        # Best effort only; chmod can fail on some filesystems.
        pass

    resolved_target = _ensure_within(uploads, target)
    rel_path = str(resolved_target.relative_to(root))
    return StoredAttachment(
        id=f"att-{int(time.time() * 1000)}-{nonce}",
        name=safe_name,
        mime_type=normal_mime,
        size_bytes=len(content),
        absolute_path=str(resolved_target),
        relative_path=rel_path,
    )


def resolve_existing_attachment(
    *,
    base_dir: Path,
    absolute_path: str,
    name: str,
    mime_type: str,
) -> StoredAttachment:
    if not absolute_path:
        raise ValueError("attachment path is required")
    safe_name = sanitize_filename(name or Path(absolute_path).name)
    normal_mime = _normalise_mime(mime_type)
    validate_name_and_type(filename=safe_name, mime_type=normal_mime)

    root = base_dir.expanduser().resolve(strict=False)
    uploads = upload_root_for(root, create=False).resolve(strict=False)
    candidate = Path(absolute_path).expanduser().resolve(strict=False)
    resolved = _ensure_within(uploads, candidate)
    if not resolved.exists() or not resolved.is_file():
        raise ValueError(f"attachment file not found: {resolved}")

    size_bytes = resolved.stat().st_size
    if size_bytes <= 0:
        raise ValueError(f"attachment file is empty: {resolved}")
    if size_bytes > MAX_ATTACHMENT_SIZE_BYTES:
        raise ValueError("attachment exceeds size limit")

    rel_path = str(resolved.relative_to(root))
    return StoredAttachment(
        id=f"att-{resolved.stem}",
        name=safe_name,
        mime_type=normal_mime,
        size_bytes=size_bytes,
        absolute_path=str(resolved),
        relative_path=rel_path,
    )


def validate_batch_limits(attachments: list[StoredAttachment]) -> None:
    if len(attachments) > MAX_ATTACHMENT_COUNT:
        raise ValueError("attachment count exceeds limit")
    total = sum(item.size_bytes for item in attachments)
    if total > MAX_ATTACHMENT_TOTAL_BYTES:
        raise ValueError("attachments total size exceeds limit")


def build_path_injection_message(*, user_message: str, attachments: list[StoredAttachment]) -> str:
    if not attachments:
        return user_message
    lines: list[str] = []
    base = (user_message or "").rstrip()
    if base:
        lines.append(base)
        lines.append("")
    lines.append("## ClawsomeFlow Uploaded Attachments")
    lines.append("Use the following files from your workspace as context:")
    for idx, item in enumerate(attachments, start=1):
        lines.append(f"{idx}. {item.name}")
        lines.append(f"   path: {item.relative_path}")
        if item.mime_type:
            lines.append(f"   mime: {item.mime_type}")
        lines.append(f"   size_bytes: {item.size_bytes}")
    lines.append(
        "Read these paths directly. If a file is binary/media, use tools that can inspect it."
    )
    return "\n".join(lines).strip()


__all__ = [
    "MAX_ATTACHMENT_COUNT",
    "MAX_ATTACHMENT_SIZE_BYTES",
    "MAX_ATTACHMENT_TOTAL_BYTES",
    "StoredAttachment",
    "build_path_injection_message",
    "resolve_existing_attachment",
    "sanitize_filename",
    "store_upload_bytes",
    "upload_root_for",
    "validate_batch_limits",
    "validate_name_and_type",
]
