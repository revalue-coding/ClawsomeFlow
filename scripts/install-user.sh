#!/usr/bin/env bash
# ClawsomeFlow end-user installer (local mode, managed user service).
#
# This script is designed to be executed directly or via remote pipe:
#   curl -fsSL <url>/scripts/install-user.sh | bash
#
# Guarantees after successful run:
#   1) ClawsomeFlow runs in background as a user service.
#   2) Service is enabled at boot.
#      - Linux: systemd --user (+ linger when permitted)
#      - macOS: launchd (LaunchAgents)
#   3) Runtime is isolated under ~/.clawsomeflow/.venv (Python 3.11+).

set -euo pipefail

YES=0
USE_PRE=0
SKIP_LINGER=0
OS_NAME="$(uname -s)"
IS_MACOS=0
if [[ "${OS_NAME}" == "Darwin" ]]; then
  IS_MACOS=1
fi
PYPI_INDEX_URL="${CSFLOW_PYPI_INDEX_URL:-https://pypi.org/simple}"
CSFLOW_HOME="${CSFLOW_HOME:-$HOME/.clawsomeflow}"
VENV_DIR="${CSFLOW_VENV_DIR:-${CSFLOW_HOME}/.venv}"
VENV_BIN="${VENV_DIR}/bin"
CSFLOW_PORT="${CSFLOW_PORT:-17017}"
LOCAL_CLAWTEAM_SOURCE="${HOME}/ClawTeam"
EXISTING_DEPLOYMENT=0
PYTHON_RUNTIME_BIN=""

for arg in "$@"; do
  case "$arg" in
    -y|--yes) YES=1 ;;
    --pre) USE_PRE=1 ;;
    --skip-linger) SKIP_LINGER=1 ;;
    -h|--help)
      cat <<'USAGE'
ClawsomeFlow end-user installer (background + auto-start).

Usage:
  ./install-user.sh [--yes|-y] [--pre] [--skip-linger]

Options:
  --yes, -y        Non-interactive mode for package/service steps.
  --pre            Install pre-release clawsomeflow from PyPI.
  --skip-linger    Linux-only: do not run `loginctl enable-linger`.

Result:
  - Installs Python 3.11 runtime.
  - Installs latest stable clawsomeflow release into ~/.clawsomeflow/.venv (or prerelease with --pre).
  - Installs latest clawteam from PyPI (no version pin) and verifies runtime subcommand.
  - OpenClaw / Claude / Codex / Cursor / Hermes are optional runtimes (not auto-installed by this script).
  - Writes launcher shims into ~/.local/bin (csflow / clawsomeflow / clawteam).
  - First-time install initializes ~/.clawsomeflow; rerun performs in-place upgrade.
  - Enables and starts managed background service (`systemd --user` on Linux / `launchd` on macOS).

Upgrade CLI (for existing installs):
  - Stable:      curl -fsSL https://clawsomeflow.com/upgrade.sh | bash
  - Pre-release: curl -fsSL https://clawsomeflow.com/upgrade.sh | bash -s -- --pre

PEP 668 safety:
  - Upgrade/install always use ~/.clawsomeflow/.venv/bin/pip (never system pip),
    so they avoid Linux "error: externally-managed-environment".

Environment overrides:
  CSFLOW_PYPI_INDEX_URL   Package index (default: https://pypi.org/simple)
  CSFLOW_VENV_DIR         Runtime venv path (default: ~/.clawsomeflow/.venv)
USAGE
      exit 0
      ;;
  esac
done

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
warn() { printf "\033[1;33m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

if [[ "${YES}" == "1" ]]; then
  export DEBIAN_FRONTEND=noninteractive
fi

ensure_local_bin_in_path() {
  mkdir -p "${HOME}/.local/bin"
  case ":${PATH}:" in
    *":${HOME}/.local/bin:"*) ;;
    *) export PATH="${HOME}/.local/bin:${PATH}" ;;
  esac
}

probe_existing_deployment() {
  if [[ -d "${CSFLOW_HOME}" ]]; then
    EXISTING_DEPLOYMENT=1
  else
    EXISTING_DEPLOYMENT=0
  fi
}

list_port_listeners() {
  local port="$1"
  if command -v lsof >/dev/null 2>&1; then
    lsof -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR>1 {print}'
    return
  fi
  if command -v ss >/dev/null 2>&1; then
    ss -ltnp "sport = :${port}" 2>/dev/null | awk 'NR>1 {print}'
    return
  fi
}

