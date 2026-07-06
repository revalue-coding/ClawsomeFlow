#!/usr/bin/env bash
# ClawsomeFlow end-user upgrader (stable by default, optional --pre).
#
# Designed to be executed directly or via remote pipe:
#   curl -fsSL https://clawsomeflow.com/upgrade.sh | bash
#
# Unlike the installer, this script ONLY upgrades an existing deployment:
#   1) Upgrades the clawsomeflow package inside the managed runtime venv
#      (~/.clawsomeflow/.venv) — never touches system Python, so it is immune
#      to "externally-managed-environment".
#      - default: latest stable release
#      - with --pre: latest pre-release (beta/rc) allowed by PyPI
#   2) Runs `csflow upgrade-runtime` to sync the data dir and restart the service.
#
# If no existing install is found, it points the user at install.sh instead.

set -euo pipefail

USE_PRE=0
PYPI_INDEX_URL="${CSFLOW_PYPI_INDEX_URL:-https://pypi.org/simple}"
CSFLOW_HOME="${CSFLOW_HOME:-$HOME/.clawsomeflow}"
VENV_DIR="${CSFLOW_VENV_DIR:-${CSFLOW_HOME}/.venv}"
VENV_BIN="${VENV_DIR}/bin"

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

resolve_latest_stable_version() {
  local json_url="${CSFLOW_PYPI_JSON_URL:-}"
  if [[ -z "${json_url}" ]]; then
    local normalized_index="${PYPI_INDEX_URL%/}"
    if [[ "${normalized_index}" != "https://pypi.org/simple" ]]; then
      return 1
    fi
    json_url="https://pypi.org/pypi/clawsomeflow/json"
  fi

  "${VENV_BIN}/python" - "${json_url}" <<'PY'
import json
import sys
import urllib.request

url = sys.argv[1]

def stable_key(version: str):
    parts = version.split(".")
    if len(parts) != 3 or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)

try:
    with urllib.request.urlopen(url, timeout=5) as resp:
        payload = json.load(resp)
except Exception:
    print("")
    raise SystemExit(0)

releases = payload.get("releases") if isinstance(payload, dict) else None
if not isinstance(releases, dict):
    print("")
    raise SystemExit(0)

best_version = ""
best_key = None
for version in releases:
    if not isinstance(version, str):
        continue
    key = stable_key(version)
    if key is None:
        continue
    if best_key is None or key > best_key:
        best_key = key
        best_version = version

print(best_version)
PY
}

install_latest_stable_clawsomeflow() {
  local stable_version=""
  stable_version="$(resolve_latest_stable_version || true)"
  if [[ -n "${stable_version}" ]]; then
    say "  -> target latest stable: ${stable_version}"
    if _try_install_pinned_stable_quietly "${stable_version}"; then
      return 0
    fi
    warn "Pinned stable artifact is not available yet on current index mirror; retrying standard stable upgrade."
  fi

  "${VENV_BIN}/pip" install --upgrade --index-url "${PYPI_INDEX_URL}" clawsomeflow
}

_try_install_pinned_stable_quietly() {
  local stable_version="$1"
  local pip_output
  pip_output="$(mktemp)"
  if "${VENV_BIN}/pip" install --upgrade --index-url "${PYPI_INDEX_URL}" \
    "clawsomeflow==${stable_version}" >"${pip_output}" 2>&1; then
    rm -f "${pip_output}"
    return 0
  fi
  if [[ "${CSFLOW_INSTALL_DEBUG:-0}" == "1" ]]; then
    warn "Pinned install debug output:"
    while IFS= read -r line; do
      warn "  ${line}"
    done < "${pip_output}"
  fi
  rm -f "${pip_output}"
  return 1
}

for arg in "$@"; do
  case "$arg" in
    --pre) USE_PRE=1 ;;
    -h|--help)
      cat <<'USAGE'
ClawsomeFlow end-user upgrader (stable by default, optional --pre).

Usage:
  ./upgrade-user.sh [--pre]

Result:
  - Upgrades clawsomeflow in ~/.clawsomeflow/.venv.
    * default: latest stable release
    * with --pre: latest pre-release (beta/rc)
  - Runs `csflow upgrade-runtime --restart-service` to migrate data + restart.

