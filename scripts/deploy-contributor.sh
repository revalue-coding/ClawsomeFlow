#!/usr/bin/env bash
# Contributor-oriented local source deploy with isolated runtime/data profile.
# Behavior intentionally mirrors user deployment reconcile pipeline; only
# runtime paths/ports are isolated.
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

CSFLOW_DEV_HOME="${CSFLOW_DEV_HOME:-$HOME/.clawsomeflow-dev}"
CSFLOW_DEV_VENV_DIR="${CSFLOW_DEV_VENV_DIR:-${CSFLOW_DEV_HOME}/.venv}"
CSFLOW_DEV_BACKEND_PORT="${CSFLOW_DEV_BACKEND_PORT:-17117}"
CSFLOW_DEV_FRONTEND_PORT="${CSFLOW_DEV_FRONTEND_PORT:-5174}"
CSFLOW_DEV_BOARD_PORT="${CSFLOW_DEV_BOARD_PORT:-17118}"
CSFLOW_DEV_CLAWTEAM_DATA_DIR="${CSFLOW_DEV_CLAWTEAM_DATA_DIR:-${CSFLOW_DEV_HOME}/.clawteam-data}"
CSFLOW_DEV_START_VITE="${CSFLOW_DEV_START_VITE:-1}"
CSFLOW_DEV_FRONTEND_PM="${CSFLOW_DEV_FRONTEND_PM:-npm}" # npm (default) | pnpm
CSFLOW_DEV_LOG_DIR="${CSFLOW_DEV_LOG_DIR:-${CSFLOW_DEV_HOME}/.logs}"
CSFLOW_DEV_SKIP_FRONTEND_BUILD="${CSFLOW_DEV_SKIP_FRONTEND_BUILD:-0}"
CSFLOW_DEV_DISABLE_COMMON_CRON_AUTO_SYNC="${CSFLOW_DEV_DISABLE_COMMON_CRON_AUTO_SYNC:-0}"

BACKEND_PYTHON="${CSFLOW_DEV_VENV_DIR}/bin/python"
RUNTIME_PIP="${CSFLOW_DEV_VENV_DIR}/bin/pip"
RUNTIME_CSFLOW="${CSFLOW_DEV_VENV_DIR}/bin/csflow"

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

print_usage() {
  cat <<'USAGE'
Contributor local deploy (isolated profile) for source checkouts.

Usage:
  bash scripts/deploy-contributor.sh

Default behavior:
  - Uses isolated data home: ~/.clawsomeflow-dev
  - Uses isolated runtime venv: ~/.clawsomeflow-dev/.venv
  - Runs the same runtime reconcile pipeline as user deployment (`csflow upgrade-runtime`)
  - Starts backend on 17117 and Vite on 5174
  - Uses current local source via editable install (`pip install -e backend`)
  - Does not auto-install OpenClaw

Environment overrides:
  CSFLOW_DEV_HOME                    Data home (default: ~/.clawsomeflow-dev)
  CSFLOW_DEV_VENV_DIR                Runtime venv (default: <dev-home>/.venv)
  CSFLOW_DEV_BACKEND_PORT            API/backend port (default: 17117)
  CSFLOW_DEV_FRONTEND_PORT           Vite dev port (default: 5174)
  CSFLOW_DEV_BOARD_PORT              ClawTeam board port (default: 17118)
  CSFLOW_DEV_CLAWTEAM_DATA_DIR       ClawTeam data dir (default: <dev-home>/.clawteam-data)
  CSFLOW_DEV_START_VITE              1/0 (default: 1)
  CSFLOW_DEV_FRONTEND_PM             npm/pnpm (default: npm)
  CSFLOW_DEV_LOG_DIR                 Log dir (default: <dev-home>/.logs)
  CSFLOW_DEV_SKIP_FRONTEND_BUILD     1/0 (default: 0)
  CSFLOW_DEV_DISABLE_COMMON_CRON_AUTO_SYNC 1/0 (default: 0)
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

frontend_dev_cmd() {
  if [[ "$CSFLOW_DEV_FRONTEND_PM" == "pnpm" ]]; then
    echo "exec corepack pnpm dev --host 0.0.0.0 --port ${CSFLOW_DEV_FRONTEND_PORT} --strictPort"
  else
    echo "exec npm run dev -- --host 0.0.0.0 --port ${CSFLOW_DEV_FRONTEND_PORT} --strictPort"
  fi
}

kill_port_listener() {
  local port="$1"
  if command -v fuser >/dev/null 2>&1; then
    fuser -k "${port}/tcp" >/dev/null 2>&1 || true
    return
  fi
  if command -v lsof >/dev/null 2>&1; then
    local pids=""
    pids="$(lsof -tiTCP:"${port}" -sTCP:LISTEN 2>/dev/null || true)"
    if [[ -n "${pids}" ]]; then
      kill ${pids} >/dev/null 2>&1 || true
    fi
  fi
}

ensure_runtime_bootstrapped() {
  if [[ -x "${BACKEND_PYTHON}" && -x "${RUNTIME_PIP}" && -x "${RUNTIME_CSFLOW}" ]]; then
    return
  fi

  say "bootstrapping isolated contributor runtime ..."
  CSFLOW_HOME="${CSFLOW_DEV_HOME}" \
  CSFLOW_VENV_DIR="${CSFLOW_DEV_VENV_DIR}" \
  CSFLOW_INSTALL_SHIMS=0 \
  CSFLOW_RECONCILE_DATA_DIR=0 \
  bash "${ROOT_DIR}/scripts/install_clawsomeflow.sh"
}

ensure_isolated_config() {
  (
    cd "${ROOT_DIR}/backend"
    CSFLOW_HOME="${CSFLOW_DEV_HOME}" \
    CSFLOW_DEV_BACKEND_PORT="${CSFLOW_DEV_BACKEND_PORT}" \
    CSFLOW_DEV_BOARD_PORT="${CSFLOW_DEV_BOARD_PORT}" \
    CSFLOW_DEV_CLAWTEAM_DATA_DIR="${CSFLOW_DEV_CLAWTEAM_DATA_DIR}" \
    PYTHONPATH=. "${BACKEND_PYTHON}" - <<'PY'
import os
from pathlib import Path

from app.config import load_config, save_config

api_port = int(os.environ["CSFLOW_DEV_BACKEND_PORT"])
board_port = int(os.environ["CSFLOW_DEV_BOARD_PORT"])
clawteam_data_dir = str(Path(os.environ["CSFLOW_DEV_CLAWTEAM_DATA_DIR"]).expanduser())
Path(clawteam_data_dir).mkdir(parents=True, exist_ok=True)

cfg = load_config(force_reload=True)
cfg = cfg.model_copy(
    update={
        "csflow_port": api_port,
        "clawteam_board_port": board_port,
        "clawteam_data_dir": clawteam_data_dir,
    }
)
save_config(cfg)
print(
    f"configured isolated profile: csflow={cfg.csflow_port}, "
    f"board={cfg.clawteam_board_port}, clawteam_data_dir={cfg.clawteam_data_dir}"
)
PY
  )
}

bind_current_source() {
  say "installing editable backend into contributor runtime ..."
  "${RUNTIME_PIP}" install --upgrade -e "${ROOT_DIR}/backend"

  local resolved_path
  resolved_path="$(
    "${BACKEND_PYTHON}" - <<'PY'
import pathlib
import app
print(pathlib.Path(app.__file__).resolve())
PY
  )"
  local expected_prefix="${ROOT_DIR}/backend/"
  if [[ "${resolved_path}" != "${expected_prefix}"* ]]; then
    fail "runtime import path is not bound to current source: ${resolved_path}"
  fi
  say "source binding: ${resolved_path}"
}

