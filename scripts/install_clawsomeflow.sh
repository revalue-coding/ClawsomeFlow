#!/usr/bin/env bash
# Install clawsomeflow into an isolated user venv.
#
# Default target:
#   ~/.clawsomeflow/.venv
#
# Usage:
#   scripts/install_clawsomeflow.sh
#   scripts/install_clawsomeflow.sh --pre
#
# Default behavior installs the latest stable release from PyPI.

set -euo pipefail

USE_PRE=0
PYTHON_BIN="${PYTHON_BIN:-python3.11}"
OS_NAME="$(uname -s)"
IS_MACOS=0
if [[ "${OS_NAME}" == "Darwin" ]]; then
  IS_MACOS=1
fi
CSFLOW_HOME="${CSFLOW_HOME:-$HOME/.clawsomeflow}"
VENV_DIR="${CSFLOW_VENV_DIR:-${CSFLOW_HOME}/.venv}"
PYPI_INDEX_URL="${CSFLOW_PYPI_INDEX_URL:-https://pypi.org/simple}"
INSTALL_SHIMS="${CSFLOW_INSTALL_SHIMS:-1}"
RECONCILE_DATA_DIR="${CSFLOW_RECONCILE_DATA_DIR:-1}"
LOCAL_CLAWTEAM_SOURCE="${HOME}/ClawTeam"
EXISTING_DEPLOYMENT=0

for arg in "$@"; do
  case "$arg" in
    --pre) USE_PRE=1 ;;
    -h|--help)
      cat <<'USAGE'
Install ClawsomeFlow into an isolated user virtualenv.

Usage:
  scripts/install_clawsomeflow.sh [--pre] [--no-reconcile]

Notes:
  - OpenClaw / Claude / Codex / Cursor / Hermes runtimes are optional and are not auto-installed.
  - Reconcile flow does not auto-restore removed OpenClaw registrations.

Environment overrides:
  PYTHON_BIN        Python interpreter (default: python3.11)
  CSFLOW_VENV_DIR   Target virtualenv path (default: ~/.clawsomeflow/.venv)
  CSFLOW_PYPI_INDEX_URL  Package index (default: https://pypi.org/simple)
  CSFLOW_RECONCILE_DATA_DIR  1/0 to enable install/upgrade reconciliation
USAGE
      exit 0
      ;;
    --no-reconcile) RECONCILE_DATA_DIR=0 ;;
    *)
      echo "Unknown argument: $arg" >&2
      exit 1
      ;;
  esac
done

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      PYTHON_BIN="python3"
    fi
  fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if [[ "${IS_MACOS}" == "1" ]]; then
    command -v brew >/dev/null 2>&1 || {
      echo "Missing Python 3.11+ and Homebrew is unavailable on macOS." >&2
      exit 1
    }
    brew install python@3.11 >/dev/null || {
      echo "Failed to install python@3.11 via Homebrew." >&2
      exit 1
    }
  else
    echo "Missing ${PYTHON_BIN}. Install Python 3.11+ first." >&2
    exit 1
  fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      PYTHON_BIN="python3"
    fi
  fi
fi

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Python 3.11+ is still unavailable after setup attempts." >&2
  exit 1
fi

ensure_local_bin_in_path() {
  mkdir -p "${HOME}/.local/bin"
  case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) export PATH="${HOME}/.local/bin:${PATH}" ;;
  esac
}

clawteam_stack_ready() {
  [[ -x "$VENV_DIR/bin/clawteam" ]] \
    && "$VENV_DIR/bin/clawteam" runtime --help >/dev/null 2>&1 \
    && [[ -x "$VENV_DIR/bin/clawteam-mcp" ]]
}

install_clawteam_stack() {
  local pin_mcp="${1:-1}"
  if clawteam_stack_ready; then
    if [[ "${pin_mcp}" == "1" ]]; then
      ensure_mcp_sdk_compatible || return 1
    fi
    return 0
  fi

  if [[ -n "${CSFLOW_CLAWTEAM_SOURCE:-}" ]]; then
    "$VENV_DIR/bin/pip" install --upgrade "${CSFLOW_CLAWTEAM_SOURCE}" || return 1
  elif [[ -f "${LOCAL_CLAWTEAM_SOURCE}/pyproject.toml" ]]; then
    "$VENV_DIR/bin/pip" install --upgrade "${LOCAL_CLAWTEAM_SOURCE}" || return 1
  else
    "$VENV_DIR/bin/pip" install --upgrade --index-url "$PYPI_INDEX_URL" clawteam || true
  fi

  if ! clawteam_stack_ready; then
    tmp_dir="$(mktemp -d)"
    if ! git clone --depth 1 https://github.com/HKUDS/ClawTeam.git "${tmp_dir}/ClawTeam"; then
      rm -rf "${tmp_dir}"
      echo "clawteam git clone failed." >&2
      return 1
    fi
    if ! "$VENV_DIR/bin/pip" install --upgrade "${tmp_dir}/ClawTeam"; then
      rm -rf "${tmp_dir}"
      echo "clawteam install from cloned source failed." >&2
      return 1
    fi
    rm -rf "${tmp_dir}"
  fi

  "$VENV_DIR/bin/clawteam" runtime --help >/dev/null 2>&1 || return 1
  if [[ "${pin_mcp}" == "1" ]]; then
    ensure_mcp_sdk_compatible || return 1
  fi
  [[ -x "$VENV_DIR/bin/clawteam-mcp" ]] || return 1
}

