#!/usr/bin/env bash
# Install backend + test deps on GitHub-hosted runners.
#
# PyPI only publishes clawteam <=0.2.x while clawsomeflow requires >=0.3.0
# (runtime + clawteam-mcp). Mirror install-user.sh: install clawteam from
# upstream git first, pin MCP 1.x, then editable-install clawsomeflow.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
CLAWTEAM_GIT_URL="${CSFLOW_CI_CLAWTEAM_GIT_URL:-https://github.com/HKUDS/ClawTeam.git}"

say() { printf '[ci-install] %s\n' "$*"; }

say "upgrade pip"
python -m pip install --upgrade pip

if [[ -n "${CSFLOW_CI_CLAWTEAM_SOURCE:-}" ]]; then
  say "install clawteam from CSFLOW_CI_CLAWTEAM_SOURCE"
  python -m pip install --upgrade "${CSFLOW_CI_CLAWTEAM_SOURCE}"
else
  say "install clawteam from git (${CLAWTEAM_GIT_URL})"
  python -m pip install --upgrade "clawteam @ git+${CLAWTEAM_GIT_URL}"
fi

say "pin MCP Python SDK to 1.x (clawteam-mcp compatibility)"
python -m pip install --upgrade "mcp>=1.0.0,<2.0.0"

say "editable install clawsomeflow backend[dev]"
python -m pip install -e "${ROOT_DIR}/backend[dev]"

if command -v clawteam >/dev/null 2>&1; then
  clawteam runtime --help >/dev/null 2>&1 || say "warn: clawteam runtime --help failed (fast gate may still pass)"
fi

say "backend install complete"
