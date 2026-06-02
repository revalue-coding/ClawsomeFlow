#!/usr/bin/env bash
# Fast gate for PR validation on GitHub hosted runners.

set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
export PATH="${HOME}/.local/bin:${PATH}"

say() { printf "\033[1;36m%s\033[0m\n" "$*"; }
fail() { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

AUTO_INSTALL=0
for arg in "$@"; do
  case "$arg" in
    --install)
      AUTO_INSTALL=1
      ;;
    *)
      fail "unknown argument: ${arg} (supported: --install)"
      ;;
  esac
done

command -v python3 >/dev/null || fail "python3 is required"

if [[ ! -d "${ROOT_DIR}/frontend/node_modules" ]]; then
  if [[ "${AUTO_INSTALL}" == "1" ]]; then
    command -v npm >/dev/null || fail "npm is required for --install"
    say "[fast] frontend npm ci (--install)"
    ( cd "${ROOT_DIR}/frontend" && npm ci )
  else
    fail "frontend dependencies missing: run npm ci in frontend first, or use ./scripts/test-fast.sh --install"
  fi
fi
command -v npx >/dev/null || fail "npx is required (run npm ci first)"

say "[fast] backend pytest"
( cd "${ROOT_DIR}/backend" && python3 -m pytest -q )

say "[fast] frontend typecheck"
( cd "${ROOT_DIR}/frontend" && npx tsc -b --noEmit )

say "[fast] frontend build"
( cd "${ROOT_DIR}/frontend" && npx vite build )

say "[fast] anti-loop invariants (unit)"
( cd "${ROOT_DIR}/backend" && python3 -m pytest -q tests/test_clawteam_cli.py tests/test_dispatch_prompts.py )

say "[fast] all checks passed"