Upgrade CLI:
  - Stable:      curl -fsSL https://clawsomeflow.com/upgrade.sh | bash
  - Pre-release: curl -fsSL https://clawsomeflow.com/upgrade.sh | bash -s -- --pre

PEP 668 safety:
  - This script only invokes ~/.clawsomeflow/.venv/bin/pip (never system pip),
    so it avoids Linux "error: externally-managed-environment".

Requires an existing install; run install.sh first otherwise.

Environment overrides:
  CSFLOW_PYPI_INDEX_URL   Package index (default: https://pypi.org/simple)
  CSFLOW_VENV_DIR         Runtime venv path (default: ~/.clawsomeflow/.venv)
USAGE
      exit 0
      ;;
    *)
      fail "Unknown argument: ${arg}"
      ;;
  esac
done

if [[ ! -x "${VENV_BIN}/pip" || ! -x "${VENV_BIN}/csflow" ]]; then
  fail "No existing ClawsomeFlow runtime found at ${VENV_DIR}.
Run the installer instead:
  curl -fsSL https://clawsomeflow.com/install.sh | bash"
fi

if [[ "${USE_PRE}" == "1" ]]; then
  say "[1/4] Upgrading clawsomeflow package (pre-release channel)"
  "${VENV_BIN}/pip" install --upgrade --index-url "${PYPI_INDEX_URL}" --pre clawsomeflow \
    || fail "Failed to upgrade clawsomeflow from PyPI (--pre)."
else
  say "[1/4] Upgrading clawsomeflow package (stable channel)"
  install_latest_stable_clawsomeflow \
    || fail "Failed to upgrade clawsomeflow from PyPI."
fi

installed_version="$("${VENV_BIN}/csflow" version 2>/dev/null || true)"
[[ -n "${installed_version}" ]] || fail "Failed to detect installed clawsomeflow version."
say "  ✓ Upgraded clawsomeflow package to: ${installed_version}"

# The pip upgrade may re-resolve dependencies (notably `mcp` with --pre) or
# disturb the clawteam stack; re-verify like the installer does.
say "[2/4] Verifying runtime stack (MCP pin + clawteam)"
"${VENV_BIN}/pip" install --upgrade 'mcp>=1.0.0,<2.0.0' >/dev/null \
  || fail "Failed to pin MCP Python SDK to 1.x (clawteam-mcp compatibility)."
if ! { [[ -x "${VENV_BIN}/clawteam" ]] \
    && "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1 \
    && [[ -x "${VENV_BIN}/clawteam-mcp" ]]; }; then
  fail "clawteam stack is broken after the package upgrade.
Repair it by re-running the installer (safe: existing data is upgraded in place):
  curl -fsSL https://clawsomeflow.com/install.sh | bash"
fi
say "  ✓ mcp 1.x + clawteam runtime + clawteam-mcp verified"

say "[3/4] Syncing data directory and restarting service"
"${VENV_BIN}/csflow" upgrade-runtime --restart-service || fail "csflow upgrade-runtime failed"

say "[4/4] Verifying service health"
csflow_port="${CSFLOW_PORT:-}"
if [[ -z "${csflow_port}" ]]; then
  csflow_port="$("${VENV_BIN}/python" - <<'PY' 2>/dev/null || echo 17017
from app import config
print(config.load_config().csflow_port)
PY
)"
fi
csflow_port="${csflow_port:-17017}"
health_url="http://127.0.0.1:${csflow_port}/health"
health_ok=0
for ((i = 1; i <= 60; i++)); do
  if curl -fsS "${health_url}" >/dev/null 2>&1; then
    health_ok=1
    break
  fi
  sleep 1
done
if [[ "${health_ok}" == "1" ]]; then
  say "✅ Upgrade complete → ${installed_version} (service healthy: ${health_url})"
else
  warn "Upgrade finished (${installed_version}) but the health check did not pass within 60s: ${health_url}"
  warn "Inspect with: ${VENV_BIN}/csflow doctor   /   journalctl --user -u csflow -n 50 --no-pager"
fi