ensure_mcp_sdk_compatible() {
  "$VENV_DIR/bin/pip" install --upgrade 'mcp>=1.0.0,<2.0.0' || return 1
  local mcp_ver=""
  mcp_ver="$("$VENV_DIR/bin/pip" show mcp 2>/dev/null | sed -n 's/^Version: //p' | head -1)"
  if [[ -n "${mcp_ver}" ]]; then
    local major="${mcp_ver%%.*}"
    if [[ "${major}" =~ ^[0-9]+$ ]] && (( major >= 2 )); then
      return 1
    fi
    return 0
  fi
  if ! "$VENV_DIR/bin/python" - <<'PY'
import importlib.metadata as md
import re
import sys

try:
    raw = md.version("mcp")
except md.PackageNotFoundError:
    raise SystemExit(1)
major_match = re.search(r"(\d+)", raw)
major = int(major_match.group(1)) if major_match else -1
if major >= 2:
    raise SystemExit(1)
PY
  then
    return 1
  fi
}

probe_existing_deployment() {
  if [[ -d "${CSFLOW_HOME}" ]]; then
    EXISTING_DEPLOYMENT=1
  else
    EXISTING_DEPLOYMENT=0
  fi
}

reconcile_data_dir() {
  if [[ "${RECONCILE_DATA_DIR}" != "1" ]]; then
    return
  fi
  if [[ "${EXISTING_DEPLOYMENT}" == "1" ]]; then
    "$VENV_DIR/bin/csflow" upgrade-runtime --yes --no-restart-service
  else
    "$VENV_DIR/bin/csflow" install --yes --no-restart-service
  fi
}

cursor_bootstrap_hint() {
  local mac_cursor_bin="/Applications/Cursor.app/Contents/Resources/app/bin/cursor"
  if command -v cursor >/dev/null 2>&1; then
    echo "run \`cursor agent --help\` once, then ensure ~/.local/bin is in PATH"
    return
  fi
  if [[ -x "${mac_cursor_bin}" ]]; then
    echo "run \`${mac_cursor_bin} agent --help\` once, then ensure ~/.local/bin is in PATH"
    return
  fi
  echo "install Cursor CLI, run \`cursor agent --help\` once, and ensure ~/.local/bin is in PATH (https://cursor.com/docs/cli/installation)"
}

probe_cli_runtime() {
  local install_hint="$1"
  shift
  local cmd=""
  local resolved=""
  local candidate
  for candidate in "$@"; do
    if resolved="$(command -v "$candidate" 2>/dev/null)"; then
      cmd="$candidate"
      break
    fi
  done

  if [[ -z "${cmd}" ]]; then
    echo "Unavailable — ${install_hint}"
    return
  fi

  local version_output=""
  version_output="$(
    "$cmd" --version 2>/dev/null \
      || "$cmd" version 2>/dev/null \
      || "$cmd" -v 2>/dev/null \
      || true
  )"
  if [[ -n "${version_output}" ]]; then
    echo "${version_output}"
    return
  fi
  if "$cmd" --help >/dev/null 2>&1; then
    echo "Reachable (${resolved}; version output unavailable)"
    return
  fi
  echo "Unavailable — found ${resolved}, but command probe failed. ${install_hint}"
}