ensure_port_reusable() {
  local listeners
  listeners="$(list_port_listeners "${CSFLOW_PORT}" || true)"
  if [[ -z "${listeners}" ]]; then
    return
  fi
  warn "Detected existing listeners on port ${CSFLOW_PORT}; trying graceful stop."
  "${VENV_BIN}/csflow" stop >/dev/null 2>&1 || true
  sleep 1
  listeners="$(list_port_listeners "${CSFLOW_PORT}" || true)"
  if [[ -n "${listeners}" ]]; then
    fail "Port ${CSFLOW_PORT} is still occupied before service startup: ${listeners}"
  fi
}

resolve_python_runtime_bin() {
  if command -v python3.11 >/dev/null 2>&1; then
    PYTHON_RUNTIME_BIN="$(command -v python3.11)"
    return
  fi
  if command -v python3 >/dev/null 2>&1; then
    if python3 - <<'PY' >/dev/null 2>&1
import sys
raise SystemExit(0 if sys.version_info >= (3, 11) else 1)
PY
    then
      PYTHON_RUNTIME_BIN="$(command -v python3)"
      return
    fi
  fi
  PYTHON_RUNTIME_BIN=""
}

ensure_python311() {
  resolve_python_runtime_bin
  if [[ -n "${PYTHON_RUNTIME_BIN}" ]]; then
    say "[1/9] Python 3.11 already present"
    return
  fi
  if [[ "${IS_MACOS}" == "1" ]]; then
    command -v brew >/dev/null 2>&1 \
      || fail "Python 3.11 is required on macOS. Please install Homebrew first: https://brew.sh"
    say "[1/9] Installing Python 3.11 (Homebrew)"
    brew install python@3.11 || fail "Failed to install python@3.11 via Homebrew."
    resolve_python_runtime_bin
    [[ -n "${PYTHON_RUNTIME_BIN}" ]] || fail "python@3.11 installed but python3.11 still unavailable in PATH."
    return
  fi
  if ! command -v apt-get >/dev/null; then
    fail "Python 3.11 is required. This host is not apt-based; install Python 3.11 manually."
  fi
  say "[1/9] Installing Python 3.11"
  sudo apt-get update
  sudo apt-get install -y python3.11 python3.11-venv
  resolve_python_runtime_bin
  [[ -n "${PYTHON_RUNTIME_BIN}" ]] || fail "Python 3.11 installation finished but executable still unavailable."
}

ensure_runtime_venv() {
  say "[2/9] Preparing isolated runtime venv"
  mkdir -p "$(dirname "${VENV_DIR}")"
  if [[ ! -x "${VENV_BIN}/python" ]]; then
    [[ -n "${PYTHON_RUNTIME_BIN}" ]] || fail "Python runtime not resolved."
    "${PYTHON_RUNTIME_BIN}" -m venv "${VENV_DIR}"
    say "  ✓ Created venv: ${VENV_DIR}"
  fi
  "${VENV_BIN}/python" -m pip install --upgrade pip >/dev/null
}

