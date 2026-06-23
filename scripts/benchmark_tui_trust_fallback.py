#!/usr/bin/env python3
"""Benchmark wait_tui_ready fallback latency per agent platform.

Simulates ClawTeam missing startup gates (``spawn_ready_timeout=0.01``) for
claude/codex/gemini, then measures ``wait_tui_ready`` recovery time.

Usage (from repo root)::
    cd backend && python3 ../scripts/benchmark_tui_trust_fallback.py --runs 20
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import statistics
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

# Allow ``python3 ../scripts/...`` from backend/
_BACKEND = Path(__file__).resolve().parents[1] / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from app.scheduler.sessions.tmux_ready import (  # noqa: E402
    TRUST_HANDLED_PLATFORMS,
    _is_composer_ready,
    _pane_active_text,
    _startup_prompt_action,
    resolve_trust_platform,
    wait_tui_ready,
    tmux_capture_pane,
)

# Mirrors tmux_live._KIND_TO_CMD spawn argv (permission flags self-controlled where noted).
_PLATFORM_SPAWN: dict[str, dict] = {
    "claude": {
        "binary": "claude",
        "cmd": ["claude", "--permission-mode", "bypassPermissions"],
        "skip_permissions": False,
    },
    "codex": {
        "binary": "codex",
        "cmd": [
            "codex",
            "-c", "notice.model_migrations={}",
            "-c", "tui.model_availability_nux={}",
            "-c", "disable_paste_burst=true",
        ],
        "skip_permissions": False,
    },
    "gemini": {
        "binary": "gemini",
        "cmd": ["gemini", "--approval-mode", "yolo"],
        "skip_permissions": False,
    },
    "kimi": {"binary": "kimi", "cmd": ["kimi", "--yolo"], "skip_permissions": False},
    "qwen": {
        "binary": "qwen",
        "cmd": ["qwen", "--approval-mode", "yolo", "--chat-recording"],
        "skip_permissions": False,
    },
    "opencode": {"binary": "opencode", "cmd": ["opencode"], "skip_permissions": False},
    "nanobot": {
        "binary": "nanobot",
        "cmd": ["nanobot", "agent", "-s", "bench-nanobot"],
        "skip_permissions": False,
    },
    "hermes": {"binary": "hermes", "cmd": ["hermes", "--yolo"], "skip_permissions": True},
    "qoder": {
        "binary": "qodercli",
        "cmd": ["qodercli", "--permission-mode", "bypass_permissions"],
        "skip_permissions": False,
    },
    "codebuddy": {
        "binary": "codebuddy",
        "cmd": ["codebuddy", "--permission-mode", "bypassPermissions"],
        "skip_permissions": False,
    },
}


@dataclass
class RunResult:
    ok: bool
    wait_sec: float
    startup_prompt: bool
    reason: str = ""
    error: str = ""


@dataclass
class PlatformStats:
    platform: str
    trust_handled: bool
    runs: list[RunResult] = field(default_factory=list)

    def summary(self) -> dict:
        ok_runs = [r for r in self.runs if r.ok]
        fallback = [r for r in ok_runs if r.startup_prompt]
        wait_all = [r.wait_sec for r in ok_runs]
        wait_fb = [r.wait_sec for r in fallback]

        def _stats(vals: list[float]) -> dict:
            if not vals:
                return {"n": 0}
            return {
                "n": len(vals),
                "min": round(min(vals), 3),
                "max": round(max(vals), 3),
                "mean": round(statistics.mean(vals), 3),
                "p50": round(statistics.median(vals), 3),
            }

        return {
            "platform": self.platform,
            "trust_handled": self.trust_handled,
            "attempted": len(self.runs),
            "ok": len(ok_runs),
            "fallback_runs": len(fallback),
            "wait_all_ok": _stats(wait_all),
            "wait_fallback_only": _stats(wait_fb),
            "failures": [
                {"error": r.error, "reason": r.reason, "wait_sec": round(r.wait_sec, 3)}
                for r in self.runs
                if not r.ok
            ],
        }


def _make_repo() -> Path:
    repo = Path(tempfile.mkdtemp(prefix="csflow-bench-repo-"))
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "bench@test"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "bench"], cwd=repo, check=True)
    (repo / "README.md").write_text("benchmark\n", encoding="utf-8")
    subprocess.run(["git", "add", "README.md"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-qm", "init"], cwd=repo, check=True)
    return repo


def _spawn(
    *,
    platform: str,
    team: str,
    agent: str,
    repo: Path,
    cfg: dict,
    cfg_path: Path,
) -> tuple[str, float]:
    meta = _PLATFORM_SPAWN[platform]
    if not shutil.which(meta["binary"]):
        raise RuntimeError(f"{meta['binary']} not installed")

    trust_handled = platform in TRUST_HANDLED_PLATFORMS
    cfg["spawn_ready_timeout"] = 0.01 if trust_handled else cfg.get("spawn_ready_timeout", 2.0)
    cfg_path.write_text(json.dumps(cfg, indent=2) + "\n", encoding="utf-8")

    skip_flag = "--no-skip-permissions" if not meta["skip_permissions"] else "--skip-permissions"
    argv = [
        "clawteam", "spawn", "tmux",
        "--team", team,
        "--agent-name", agent,
        "--workspace", "--repo", str(repo),
        "--no-keepalive",
        skip_flag,
        "--", *meta["cmd"],
    ]
    t0 = time.monotonic()
    proc = subprocess.run(argv, capture_output=True, text=True, timeout=180)
    spawn_sec = time.monotonic() - t0
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr[-800:] or proc.stdout[-800:] or "spawn failed")
    target = f"clawteam-{team}:{agent}"
    return target, spawn_sec


def _cleanup(team: str, agent: str, repo: Path | None) -> None:
    subprocess.run(
        ["clawteam", "lifecycle", "request-shutdown", "-t", team, "-n", agent, "--reason", "bench"],
        capture_output=True,
    )
    subprocess.run(["tmux", "kill-session", "-t", f"clawteam-{team}"], capture_output=True)
    subprocess.run(
        ["clawteam", "team", "cleanup", team, "--force"],
        capture_output=True,
    )
    if repo is not None:
        shutil.rmtree(repo, ignore_errors=True)


async def _bench_one(
    *,
    platform: str,
    team: str,
    repo: Path,
    cfg: dict,
    cfg_path: Path,
) -> RunResult:
    agent = platform
    target = ""
    try:
        target, _spawn_sec = _spawn(
            platform=platform, team=team, agent=agent, repo=repo, cfg=cfg, cfg_path=cfg_path,
        )
        trust_platform = resolve_trust_platform(agent_kind=platform)

        # Wait until pane is non-empty or startup/composer visible.
        pane = ""
        for _ in range(40):
            pane = await tmux_capture_pane(target, history_lines=120)
            active = _pane_active_text(pane)
            if _startup_prompt_action(active, trust_platform) or _is_composer_ready(
                pane, trust_platform=trust_platform,
            ):
                break
            await asyncio.sleep(0.15)

        active = _pane_active_text(pane)
        had_startup = _startup_prompt_action(active, trust_platform) is not None

        t0 = time.monotonic()
        result = await wait_tui_ready(
            target,
            trust_platform=trust_platform,
            timeout_sec=45.0,
            poll_interval=0.15,
        )
        wait_sec = time.monotonic() - t0
        return RunResult(
            ok=result.ok,
            wait_sec=wait_sec,
            startup_prompt=had_startup,
            reason=result.reason_code,
            error="" if result.ok else result.message,
        )
    except Exception as exc:
        return RunResult(ok=False, wait_sec=0.0, startup_prompt=False, error=str(exc))
    finally:
        if target:
            subprocess.run(
                ["clawteam", "lifecycle", "request-shutdown", "-t", team, "-n", agent, "--reason", "bench"],
                capture_output=True,
            )
            subprocess.run(["tmux", "kill-session", "-t", f"clawteam-{team}"], capture_output=True)


async def run_benchmark(*, runs: int, platforms: list[str]) -> list[PlatformStats]:
    cfg_path = Path.home() / ".clawteam" / "config.json"
    orig_cfg = cfg_path.read_text(encoding="utf-8") if cfg_path.exists() else None
    cfg = json.loads(orig_cfg) if orig_cfg else {}

    stats: list[PlatformStats] = []
    try:
        for platform in platforms:
            if platform not in _PLATFORM_SPAWN:
                continue
            if not shutil.which(_PLATFORM_SPAWN[platform]["binary"]):
                print(f"[skip] {platform}: binary missing", flush=True)
                continue

            ps = PlatformStats(
                platform=platform,
                trust_handled=platform in TRUST_HANDLED_PLATFORMS,
            )
            print(f"\n=== {platform} ({runs} runs, trust_handled={ps.trust_handled}) ===", flush=True)
            for i in range(runs):
                team = f"csflow-bench-{platform}-{int(time.time())}-{i}"
                repo = _make_repo()
                try:
                    res = await _bench_one(
                        platform=platform,
                        team=team,
                        repo=repo,
                        cfg=cfg,
                        cfg_path=cfg_path,
                    )
                    ps.runs.append(res)
                    tag = "fallback" if res.startup_prompt else "direct"
                    status = "ok" if res.ok else "FAIL"
                    print(
                        f"  #{i+1:2d} {status} wait={res.wait_sec:.2f}s mode={tag} reason={res.reason}",
                        flush=True,
                    )
                    if not res.ok:
                        print(f"       err={res.error[:120]}", flush=True)
                finally:
                    _cleanup(team, platform, repo)
            stats.append(ps)
    finally:
        if orig_cfg is not None:
            cfg_path.write_text(orig_cfg, encoding="utf-8")

    return stats


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--runs", type=int, default=20)
    parser.add_argument(
        "--platforms",
        nargs="*",
        default=list(_PLATFORM_SPAWN.keys()),
        help="Subset of platforms to benchmark",
    )
    args = parser.parse_args()

    stats = asyncio.run(run_benchmark(runs=args.runs, platforms=args.platforms))
    report = [s.summary() for s in stats]
    print("\n\n========== SUMMARY (seconds) ==========")
    print(json.dumps(report, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
