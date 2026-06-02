#!/usr/bin/env bash
# Weekly performance gate on isolated deployment namespace.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"

PYTHON_BIN="${PYTHON_BIN:-python3.11}"
PROD_SERVICE_NAME="${CSFLOW_PROD_SERVICE_NAME:-csflow}"
PROD_HEALTH_URL="${CSFLOW_PROD_HEALTH_URL:-http://127.0.0.1:17017/}"
PROD_PORT="${CSFLOW_PROD_PORT:-17017}"
PROD_BOARD_PORT="${CSFLOW_PROD_BOARD_PORT:-17018}"

TEST_RUN_ID="${TEST_RUN_ID:-perf-$(date -u +%Y%m%d%H%M%S)-$("${PYTHON_BIN}" -c 'import secrets; print(secrets.token_hex(3))')}"
TEST_SERVICE_NAME="${CSFLOW_TEST_SERVICE_NAME:-csflow-test-${TEST_RUN_ID}}"
TEST_HOME="${CSFLOW_TEST_HOME:-${HOME}/.clawsomeflow-test/${TEST_RUN_ID}}"
TEST_UNIT_DIR="${CSFLOW_TEST_UNIT_DIR:-${HOME}/.config/systemd/user}"
TEST_PORT="${CSFLOW_TEST_PORT:-28117}"
TEST_BOARD_PORT="${CSFLOW_TEST_BOARD_PORT:-28118}"
TEST_BASE_URL="http://127.0.0.1:${TEST_PORT}"
TEST_CLAWTEAM_DATA_DIR="${CSFLOW_TEST_CLAWTEAM_DATA_DIR:-${TEST_HOME}/.clawteam}"
TEST_OPENCLAW_HOME="${CSFLOW_TEST_OPENCLAW_HOME:-${TEST_HOME}/.openclaw}"
TEST_OPENCLAW_GATEWAY_URL="${CSFLOW_TEST_OPENCLAW_GATEWAY_URL:-http://127.0.0.1:28789}"
TEST_VENV="${CSFLOW_TEST_VENV:-${TEST_HOME}/.venv}"

LOCK_FILE="${CSFLOW_TEST_LOCK_FILE:-${HOME}/.clawsomeflow-test/.runtime-test.lock}"
CLEANED_UP=0

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

require_cmd() {
  command -v "$1" >/dev/null || fail "missing required command: $1"
}

ensure_test_clawteam_runtime() {
  if "${TEST_VENV}/bin/clawteam" runtime --help >/dev/null 2>&1; then
    return
  fi

  local source_spec="${CSFLOW_TEST_CLAWTEAM_SOURCE:-}"

  if [[ -n "${source_spec}" ]]; then
    say "[perf] install clawteam runtime from source: ${source_spec}"
    if [[ -d "${source_spec}" ]]; then
      [[ -f "${source_spec}/pyproject.toml" ]] || fail "invalid clawteam source dir: ${source_spec}"
      "${TEST_PYTHON}" -m pip install --upgrade -e "${source_spec}"
    else
      "${TEST_PYTHON}" -m pip install --upgrade "${source_spec}"
    fi
  else
    say "[perf] install clawteam runtime from upstream git"
    "${TEST_PYTHON}" -m pip install --upgrade \
      "clawteam @ git+https://github.com/HKUDS/ClawTeam.git"
  fi

  "${TEST_VENV}/bin/clawteam" runtime --help >/dev/null 2>&1 || \
    fail "clawteam runtime command unavailable in test venv"
}

check_prod_health() {
  local stage="$1"
  if [[ "$(systemctl --user is-active "${PROD_SERVICE_NAME}" 2>/dev/null || true)" != "active" ]]; then
    fail "production service '${PROD_SERVICE_NAME}' is not active (${stage})"
  fi
  curl -fsS "${PROD_HEALTH_URL}" >/dev/null || fail "production healthcheck failed (${stage})"
}

