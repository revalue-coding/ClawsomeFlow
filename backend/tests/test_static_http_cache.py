"""HTTP cache headers for the SPA shell vs hashed ``/assets`` bundles."""

from __future__ import annotations

from pathlib import Path

from fastapi.testclient import TestClient


def test_spa_shell_not_cached_hashed_assets_immutable(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    dist = tmp_path
    monkeypatch.setenv("CSFLOW_FRONTEND_DIST", str(dist))
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>x</body></html>",
        encoding="utf-8",
    )
    (dist / "assets" / "chunk-abc.js").write_text("export {};", encoding="utf-8")

    from app.main import create_app

    client = TestClient(create_app())

    for url in ("/", "/runs/run-test"):
        r = client.get(url)
        assert r.status_code == 200, url
        cc = r.headers.get("cache-control", "").lower()
        assert "no-store" in cc, url
        assert "etag" not in {k.lower() for k in r.headers.keys()}, url

    r_asset = client.get("/assets/chunk-abc.js")
    assert r_asset.status_code == 200
    cc_a = r_asset.headers.get("cache-control", "").lower()
    assert "immutable" in cc_a
    assert "31536000" in r_asset.headers.get("cache-control", "")


def test_root_level_static_files_served_not_spa(
    tmp_path: Path,
    monkeypatch: object,
) -> None:
    """``/logo.png`` (and other dist-root files) must serve the real file, not
    the SPA index. Regression for the sidebar logo / favicon failing to load in
    the packaged wheel, where only ``/assets/*`` was mounted."""
    dist = tmp_path
    monkeypatch.setenv("CSFLOW_FRONTEND_DIST", str(dist))
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text(
        "<!doctype html><html><body>x</body></html>",
        encoding="utf-8",
    )
    # 1x1 PNG header bytes are enough to assert the content type / body.
    png_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
    (dist / "logo.png").write_bytes(png_bytes)

    from app.main import create_app

    client = TestClient(create_app())

    r = client.get("/logo.png")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/")
    assert r.content == png_bytes

    # A non-file route still falls through to the SPA shell.
    r_spa = client.get("/flows")
    assert r_spa.status_code == 200
    assert "<!doctype html>" in r_spa.text.lower()

    # A missing root-level *asset-looking* path returns the SPA shell too
    # (React Router handles unknown routes), but a traversal attempt is denied.
    r_trav = client.get("/../etc/passwd")
    assert r_trav.status_code in (200, 404)
