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
#   CSFLOW_TEST_NODE_VERSION  Node linux-x64 tarball pin (default: 22.23.2).
set -euo pipefail

# Host-side Node 22 fetch. Official tarball first, then mirrors; resume
# retries so a 50–100 KB/s link can finish a ~30MB file. Checksum is
# verified here so the Dockerfile only unpacks.
NODE_VERSION="${CSFLOW_TEST_NODE_VERSION:-22.23.2}"
NODE_TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz"
NODE_CACHE_DIR="${CSFLOW_TEST_NODE_CACHE:-${TMPDIR:-/tmp}/csflow-test-node}"

fetch_url_resumable() {
  local dest="$1" url="$2"
  local i
  rm -f "$dest"
  for i in 1 2 3 4 5 6 7 8; do
    if curl -fL -4 -C - --connect-timeout 15 --max-time 240 -o "$dest" "$url"; then
      return 0
    fi
    # Connect/SSL failure left nothing — skip remaining retries for this URL.
    if [[ ! -s "$dest" ]]; then
      return 1
    fi
  done
  return 1
}

fetch_first_ok() {
  local dest="$1"; shift
  local url
  for url in "$@"; do
    echo "  fetching ${url}"
    if fetch_url_resumable "$dest" "$url"; then
      return 0
    fi
    rm -f "$dest"
  done
  return 1
}

stage_node_tarball() {
  local dest_dir="$1"
  local cache="${NODE_CACHE_DIR}/${NODE_TARBALL}"
  local sums="${NODE_CACHE_DIR}/SHASUMS256-${NODE_VERSION}.txt"
  mkdir -p "$NODE_CACHE_DIR"

  if [[ ! -s "$sums" ]]; then
    fetch_first_ok "$sums" \
      "https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt" \
      "https://cdn.npmmirror.com/binaries/node/v${NODE_VERSION}/SHASUMS256.txt" \
      "https://mirrors.cloud.tencent.com/nodejs-release/v${NODE_VERSION}/SHASUMS256.txt" \
      || { echo "ERROR: failed to download Node ${NODE_VERSION} SHASUMS256.txt" >&2; return 1; }
  fi

  local expected actual
  expected="$(awk -v f="$NODE_TARBALL" '$2 == f || $2 == ("*" f) { print $1; exit }' "$sums")"
  [[ -n "$expected" ]] || { echo "ERROR: ${NODE_TARBALL} not listed in SHASUMS256.txt" >&2; return 1; }

  actual=""
  if [[ -s "$cache" ]]; then
    actual="$(sha256sum "$cache" | awk '{ print $1 }')"
  fi
  if [[ "$actual" != "$expected" ]]; then
    echo "Downloading Node ${NODE_VERSION} linux-x64 tarball on the host..."
    fetch_first_ok "$cache" \
      "https://cdn.npmmirror.com/binaries/node/v${NODE_VERSION}/${NODE_TARBALL}" \
      "https://mirrors.cloud.tencent.com/nodejs-release/v${NODE_VERSION}/${NODE_TARBALL}" \
      "https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}" \
      || { echo "ERROR: failed to download ${NODE_TARBALL}" >&2; return 1; }
    actual="$(sha256sum "$cache" | awk '{ print $1 }')"
    [[ "$actual" == "$expected" ]] || {
      echo "ERROR: ${NODE_TARBALL} sha256 mismatch (got ${actual}, want ${expected})" >&2
      rm -f "$cache"
      return 1
    }
  else
    echo "Using cached Node ${NODE_VERSION} tarball (${cache})"
  fi

  cp "$cache" "${dest_dir}/node.tar.xz"
}

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
  stage_node_tarball "$CTX"

  # Linux: host network avoids Docker-bridge CDN timeouts (NodeSource / npm).
  BUILD_OPTS=()
  if [[ "$(uname -s)" == "Linux" ]]; then
    BUILD_OPTS+=(--network host)
  fi
  $DOCKER build "${BUILD_OPTS[@]}" -t "$IMAGE" "$CTX"
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
