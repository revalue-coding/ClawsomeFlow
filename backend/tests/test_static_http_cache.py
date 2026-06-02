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
