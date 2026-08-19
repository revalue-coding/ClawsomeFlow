#!/usr/bin/env bash
# Run the ClawsomeFlow test suite inside an isolated Docker container.
#
# This is the ONLY supported way to run the tests: the container has its own
# filesystem and network namespace, so a test can never touch the host's real
# ~/.clawsomeflow / ~/.openclaw / ~/.clawteam or a live gateway on :18789.
#
# The repo and the ClawTeam checkout are copied READ-ONLY into a throwaway build
# context (a temp dir); nothing is ever written back into the source tree.
#
# Usage:
#   scripts/test-in-docker.sh                       # L1 backend suite (default)
#   scripts/test-in-docker.sh -q backend/tests/test_api_guard.py   # subset (args -> pytest)
#   SKIP_BUILD=1 scripts/test-in-docker.sh ...      # reuse the existing image
#   KEEP_IMAGE=1 scripts/test-in-docker.sh ...      # keep the image after the run
#
# The test image is ~2GB; by default it is REMOVED once the run finishes so
# repeated runs never pile up layers in the container store. Pair KEEP_IMAGE=1
# with SKIP_BUILD=1 when iterating on a test locally, then drop KEEP_IMAGE on the
# final run to leave the host clean.
#
# Env:
#   CLAWTEAM_SRC   Path to the local ClawTeam checkout (clawteam is not on PyPI).
#                  Default: a sibling "ClawTeam" dir next to this repo.
#   CSFLOW_TEST_IMAGE  Image tag (default: csflow-test:latest).
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$HERE/.." && pwd)"
DOCKER_DIR="$REPO/docker/test"
CLAWTEAM_SRC="${CLAWTEAM_SRC:-$(cd "$REPO/.." && pwd)/ClawTeam}"
IMAGE="${CSFLOW_TEST_IMAGE:-csflow-test:latest}"

# Use rootless docker if it works without sudo; otherwise fall back to sudo.
if docker info >/dev/null 2>&1; then DOCKER="docker"; else DOCKER="sudo docker"; fi

if [[ ! -f "$CLAWTEAM_SRC/pyproject.toml" ]]; then
  echo "ERROR: ClawTeam source not found at '$CLAWTEAM_SRC'." >&2
  echo "       clawteam is not published to PyPI — point CLAWTEAM_SRC at a local checkout:" >&2
  echo "       CLAWTEAM_SRC=/path/to/ClawTeam scripts/test-in-docker.sh" >&2
  exit 2
fi

if [[ "${SKIP_BUILD:-0}" != "1" ]]; then
  CTX="$(mktemp -d)"
  trap 'rm -rf "$CTX"' EXIT

  rsync -a \
    --exclude '.git' --exclude 'node_modules' --exclude '__pycache__' \
    --exclude '*.pyc' --exclude '.pytest_cache' --exclude '.ruff_cache' \
    --exclude '.mypy_cache' --exclude 'frontend/dist' \
    --exclude '.venv' --exclude '.venv311' --exclude 'backend/.venv' \
    "$REPO/" "$CTX/app/"

  # Placeholder SPA so the backend's hatchling force-include (../frontend/dist)
  # resolves during install AND mount_frontend() finds dist/ + dist/assets/.
  mkdir -p "$CTX/app/frontend/dist/assets"
  printf '<!doctype html><title>test placeholder</title>\n' > "$CTX/app/frontend/dist/index.html"
  : > "$CTX/app/frontend/dist/assets/.keep"

  rsync -a \
    --exclude '.git' --exclude '__pycache__' --exclude '*.pyc' \
    --exclude '.pytest_cache' --exclude '.venv' \
    "$CLAWTEAM_SRC/" "$CTX/clawteam/"

  cp "$DOCKER_DIR/Dockerfile" "$CTX/Dockerfile"
  cp "$DOCKER_DIR/run-tests.sh" "$CTX/run-tests.sh"
  cp -r "$DOCKER_DIR/fake-bin" "$CTX/fake-bin"

  $DOCKER build -t "$IMAGE" "$CTX"
fi

status=0
$DOCKER run --rm "$IMAGE" "$@" || status=$?

if [[ "${KEEP_IMAGE:-0}" != "1" ]]; then
  $DOCKER rmi -f "$IMAGE" >/dev/null 2>&1 || true
  # Dangling-only: never touch another project's tagged images on a shared host.
  $DOCKER image prune -f >/dev/null 2>&1 || true
  $DOCKER builder prune -f >/dev/null 2>&1 || true
fi

exit "$status"