check_test_health() {
  if [[ "$(systemctl --user is-active "${TEST_SERVICE_NAME}" 2>/dev/null || true)" != "active" ]]; then
    fail "test service '${TEST_SERVICE_NAME}' is not active"
  fi
  local i
  for i in 1 2 3 4 5 6 7 8 9 10; do
    if curl -fsS "${TEST_BASE_URL}/" >/dev/null 2>&1; then
      return
    fi
    sleep 1
  done
  fail "test healthcheck failed"
}

cleanup() {
  set +e
  if [[ "${CLEANED_UP}" == "1" ]]; then
    return
  fi
  CLEANED_UP=1
  if [[ "${TEST_SERVICE_NAME}" == "${PROD_SERVICE_NAME}" ]]; then
    return
  fi
  systemctl --user stop "${TEST_SERVICE_NAME}" >/dev/null 2>&1 || true
  systemctl --user disable "${TEST_SERVICE_NAME}" >/dev/null 2>&1 || true
  rm -f "${TEST_UNIT_DIR}/${TEST_SERVICE_NAME}.service"
  systemctl --user daemon-reload >/dev/null 2>&1 || true
  rm -rf "${TEST_HOME}"

  # Safety net: cleanup must never leave production unavailable.
  if [[ "${PROD_SERVICE_NAME}" != "${TEST_SERVICE_NAME}" ]]; then
    if [[ "$(systemctl --user is-active "${PROD_SERVICE_NAME}" 2>/dev/null || true)" != "active" ]]; then
      warn "[perf] production service '${PROD_SERVICE_NAME}' became inactive during cleanup; attempting recovery"
      systemctl --user start "${PROD_SERVICE_NAME}" >/dev/null 2>&1 || true
    fi
    local i
    for i in 1 2 3 4 5; do
      if curl -fsS "${PROD_HEALTH_URL}" >/dev/null 2>&1; then
        break
      fi
      sleep 1
    done
  fi
}

require_cmd "${PYTHON_BIN}"
require_cmd systemctl
require_cmd curl
require_cmd flock

[[ "${TEST_SERVICE_NAME}" != "${PROD_SERVICE_NAME}" ]] || fail "test service name must differ from production"
[[ "${TEST_PORT}" != "${PROD_PORT}" ]] || fail "test port must differ from production port"
[[ "${TEST_BOARD_PORT}" != "${PROD_BOARD_PORT}" ]] || fail "test board port must differ from production board port"
[[ "${TEST_HOME}" == "${HOME}/.clawsomeflow-test/"* ]] || fail "test home must be under ~/.clawsomeflow-test/"
[[ "${TEST_CLAWTEAM_DATA_DIR}" != "${HOME}/.clawteam" ]] || fail "test clawteam dir must not equal ~/.clawteam"
[[ "${TEST_OPENCLAW_HOME}" != "${HOME}/.openclaw" ]] || fail "test openclaw home must not equal ~/.openclaw"
[[ "${TEST_CLAWTEAM_DATA_DIR}" == "${HOME}/.clawsomeflow-test/"* ]] || fail "test clawteam dir must be under ~/.clawsomeflow-test/"
[[ "${TEST_OPENCLAW_HOME}" == "${HOME}/.clawsomeflow-test/"* ]] || fail "test openclaw home must be under ~/.clawsomeflow-test/"

mkdir -p "$(dirname "${LOCK_FILE}")"
exec 9>"${LOCK_FILE}"
if ! flock -n 9; then
  fail "another runtime/perf test task is running; refusing parallel execution"
fi

trap cleanup EXIT INT TERM

check_prod_health "before_perf_tests"

