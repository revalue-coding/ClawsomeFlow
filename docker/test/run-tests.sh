#!/usr/bin/env bash
# Container entrypoint: run the pytest suite inside the isolated image.
#
# Isolation guarantees (by construction, no app-code changes needed):
#   * A throwaway $HOME inside the container — conftest further redirects
#     CSFLOW_HOME to a per-test tmp dir, so no real ~/.clawsomeflow is involved.
#   * The container has its own network namespace, so 127.0.0.1:18789 is the
#     container's own loopback — the host's live OpenClaw gateway is unreachable.
#   * The host filesystem is not mounted, so ~/.openclaw / ~/.clawteam are absent.
#
# With no args it runs the L1 backend gate (backend/tests). The repo-root tests/
# dir (L2/L3 runtime/e2e, gated by CSFLOW_*_TEST_ACTIVE) is intentionally NOT
# loaded by default: it shares the package name `tests` with backend/tests, so
# loading both conftests in one invocation collides under importlib mode. Pass
# explicit paths to run anything else, e.g.:
#   docker run --rm csflow-test:latest -q backend/tests/test_api_guard.py
set -uo pipefail

export HOME=/tmp/testhome
mkdir -p "$HOME"

cd /app
if [ "$#" -eq 0 ]; then
  exec python -m pytest -q backend/tests
fi
exec python -m pytest "$@"
