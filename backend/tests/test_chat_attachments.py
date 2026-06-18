from __future__ import annotations

from pathlib import Path

import pytest

from app.services import chat_attachments as svc


def test_sanitize_filename_removes_traversal_and_weird_chars() -> None:
    assert svc.sanitize_filename("../../evil?.md") == "evil.md"
    assert svc.sanitize_filename("  hello world!!.txt ") == "hello_world.txt"


def test_store_upload_bytes_writes_under_controlled_directory(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    item = svc.store_upload_bytes(
        base_dir=root,
        raw_filename="spec.md",
        mime_type="text/markdown",
        content=b"# spec",
    )
    assert item.relative_path.startswith(".csflow-chat-uploads/")
    target = Path(item.absolute_path)
    assert target.exists()
    assert target.read_bytes() == b"# spec"


def test_store_upload_bytes_rejects_disallowed_type(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    with pytest.raises(ValueError, match="file type is not allowed"):
        svc.store_upload_bytes(
            base_dir=root,
            raw_filename="script.exe",
            mime_type="application/octet-stream",
            content=b"x",
        )


def test_resolve_existing_attachment_rejects_path_escape(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    outside = tmp_path / "outside.md"
    outside.write_text("oops", encoding="utf-8")
    with pytest.raises(ValueError, match="escapes allowed upload root"):
        svc.resolve_existing_attachment(
            base_dir=root,
            absolute_path=str(outside),
            name="outside.md",
            mime_type="text/markdown",
        )


def test_validate_batch_limits_rejects_excess_count(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    items = [
        svc.store_upload_bytes(
            base_dir=root,
            raw_filename=f"f{idx}.md",
            mime_type="text/markdown",
            content=b"x",
        )
        for idx in range(svc.MAX_ATTACHMENT_COUNT + 1)
    ]
    with pytest.raises(ValueError, match="count exceeds limit"):
        svc.validate_batch_limits(items)


def test_build_path_injection_message_contains_relative_paths(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    root.mkdir(parents=True, exist_ok=True)
    item = svc.store_upload_bytes(
        base_dir=root,
        raw_filename="notes.md",
        mime_type="text/markdown",
        content=b"hello",
    )
    msg = svc.build_path_injection_message(
        user_message="please check",
        attachments=[item],
    )
    assert "## ClawsomeFlow Uploaded Attachments" in msg
    assert item.relative_path in msg
    assert "please check" in msg


