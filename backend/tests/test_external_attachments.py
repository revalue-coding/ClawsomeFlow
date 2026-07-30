"""services/external_attachments — sanitisation, limits, path safety."""

from __future__ import annotations

import io

import pytest

from app.paths import run_dir
from app.services.external_attachments import (
    MAX_ATTACHMENT_COUNT,
    AttachmentError,
    format_attachments_block,
    resolve_attachment_path,
    sanitize_attachment_name,
    save_attachments,
)


def test_sanitize_attachment_name_strips_paths_and_controls() -> None:
    assert sanitize_attachment_name("../../etc/passwd") == "passwd"
    assert sanitize_attachment_name("..\\..\\win.ini") == "win.ini"
    assert sanitize_attachment_name("a\x00b\nc.txt") == "abc.txt"
    assert sanitize_attachment_name("  .hidden.  ") == "hidden"
    assert sanitize_attachment_name("") == "attachment"
    assert sanitize_attachment_name(None) == "attachment"
    assert sanitize_attachment_name('a<b>:c"|?*.txt') == "a_b__c____.txt"
    # Long names keep their extension.
    long = sanitize_attachment_name("x" * 300 + ".pdf")
    assert long.endswith(".pdf") and len(long) <= 120
    # CJK filenames survive (photos from a phone).
    assert sanitize_attachment_name("现场照片.jpg") == "现场照片.jpg"


def test_save_attachments_writes_dedupes_and_reports() -> None:
    saved = save_attachments(
        run_id="run-att-1", task_id="t1", nonce="n-1",
        files=[
            ("a.txt", io.BytesIO(b"one")),
            ("a.txt", io.BytesIO(b"two")),
            ("../b.txt", io.BytesIO(b"three")),
        ],
    )
    names = [s["name"] for s in saved]
    assert names == ["a.txt", "a-1.txt", "b.txt"]
    base = run_dir("run-att-1") / "attachments" / "t1" / "n-1"
    assert (base / "a-1.txt").read_bytes() == b"two"
    assert saved[2]["path"] == str(base / "b.txt")
    assert saved[0]["size"] == 3


def test_save_attachments_enforces_count_and_size(monkeypatch) -> None:
    too_many = [(f"f{i}.txt", io.BytesIO(b"x")) for i in range(MAX_ATTACHMENT_COUNT + 1)]
    with pytest.raises(AttachmentError) as exc:
        save_attachments(
            run_id="run-att-2", task_id="t1", nonce="n-1", files=too_many,
        )
    assert exc.value.code == "EXTERNAL_ATTACHMENTS_TOO_MANY"

    from app.services import external_attachments as mod

    monkeypatch.setattr(mod, "MAX_ATTACHMENT_BYTES", 4)
    with pytest.raises(AttachmentError) as exc2:
        save_attachments(
            run_id="run-att-2", task_id="t1", nonce="n-2",
            files=[("ok.txt", io.BytesIO(b"hi")), ("big.bin", io.BytesIO(b"toolarge"))],
        )
    assert exc2.value.code == "EXTERNAL_ATTACHMENT_TOO_LARGE"
    # Failed batch cleans up its partial writes.
    base = run_dir("run-att-2") / "attachments" / "t1" / "n-2"
    assert not (base / "ok.txt").exists()
    assert not (base / "big.bin").exists()


def test_resolve_attachment_path_is_traversal_safe() -> None:
    save_attachments(
        run_id="run-att-3", task_id="t1", nonce="n-1",
        files=[("ok.txt", io.BytesIO(b"data"))],
    )
    found = resolve_attachment_path("run-att-3", "t1", "n-1", "ok.txt")
    assert found is not None and found.read_bytes() == b"data"
    assert resolve_attachment_path("run-att-3", "t1", "n-1", "missing.txt") is None
    assert resolve_attachment_path("run-att-3", "t1", "n-1", "../ok.txt") is None
    assert resolve_attachment_path("run-att-3", "t1", "n-1", "..") is None


def test_format_attachments_block_lists_paths() -> None:
    block = format_attachments_block(
        [{"name": "a.pdf", "path": "/x/a.pdf", "size": 2048}], lang="zh",
    )
    assert block.startswith("## 回执附件")
    assert "- a.pdf (2.0KB): /x/a.pdf" in block
    en = format_attachments_block(
        [{"name": "a.pdf", "path": "/x/a.pdf", "size": 10}], lang="en",
    )
    assert en.startswith("## Receipt Attachments")
    assert format_attachments_block([]) == ""
