#!/usr/bin/env bash
# Shared isolation guards for L2/L3 gates on a host that also runs production csflow.
# Source this file from test-runtime.sh / test-perf.sh after setting TEST_* variables.

_test_guard_fail() {
  printf "\033[1;31m%s\033[0m\n" "$*" >&2
  exit 1
}

test_isolation_assert_namespace() {
  local prod_service_name="${1:?prod service name required}"
  local prod_port="${2:?prod port required}"
  local prod_board_port="${3:?prod board port required}"
  local test_service_name="${4:?test service name required}"
  local test_home="${5:?test home required}"
  local test_port="${6:?test port required}"
  local test_board_port="${7:?test board port required}"
  local test_clawteam_data_dir="${8:?test clawteam dir required}"
  local test_openclaw_home="${9:?test openclaw home required}"

  [[ "${test_service_name}" != "${prod_service_name}" ]] \
    || _test_guard_fail "test service name must differ from production"
  [[ "${test_service_name}" == csflow-test-* ]] \
    || _test_guard_fail "test service name must use csflow-test-<run-id> prefix"
  [[ "${test_port}" != "${prod_port}" ]] \
    || _test_guard_fail "test port must differ from production port (${prod_port})"
  [[ "${test_board_port}" != "${prod_board_port}" ]] \
    || _test_guard_fail "test board port must differ from production board port (${prod_board_port})"
  [[ "${test_home}" != "${HOME}/.clawsomeflow" ]] \
    || _test_guard_fail "test home must not equal production ~/.clawsomeflow"
  [[ "${test_home}" == "${HOME}/.clawsomeflow-test/"* ]] \
    || _test_guard_fail "test home must be under ~/.clawsomeflow-test/"
  [[ "${test_clawteam_data_dir}" != "${HOME}/.clawteam" ]] \
    || _test_guard_fail "test clawteam dir must not equal ~/.clawteam"
  [[ "${test_openclaw_home}" != "${HOME}/.openclaw" ]] \
    || _test_guard_fail "test openclaw home must not equal ~/.openclaw"
  [[ "${test_clawteam_data_dir}" == "${HOME}/.clawsomeflow-test/"* ]] \
    || _test_guard_fail "test clawteam dir must be under ~/.clawsomeflow-test/"
  [[ "${test_openclaw_home}" == "${HOME}/.clawsomeflow-test/"* ]] \
    || _test_guard_fail "test openclaw home must be under ~/.clawsomeflow-test/"
}

test_isolation_verify_prod_untouched() {
  local prod_health_url="${1:?prod health url required}"
  local prod_port="${2:?prod port required}"

  if ! curl -fsS "${prod_health_url}" >/dev/null 2>&1; then
    return 0
  fi

  local flows_json
  flows_json="$(
    curl -fsS \
      -H "Origin: http://127.0.0.1:${prod_port}" \
      "http://127.0.0.1:${prod_port}/api/flows" 2>/dev/null || true
  )"
  if [[ -z "${flows_json}" ]]; then
    return 0
  fi

  if printf '%s' "${flows_json}" | grep -Eq 'e2e-flow-|e2e-openclaw-|runtime-e2e-'; then
    _test_guard_fail "production service on port ${prod_port} contains e2e test artifacts"
  fi
}

test_isolation_sweep_test_namespace() {
  local base_url="${1:?test base url required}"

  if ! curl -fsS "${base_url}/" >/dev/null 2>&1; then
    return 0
  fi

  local flows_json
  flows_json="$(curl -fsS "${base_url}/api/flows" 2>/dev/null || true)"
  if [[ -z "${flows_json}" ]]; then
    return 0
  fi

  local flow_ids
  flow_ids="$(
    FLOW_JSON="${flows_json}" python3 - <<'PY'
import json
import os
import re

raw = os.environ.get("FLOW_JSON", "")
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit(0)

items = payload.get("items") or payload.get("flows") or []
if isinstance(items, dict):
    items = list(items.values())

pattern = re.compile(r"^(e2e-flow-|e2e-openclaw-|runtime-e2e-)", re.I)
for item in items:
    if not isinstance(item, dict):
        continue
    flow_id = item.get("id")
    name = str(item.get("name") or "")
    if not flow_id:
        continue
    if pattern.search(name) or pattern.search(str(flow_id)):
        print(flow_id)
PY
  )"

  if [[ -z "${flow_ids}" ]]; then
    return 0
  fi

  while IFS= read -r flow_id; do
    [[ -n "${flow_id}" ]] || continue
    local runs_json
    runs_json="$(curl -fsS "${base_url}/api/runs?flowId=${flow_id}" 2>/dev/null || true)"
    if [[ -n "${runs_json}" ]]; then
      local run_ids
      run_ids="$(
        RUN_JSON="${runs_json}" python3 - <<'PY'
import json
import os

raw = os.environ.get("RUN_JSON", "")
try:
    payload = json.loads(raw)
except json.JSONDecodeError:
    raise SystemExit(0)

items = payload.get("items") or []
for item in items:
    if isinstance(item, dict) and item.get("id"):
        print(item["id"])
PY
      )"
      while IFS= read -r run_id; do
        [[ -n "${run_id}" ]] || continue
        curl -fsS -X POST "${base_url}/api/runs/${run_id}/abort" >/dev/null 2>&1 || true
      done <<<"${run_ids}"
    fi
    curl -fsS -X DELETE "${base_url}/api/flows/${flow_id}" >/dev/null 2>&1 || true
  done <<<"${flow_ids}"
}