install_clawsomeflow() {
  say "[3/9] Installing clawsomeflow into runtime venv"
  local channel_label="stable"
  if [[ "${USE_PRE}" == "1" ]]; then
    channel_label="pre-release"
    "${VENV_BIN}/pip" install --upgrade --index-url "${PYPI_INDEX_URL}" --pre clawsomeflow || \
      fail "Failed to install clawsomeflow from PyPI (--pre)."
  else
    install_latest_stable_clawsomeflow || \
      fail "Failed to install clawsomeflow from PyPI."
  fi
  local installed_version
  installed_version="$("${VENV_BIN}/csflow" version 2>/dev/null || true)"
  [[ -n "${installed_version}" ]] || fail "Failed to detect installed clawsomeflow version."
  say "  ✓ Installed clawsomeflow version (${channel_label} channel): ${installed_version}"
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

ensure_clawteam_runtime() {
  say "[4/9] Ensuring clawteam runtime capability"
  if [[ -x "${VENV_BIN}/clawteam" ]] && "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1; then
    say "  ✓ clawteam runtime available"
    return
  fi

  local source_override="${CSFLOW_CLAWTEAM_SOURCE:-}"
  if [[ -n "${source_override}" ]]; then
    say "  -> trying configured clawteam source: ${source_override}"
    "${VENV_BIN}/pip" install --upgrade "${source_override}" \
      || fail "clawteam install from CSFLOW_CLAWTEAM_SOURCE failed"
    if [[ -x "${VENV_BIN}/clawteam" ]] && "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1; then
      say "  ✓ clawteam runtime available"
      return
    fi
  fi

  if [[ -f "${LOCAL_CLAWTEAM_SOURCE}/pyproject.toml" ]]; then
    say "  -> trying local clawteam source: ${LOCAL_CLAWTEAM_SOURCE}"
    "${VENV_BIN}/pip" install --upgrade "${LOCAL_CLAWTEAM_SOURCE}" \
      || fail "clawteam install from local source failed"
    if [[ -x "${VENV_BIN}/clawteam" ]] && "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1; then
      say "  ✓ clawteam runtime available"
      return
    fi
  fi

  say "  -> trying PyPI clawteam"
  if ! "${VENV_BIN}/pip" install --upgrade --index-url "${PYPI_INDEX_URL}" clawteam; then
    warn "PyPI clawteam install failed, will fallback to git clone source install."
  fi
  if [[ -x "${VENV_BIN}/clawteam" ]] && "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1; then
    say "  ✓ clawteam runtime available"
    return
  fi

  say "  -> PyPI clawteam lacks runtime, cloning upstream source"
  local tmp_dir
  tmp_dir="$(mktemp -d)"
  if ! git clone --depth 1 https://github.com/HKUDS/ClawTeam.git "${tmp_dir}/ClawTeam"; then
    rm -rf "${tmp_dir}"
    fail "clawteam git clone failed"
  fi
  if ! "${VENV_BIN}/pip" install --upgrade "${tmp_dir}/ClawTeam"; then
    rm -rf "${tmp_dir}"
    fail "clawteam install from cloned source failed"
  fi
  rm -rf "${tmp_dir}"

  [[ -x "${VENV_BIN}/clawteam" ]] || fail "clawteam install failed (binary missing)"
  "${VENV_BIN}/clawteam" runtime --help >/dev/null 2>&1 \
    || fail "clawteam installed but runtime command missing"
  say "  ✓ clawteam runtime available"
}

install_launchers() {
  say "[5/9] Installing launcher shims into ~/.local/bin"
  ensure_local_bin_in_path
  ln -sf "${VENV_BIN}/csflow" "${HOME}/.local/bin/csflow"
  ln -sf "${VENV_BIN}/clawsomeflow" "${HOME}/.local/bin/clawsomeflow"
  ln -sf "${VENV_BIN}/clawteam" "${HOME}/.local/bin/clawteam"
  hash -r
}

snapshot_existing_metadata() {
  [[ "${EXISTING_DEPLOYMENT}" == "1" ]] || return 0
  local backup_root="${CSFLOW_HOME}/.backups"
  local snapshot="${backup_root}/pre-upgrade-$(date +%Y%m%d-%H%M%S)"
  mkdir -p "${snapshot}"
  local copied=0
  for rel in "config.json" ".csflow-version"; do
    if [[ -f "${CSFLOW_HOME}/${rel}" ]]; then
      cp -a "${CSFLOW_HOME}/${rel}" "${snapshot}/${rel}"
      copied=1
    fi
  done
  if [[ "${copied}" == "1" ]]; then
    say "  ✓ Saved metadata snapshot: ${snapshot}"
  else
    rmdir "${snapshot}" >/dev/null 2>&1 || true
  fi
}

reconcile_installation() {
  say "[6/9] Reconciling ClawsomeFlow data"
  if [[ "${EXISTING_DEPLOYMENT}" == "1" ]]; then
    say "  -> Existing deployment detected: in-place upgrade (no uninstall)"
    snapshot_existing_metadata
    "${VENV_BIN}/csflow" upgrade-runtime --yes --no-restart-service \
      || fail "csflow upgrade-runtime failed"
  else
    say "  -> First-time deployment detected: initialize data layout"
    if [[ "${YES}" == "1" ]]; then
      "${VENV_BIN}/csflow" install --yes --no-restart-service || fail "csflow install failed"
    else
      "${VENV_BIN}/csflow" install --no-restart-service || fail "csflow install failed"
    fi
  fi
}

write_user_service() {
  say "[7/9] Configuring managed service"
  if [[ "${IS_MACOS}" == "1" ]]; then
    say "  -> macOS detected; launchd service file is managed by csflow CLI."
    return
  fi
  if ! command -v systemctl >/dev/null; then
    fail "systemctl not found; cannot configure background auto-start service."
  fi
  say "  -> configuring systemd user service"
  local unit_dir="${HOME}/.config/systemd/user"
  local unit_path="${unit_dir}/csflow.service"
  local csflow_bin="${VENV_BIN}/csflow"
  [[ -x "${csflow_bin}" ]] || fail "Missing csflow executable: ${csflow_bin}"
  mkdir -p "${unit_dir}"
  cat > "${unit_path}" <<EOF
[Unit]
Description=ClawsomeFlow backend (local mode)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
Environment=PATH=${VENV_BIN}:%h/.local/bin:/usr/local/bin:/usr/bin:/bin
ExecStart=${csflow_bin} serve --host 127.0.0.1 --port ${CSFLOW_PORT}
Restart=on-failure
RestartSec=3
KillMode=mixed
TimeoutStopSec=30

[Install]
WantedBy=default.target
EOF
}

enable_linger_if_possible() {
  if [[ "${IS_MACOS}" == "1" ]]; then
    say "[8/9] Skipping linger setup on macOS"
    return
  fi
  if [[ "${SKIP_LINGER}" == "1" ]]; then
    warn "Skipped loginctl linger setup (--skip-linger). Auto-start requires user session."
    return
  fi
  if ! command -v loginctl >/dev/null; then
    warn "loginctl not available; skip linger setup."
    return
  fi
  if [[ "${YES}" == "1" ]] && ! sudo -n true >/dev/null 2>&1; then
    warn "Non-interactive mode without passwordless sudo; skip linger setup."
    warn "Run manually for boot auto-start without login: sudo loginctl enable-linger ${USER}"
    return
  fi
  if sudo true >/dev/null 2>&1; then
    say "[8/9] Enabling user linger for boot auto-start"
    sudo loginctl enable-linger "${USER}" || warn "enable-linger failed; continuing"
  else
    warn "sudo loginctl unavailable; skip linger setup."
    warn "Run manually for boot auto-start without login: sudo loginctl enable-linger ${USER}"
  fi
}

start_user_service() {
  if [[ "${IS_MACOS}" == "1" ]]; then
    say "[9/9] Starting and enabling csflow service (launchd)"
    ensure_port_reusable
    local start_args=("start" "--skip-deps")
    if [[ "${YES}" == "1" ]]; then
      start_args+=("--yes")
    fi
    "${VENV_BIN}/csflow" "${start_args[@]}" || fail "Failed to start csflow via launchd on macOS."
    return
  fi

  say "[9/9] Starting and enabling csflow service (systemd)"
  ensure_port_reusable
  systemctl --user daemon-reload
  systemctl --user enable --now csflow
  systemctl --user --no-pager --full status csflow | sed -n '1,15p'
}

health_check() {
  local url="http://127.0.0.1:${CSFLOW_PORT}/"
  # First boot after an upgrade runs init/migration before uvicorn starts
  # listening, which can take well over a few seconds on a busy host. Give it
  # a generous window so a slow-but-successful start is not reported as a
  # failure.
  local attempts=60 i
  for ((i = 1; i <= attempts; i++)); do
    if curl -fsS "${url}" >/dev/null 2>&1; then
      say "✅ ClawsomeFlow is running in background: ${url}"
      return
    fi
    if (( i == 10 )); then
      say "… still starting (running upgrade/migration), waiting up to ${attempts}s …"
    fi
    sleep 1
  done
  fail "Service started but health check failed after ${attempts}s: ${url}"
}

print_deployed_version() {
  local deployed_version
  deployed_version="$("${VENV_BIN}/csflow" version 2>/dev/null || true)"
  [[ -n "${deployed_version}" ]] || fail "Failed to detect deployed ClawsomeFlow version."
  say "Current deployed version: ${deployed_version}"
}

say "🦞 ClawsomeFlow end-user installer"
probe_existing_deployment
ensure_local_bin_in_path
ensure_python311
ensure_runtime_venv
install_clawsomeflow
ensure_clawteam_runtime
install_launchers
reconcile_installation
write_user_service
enable_linger_if_possible
start_user_service
health_check
print_deployed_version