print_agent_platform_summary() {
  local openclaw_status=""
  local claude_status=""
  local codex_status=""
  local cursor_status=""
  local hermes_status=""

  openclaw_status="$(probe_cli_runtime "install OpenClaw CLI and verify with \`openclaw --version\`" openclaw)"
  claude_status="$(probe_cli_runtime "install Claude Code CLI and verify with \`claude --version\`" claude)"
  codex_status="$(probe_cli_runtime "install Codex CLI and verify with \`codex --version\`" codex)"
  cursor_status="$(probe_cli_runtime "$(cursor_bootstrap_hint)" agent)"
  hermes_status="$(probe_cli_runtime "install Hermes CLI (\`pip install -U hermes-agent\`) and verify with \`hermes --version\`" hermes)"

  echo "Agent runtime capability summary:"
  echo "  - OpenClaw: ${openclaw_status}"
  echo "  - Claude Code: ${claude_status}"
  echo "  - Codex: ${codex_status}"
  echo "  - Cursor: ${cursor_status}"
  echo "  - Hermes: ${hermes_status}"
  echo "  - ClawTeam runtime: Ready (required)"
  echo "  Note: deployment/upgrade does not auto-restore removed OpenClaw registrations; use \"Restore Agent\" in UI when needed."
}

resolve_latest_stable_version() {
  local json_url="${CSFLOW_PYPI_JSON_URL:-}"
  if [[ -z "${json_url}" ]]; then
    local normalized_index="${PYPI_INDEX_URL%/}"
    if [[ "${normalized_index}" != "https://pypi.org/simple" ]]; then
      return 1
    fi
    json_url="https://pypi.org/pypi/clawsomeflow/json"
  fi

  "$VENV_DIR/bin/python" - "${json_url}" <<'PY'
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

try_install_pinned_stable_quietly() {
  local stable_version="$1"
  local pip_output
  pip_output="$(mktemp)"
  if "$VENV_DIR/bin/pip" install --upgrade --index-url "$PYPI_INDEX_URL" \
    "clawsomeflow==${stable_version}" >"${pip_output}" 2>&1; then
    rm -f "${pip_output}"
    return 0
  fi
  if [[ "${CSFLOW_INSTALL_DEBUG:-0}" == "1" ]]; then
    echo "Pinned install debug output:" >&2
    while IFS= read -r line; do
      echo "  ${line}" >&2
    done < "${pip_output}"
  fi
  rm -f "${pip_output}"
  return 1
}

ensure_os_packages() {
  mkdir -p "${HOME}/.local/bin"
  if command -v git >/dev/null 2>&1 && command -v tmux >/dev/null 2>&1; then
    return
  fi
  if [[ "${IS_MACOS}" == "1" ]] && command -v brew >/dev/null 2>&1; then
    brew install git tmux
    return
  fi
  if command -v apt-get >/dev/null 2>&1; then
    sudo apt-get install -y git tmux
    return
  fi
  echo "Warning: git and tmux are required but could not be auto-installed." >&2
}

probe_existing_deployment
ensure_os_packages
mkdir -p "$(dirname "$VENV_DIR")"
if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  "$PYTHON_BIN" -m venv "$VENV_DIR"
fi
"$VENV_DIR/bin/python" -m pip install --upgrade pip

if ! install_clawteam_stack 0; then
  echo "Failed to install clawteam ≥0.3 (runtime + clawteam-mcp)." >&2
  exit 1
fi

if [[ "$USE_PRE" == "1" ]]; then
  "$VENV_DIR/bin/pip" install --upgrade --index-url "$PYPI_INDEX_URL" --pre clawsomeflow
else
  stable_version="$(resolve_latest_stable_version || true)"
  if [[ -n "${stable_version}" ]]; then
    echo "Installing latest stable clawsomeflow: ${stable_version}"
    if ! try_install_pinned_stable_quietly "${stable_version}"; then
      echo "Pinned stable artifact is not available yet on current index mirror; retrying standard stable upgrade." >&2
      "$VENV_DIR/bin/pip" install --upgrade --index-url "$PYPI_INDEX_URL" clawsomeflow
    fi
  else
    "$VENV_DIR/bin/pip" install --upgrade --index-url "$PYPI_INDEX_URL" clawsomeflow
  fi
fi

ensure_mcp_sdk_compatible || {
  echo "MCP Python SDK pin failed (need mcp>=1,<2)." >&2
  exit 1
}
install_clawteam_stack 1 || {
  echo "clawteam stack verification failed after clawsomeflow install." >&2
  exit 1
}

if [[ "$INSTALL_SHIMS" == "1" ]]; then
  ensure_local_bin_in_path
  ln -sf "$VENV_DIR/bin/csflow" "${HOME}/.local/bin/csflow"
  ln -sf "$VENV_DIR/bin/clawsomeflow" "${HOME}/.local/bin/clawsomeflow"
  ln -sf "$VENV_DIR/bin/clawteam" "${HOME}/.local/bin/clawteam"
  hash -r
fi

INSTALLED_VERSION="$("$VENV_DIR/bin/csflow" version 2>/dev/null || true)"
if [[ -z "$INSTALLED_VERSION" ]]; then
  echo "Failed to determine installed clawsomeflow version." >&2
  exit 1
fi

reconcile_data_dir

echo "Installed ClawsomeFlow into: $VENV_DIR"
echo "Installed version: $INSTALLED_VERSION"
if [[ "$RECONCILE_DATA_DIR" == "1" ]]; then
  if [[ "$EXISTING_DEPLOYMENT" == "1" ]]; then
    echo "Data directory reconciled with in-place upgrade flow."
  else
    echo "Data directory initialized via first-time install flow."
  fi
  echo "Removed OpenClaw registrations are not auto-restored during reconcile."
fi
echo "Start command:"
if [[ "$INSTALL_SHIMS" == "1" ]]; then
  echo "  csflow start"
else
  echo "  $VENV_DIR/bin/csflow start"
fi
echo "Current deployed version: $INSTALLED_VERSION"
print_agent_platform_summary