reconcile_runtime() {
  # Keep contributor deploy semantics aligned with user deployment reconcile
  # path; only runtime paths/ports are isolated.
  say "reconciling runtime via csflow upgrade-runtime ..."
  CSFLOW_HOME="${CSFLOW_DEV_HOME}" \
  CSFLOW_SKIP_FRONTEND_BUILD="${CSFLOW_DEV_SKIP_FRONTEND_BUILD}" \
  "${RUNTIME_CSFLOW}" upgrade-runtime --yes --no-restart-service
}

mkdir -p "${CSFLOW_DEV_LOG_DIR}"

say "contributor deploy profile:"
say "  data home: ${CSFLOW_DEV_HOME}"
say "  venv:      ${CSFLOW_DEV_VENV_DIR}"
say "  backend:   ${CSFLOW_DEV_BACKEND_PORT}"
say "  board:     ${CSFLOW_DEV_BOARD_PORT}"
say "  clawteam:  ${CSFLOW_DEV_CLAWTEAM_DATA_DIR}"
say "  vite:      ${CSFLOW_DEV_FRONTEND_PORT} (enabled=${CSFLOW_DEV_START_VITE})"

ensure_runtime_bootstrapped
ensure_isolated_config
bind_current_source
reconcile_runtime

kill_port_listener "${CSFLOW_DEV_BACKEND_PORT}"
kill_port_listener "${CSFLOW_DEV_FRONTEND_PORT}"
kill_port_listener "${CSFLOW_DEV_BOARD_PORT}"
sleep 1

nohup env PYTHONUNBUFFERED=1 CSFLOW_HOME="${CSFLOW_DEV_HOME}" bash -lc \
  "cd '${ROOT_DIR}/backend' && CSFLOW_DISABLE_COMMON_CRON_AUTO_SYNC='${CSFLOW_DEV_DISABLE_COMMON_CRON_AUTO_SYNC}' exec '${RUNTIME_CSFLOW}' serve --reload --host 0.0.0.0 --port ${CSFLOW_DEV_BACKEND_PORT}" \
  >>"${CSFLOW_DEV_LOG_DIR}/csflow-backend.log" 2>&1 </dev/null &
say "backend pid=$! (log ${CSFLOW_DEV_LOG_DIR}/csflow-backend.log)"

if [[ "${CSFLOW_DEV_START_VITE}" == "1" ]]; then
  DEV_CMD="$(frontend_dev_cmd)"
  nohup env bash -lc "cd '${ROOT_DIR}/frontend' && ${DEV_CMD}" \
    >>"${CSFLOW_DEV_LOG_DIR}/csflow-frontend-dev.log" 2>&1 </dev/null &
  say "vite pid=$! (log ${CSFLOW_DEV_LOG_DIR}/csflow-frontend-dev.log)"
else
  say "vite skipped (CSFLOW_DEV_START_VITE=0)"
fi

say "contributor deploy complete."
say "  API/UI: http://127.0.0.1:${CSFLOW_DEV_BACKEND_PORT}"
if [[ "${CSFLOW_DEV_START_VITE}" == "1" ]]; then
  say "  Vite:   http://127.0.0.1:${CSFLOW_DEV_FRONTEND_PORT}"
fi
say "  logs:   ${CSFLOW_DEV_LOG_DIR}"
