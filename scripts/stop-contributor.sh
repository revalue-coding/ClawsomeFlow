#!/usr/bin/env bash
# Stop the contributor local deploy (isolated profile) started by
# scripts/deploy-contributor.sh.
#
# STRICTLY scoped to the contributor profile (dev ports + dev home). It NEVER
# touches the end-user service (port 17017 / ~/.clawsomeflow / systemd csflow).
# To stop the end-user service, use `csflow stop` instead.
set -euo pipefail

CSFLOW_DEV_HOME="${CSFLOW_DEV_HOME:-$HOME/.clawsomeflow-dev}"
CSFLOW_DEV_BACKEND_PORT="${CSFLOW_DEV_BACKEND_PORT:-17117}"
CSFLOW_DEV_FRONTEND_PORT="${CSFLOW_DEV_FRONTEND_PORT:-5174}"
CSFLOW_DEV_BOARD_PORT="${CSFLOW_DEV_BOARD_PORT:-17118}"
# End-user service port — used only as a safety guard; never acted upon here.
CSFLOW_USER_PORT="${CSFLOW_PORT:-17017}"

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

print_usage() {
  cat <<'USAGE'
Stop the contributor local deploy (isolated profile).

Usage:
  bash scripts/stop-contributor.sh

Default behavior:
  - Stops listeners ONLY on the contributor dev ports:
      backend 17117, vite 5174, board 17118
  - Never touches the end-user service (port 17017 / ~/.clawsomeflow / systemd).

Environment overrides (must match your deploy-contributor.sh profile):
  CSFLOW_DEV_HOME            Data home (default: ~/.clawsomeflow-dev)
  CSFLOW_DEV_BACKEND_PORT    API/backend port (default: 17117)
  CSFLOW_DEV_FRONTEND_PORT   Vite dev port (default: 5174)
  CSFLOW_DEV_BOARD_PORT      ClawTeam board port (default: 17118)
USAGE
}

case "${1:-}" in
  -h|--help|help)
    print_usage
    exit 0
    ;;
  "")
    ;;
  *)
    fail "Unknown argument: $1 (use --help for usage)"
    ;;
esac

# Safety rail: refuse to act if a configured dev port collides with the
# end-user service port — we must never stop the user's real service.
for port in "${CSFLOW_DEV_BACKEND_PORT}" "${CSFLOW_DEV_FRONTEND_PORT}" "${CSFLOW_DEV_BOARD_PORT}"; do
  if [[ "${port}" == "${CSFLOW_USER_PORT}" ]]; then
    fail "Refusing to stop: contributor port ${port} collides with end-user port ${CSFLOW_USER_PORT}."
  fi
done

kill_port_listener() {
  local port="$1"
  local pids=""
  if command -v fuser >/dev/null 2>&1; then
    if fuser -k "${port}/tcp" >/dev/null 2>&1; then
      say "  ✓ stopped listener on :${port}"
    else
      warn "  - no listener on :${port}"
    fi
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      kill ${pids} >/dev/null 2>&1 || true
      say "  ✓ stopped listener on :${port} (pids: ${pids//$'\n'/ })"
    else
      warn "  - no listener on :${port}"
    fi
    return
  fi
  warn "  - neither fuser nor lsof available; cannot stop :${port}"
}

say "stopping contributor profile (home=${CSFLOW_DEV_HOME}):"
kill_port_listener "${CSFLOW_DEV_BACKEND_PORT}"
kill_port_listener "${CSFLOW_DEV_FRONTEND_PORT}"
kill_port_listener "${CSFLOW_DEV_BOARD_PORT}"
say "contributor profile stopped (end-user service on :${CSFLOW_USER_PORT} untouched)."
