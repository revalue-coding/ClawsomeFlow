"""Tests for app.integrations.openclaw_cli."""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from app.integrations import openclaw_cli as oc


def _make_executable(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/usr/bin/env bash\nexit 0\n", encoding="utf-8")
    path.chmod(0o755)


def test_resolve_openclaw_prefers_npm_prefix_bin(
    monkeypatch,
    tmp_path: Path,
) -> None:
    prefix = tmp_path / "npm-prefix"
    candidate = prefix / "bin" / "openclaw"
    _make_executable(candidate)
    monkeypatch.setenv("PATH", "/usr/bin")
    monkeypatch.setattr(oc, "_probe_openclaw_from_default_gateway_service", lambda: None)
    monkeypatch.setattr(
        oc.shutil,
        "which",
        lambda name: "/usr/bin/npm" if name == "npm" else "/usr/bin/openclaw",
    )
    captured: dict[str, object] = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{prefix}\n",
            stderr="",
        )

    monkeypatch.setattr(oc.subprocess, "run", _fake_run)

    resolved = oc.resolve_openclaw_executable(run_login_shell_probe=False)
    assert resolved == str(candidate)
    assert captured["argv"] == ["/usr/bin/npm", "prefix", "-g"]
    assert os.environ.get("PATH", "").split(os.pathsep)[0] == str(candidate.parent)


def test_resolve_openclaw_prefers_default_gateway_service_path(monkeypatch) -> None:
    monkeypatch.setattr(
        oc,
        "_probe_openclaw_from_default_gateway_service",
        lambda: "/service/bin/openclaw",
    )
    monkeypatch.setattr(
        oc,
        "_probe_openclaw_from_npm_prefix",
        lambda: "/npm/bin/openclaw",
    )
    monkeypatch.setattr(oc.shutil, "which", lambda _: "/usr/bin/openclaw")
    assert oc.resolve_openclaw_executable(run_login_shell_probe=False) == "/service/bin/openclaw"


def test_resolve_openclaw_prefers_path_lookup(monkeypatch) -> None:
    monkeypatch.setattr(oc, "_probe_openclaw_from_default_gateway_service", lambda: None)
    monkeypatch.setattr(oc, "_probe_openclaw_from_npm_prefix", lambda: None)
    monkeypatch.setattr(oc.shutil, "which", lambda _: "/usr/bin/openclaw")
    assert oc.resolve_openclaw_executable(run_login_shell_probe=False) == "/usr/bin/openclaw"


def test_resolve_openclaw_discovers_nvm_candidate_and_prepends_path(
    monkeypatch,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / ".nvm" / "versions" / "node" / "v22.22.3" / "bin" / "openclaw"
    _make_executable(candidate)
    monkeypatch.setattr(oc, "_probe_openclaw_from_default_gateway_service", lambda: None)
    monkeypatch.setattr(oc, "_probe_openclaw_from_npm_prefix", lambda: None)
    monkeypatch.setattr(oc.shutil, "which", lambda _: None)
    monkeypatch.setenv("PATH", "/usr/bin")

    resolved = oc.resolve_openclaw_executable(
        home=tmp_path,
        run_login_shell_probe=False,
    )

    assert resolved == str(candidate)
    assert os.environ.get("PATH", "").split(os.pathsep)[0] == str(candidate.parent)


def test_resolve_openclaw_uses_login_shell_probe_when_needed(
    monkeypatch,
    tmp_path: Path,
) -> None:
    candidate = tmp_path / "shell-bin" / "openclaw"
    _make_executable(candidate)
    monkeypatch.setattr(oc, "_probe_openclaw_from_default_gateway_service", lambda: None)
    monkeypatch.setattr(oc, "_probe_openclaw_from_npm_prefix", lambda: None)
    monkeypatch.setattr(oc.shutil, "which", lambda _: None)
    # Neutralise the well-known-locations scan (step 4): the Docker test image
    # installs a real openclaw at /usr/bin/openclaw, which would otherwise be
    # discovered here before the login-shell probe (step 5) under test.
    monkeypatch.setattr(oc, "_iter_fallback_candidates", lambda _home: iter(()))
    # Point PATH at a dir with no `openclaw` so the PATH lookup also finds nothing.
    monkeypatch.setenv("PATH", str(tmp_path / "empty-no-openclaw"))
    captured: dict[str, object] = {}

    def _fake_run(argv, **kwargs):
        captured["argv"] = argv
        captured["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            argv,
            0,
            stdout=f"{candidate}\n",
            stderr="",
        )

    monkeypatch.setattr(oc.subprocess, "run", _fake_run)

    resolved = oc.resolve_openclaw_executable(
        home=tmp_path,
        run_login_shell_probe=True,
    )

    assert resolved == str(candidate)
    assert captured["argv"] == ["bash", "-lc", "command -v openclaw"]
    assert os.environ.get("PATH", "").split(os.pathsep)[0] == str(candidate.parent)