say "[perf] install backend test dependencies"
mkdir -p "${TEST_HOME}"
"${PYTHON_BIN}" -m venv "${TEST_VENV}"
TEST_PYTHON="${TEST_VENV}/bin/python"
TEST_CSFLOW="${TEST_VENV}/bin/csflow"
"${TEST_PYTHON}" -m pip install --upgrade pip >/dev/null
"${TEST_PYTHON}" -m pip install --upgrade -e "${ROOT_DIR}/backend[dev]"
ensure_test_clawteam_runtime

mkdir -p "${TEST_UNIT_DIR}"
export CSFLOW_HOME="${TEST_HOME}"
export CSFLOW_SERVICE_NAME="${TEST_SERVICE_NAME}"
export CSFLOW_SYSTEMD_USER_DIR="${TEST_UNIT_DIR}"
export CSFLOW_SERVICE_SLICE="${CSFLOW_TEST_SLICE:-csflow-test.slice}"
export CSFLOW_SERVICE_CPU_QUOTA="${CSFLOW_TEST_CPU_QUOTA:-30%}"
export CSFLOW_SERVICE_MEMORY_MAX="${CSFLOW_TEST_MEMORY_MAX:-2G}"
export CSFLOW_DISABLE_BOARD=1
export CLAWTEAM_DATA_DIR="${TEST_CLAWTEAM_DATA_DIR}"
export CLAWTEAM_USER="csflow-test-${TEST_RUN_ID}"
export OPENCLAW_HOME="${TEST_OPENCLAW_HOME}"
export CSFLOW_TEST_OPENCLAW_GATEWAY_URL="${TEST_OPENCLAW_GATEWAY_URL}"

export CSFLOW_PERF_TEST_ACTIVE=1
export CSFLOW_PERF_BASE_URL="${TEST_BASE_URL}"
export CSFLOW_PERF_SAMPLE_COUNT="${CSFLOW_PERF_SAMPLE_COUNT:-40}"
export CSFLOW_PERF_MAX_P95_MS="${CSFLOW_PERF_MAX_P95_MS:-1200}"

resolved_service_name="$("${TEST_PYTHON}" - <<'PY'
from app.cli._user_service import service_name
print(service_name())
PY
)"
if [[ "${resolved_service_name}" != "${TEST_SERVICE_NAME}" ]]; then
  fail "service override check failed (${resolved_service_name} != ${TEST_SERVICE_NAME})"
fi

say "[perf] test namespace"
say "  service=${TEST_SERVICE_NAME} home=${TEST_HOME} port=${TEST_PORT} board=${TEST_BOARD_PORT}"
say "  clawteam_data=${TEST_CLAWTEAM_DATA_DIR} openclaw_home=${TEST_OPENCLAW_HOME}"

say "[perf] bootstrap isolated data home"
"${TEST_CSFLOW}" init --skip-openclaw --no-restart-service --port "${TEST_PORT}" --board-port "${TEST_BOARD_PORT}"

"${TEST_PYTHON}" - <<'PY'
from __future__ import annotations

import json
import os
from pathlib import Path

cfg_path = Path(os.environ["CSFLOW_HOME"]) / "config.json"
cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
cfg["clawteam_data_dir"] = os.environ["CLAWTEAM_DATA_DIR"]
cfg["openclaw_home"] = os.environ["OPENCLAW_HOME"]
cfg["openclaw_gateway_url"] = os.environ.get(
    "CSFLOW_TEST_OPENCLAW_GATEWAY_URL",
    "http://127.0.0.1:28789",
)
cfg_path.write_text(
    json.dumps(cfg, ensure_ascii=False, indent=2) + "\n",
    encoding="utf-8",
)
PY

say "[perf] boot isolated csflow test service"
"${TEST_CSFLOW}" start --yes --skip-deps --port "${TEST_PORT}" --board-port "${TEST_BOARD_PORT}"
check_test_health
check_prod_health "after_test_boot"

say "[perf] run tests/perf"
"${TEST_PYTHON}" -m pytest -q "${ROOT_DIR}/tests/perf" -m perf

check_prod_health "after_perf_tests"
say "[perf] gate passed"
