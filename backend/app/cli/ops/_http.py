"""Tiny synchronous HTTP wrapper used by the ops CLIs.

Talks to the local backend (``Config.csflow_port``) so the user can drive
flows / runs / agents from a terminal. Always uses ``CSFLOW_USER`` (or
``Config.default_user``) when the backend resolves the caller — matches
``app.api._auth.current_user``.
"""

from __future__ import annotations

import os
from typing import Any

import httpx
import typer

from app import config as cfg_mod


def _base() -> str:
    cfg = cfg_mod.load_config()
    if base := os.environ.get("CSFLOW_BASE_URL"):
        return base.rstrip("/")
    host = os.environ.get("CSFLOW_HOST", "127.0.0.1")
    return f"http://{host}:{cfg.csflow_port}"


def _headers() -> dict[str, str]:
    cfg = cfg_mod.load_config()
    user = os.environ.get("CSFLOW_USER") or cfg.default_user
    return {"X-CSFLOW-User": user}


def _client() -> httpx.Client:
    return httpx.Client(base_url=_base(), timeout=20.0, headers=_headers())


def _check(r: httpx.Response) -> dict | list:
    if r.status_code >= 400:
        try:
            err = r.json()
            typer.echo(
                f"✗ HTTP {r.status_code} {err.get('error', '?')}: {err.get('message', '')}",
                err=True,
            )
        except Exception:
            typer.echo(f"✗ HTTP {r.status_code}: {r.text[:300]}", err=True)
        raise typer.Exit(code=1)
    if r.status_code == 204 or not r.content:
        return {}
    return r.json()


def get(path: str, **params) -> Any:
    with _client() as c:
        return _check(c.get(path, params=params or None))


def post(path: str, body: Any | None = None) -> Any:
    with _client() as c:
        return _check(c.post(path, json=body if body is not None else {}))


def put(path: str, body: Any | None = None) -> Any:
    with _client() as c:
        return _check(c.put(path, json=body if body is not None else {}))


def delete(path: str) -> Any:
    with _client() as c:
        return _check(c.delete(path))


__all__ = ["delete", "get", "post", "put"]
