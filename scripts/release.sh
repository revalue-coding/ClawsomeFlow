#!/usr/bin/env bash
# ClawsomeFlow release runner — one button to ship.
#
# What it does (in order):
#   1. Pre-flight: clean git tree, on `main`, gh auth, twine config,
#      non-empty CHANGELOG [Unreleased], etc.
#   2. Run the test + build matrix (backend pytest, frontend tsc + vite build).
#   3. Ask you to choose:
#        - bump part (patch / minor / major)
#        - channel (release | beta | rc)
#      Computes & confirms the new version.
#   4. Cut the [Unreleased] section in CHANGELOG.md into the new version.
#   5. Sync the version literal into 3 files (SSOT + pyproject + package.json).
#   6. Sync public `install.sh` + `upgrade.sh` into the website repo workspace.
#      Optional: `--sync-site-repo` commits website repo; `--site-push` pushes it.
#   7. Commit + tag (`vX.Y.Z` or `vX.Y.ZbN`), push to origin (with tags).
#   8. Build the wheel + sdist into ./dist/.
#   9. `twine upload` (PyPI by default; `--testpypi` to dry-run on TestPyPI).
#   10. Create a GitHub Release (`gh release create`) with the cut CHANGELOG
#      section as the body. Pre-releases get the `--prerelease` flag so the
#      GitHub UI shows them with the right badge.
#
# Default behaviour for end users:
#   `pip install clawsomeflow` always picks the latest **release** version
#   (PEP 440 skips betas / rcs unless `--pre` is passed). So even after
#   shipping a beta, regular installs are safe.
#
# Usage:
#   scripts/release.sh                       # interactive
#   scripts/release.sh patch                 # non-interactive, channel=release
#   scripts/release.sh patch beta            # non-interactive beta
#   scripts/release.sh --testpypi minor      # dry-run upload to TestPyPI
#   scripts/release.sh --skip-full-tests patch
#      Skip L1 backend pytest only; frontend typecheck + vite build still run
#      in step 2. All other release steps unchanged. Use on hosts where the
#      deployed csflow service (17017) is an older build — release gate pytest
#      uses in-process TestClient, not that service, but you may skip it for
#      speed when L1 was already green elsewhere.
#   scripts/release.sh --skip-tests --skip-upload patch
#   scripts/release.sh --skip-install-sync patch
#   scripts/release.sh patch beta --sync-site-repo --site-push -y
#      Beta channel skips the test matrix by default (use --skip-tests on
#      release/rc, or pass tests manually before shipping). Skipped-matrix
#      releases still run `vite build` immediately before the wheel step so
#      `frontend/dist` is never stale inside the artifact.
#   scripts/release.sh --dry-run patch       # rehearsal mode: no push/upload and no persisted file edits
#
# Reversibility:
#   Steps 0–2 (venv bootstrap, pre-flight, test/build gate) never touch
#   release-mutation files. Abort there (Ctrl+C / test failure) and re-run
#   as-is — no manual rollback.
#   After you confirm the target version, the script snapshots those files and
#   auto-restores them (plus any local-only release commit/tag) on abort unless
#   the release finishes successfully or the commit has already been pushed.

set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"
RELEASE_VENV_DIR="${CSFLOW_RELEASE_VENV_DIR:-$REPO/.venv311}"
RELEASE_PYTHON_BIN="${CSFLOW_RELEASE_PYTHON_BIN:-python3.11}"
VENV_ACTIVATED=0
RELEASE_SNAPSHOT_DIR=""
RELEASE_MUTATIONS_STARTED=0
RELEASE_TESTS_GATE_PASSED=0
RELEASE_SUCCEEDED=0
PUSH_COMPLETED=0
LOCAL_RELEASE_TAG=""
RELEASE_MUTATED_FILES=(
  "CHANGELOG.md"
  "backend/app/__init__.py"
  "backend/pyproject.toml"
  "frontend/package.json"
)

# ── helpers ────────────────────────────────────────────────────────
say()   { printf "\033[1;36m%s\033[0m\n" "$*"; }
ok()    { printf "\033[1;32m%s\033[0m\n" "$*"; }
warn()  { printf "\033[1;33m%s\033[0m\n" "$*"; }
fail()  { printf "\033[1;31m%s\033[0m\n" "$*" >&2; exit 1; }

confirm() {
  if [[ "$YES" == "1" ]]; then return 0; fi
  read -r -p "  $1 [y/N]: " answer
  [[ "$answer" =~ ^[yY] ]]
}

ask_choice() {
  # ask_choice "question" default opt1 opt2 ...
  local q="$1"; shift
  local def="$1"; shift
  if [[ "$YES" == "1" ]]; then echo "$def"; return; fi
  echo "  $q (one of: $*; default: $def)"
  read -r -p "  > " v
  echo "${v:-$def}"
}

current_git_branch() {
  git symbolic-ref --quiet --short HEAD 2>/dev/null || true
}

require_main_branch() {
  local branch
  branch="$(current_git_branch)"
  [[ -n "$branch" ]] || fail "Must be on branch 'main' (detached HEAD is not allowed)."
  [[ "$branch" == "main" ]] || fail "Version release can only run on 'main' (currently on '$branch')."
}

activate_release_env() {
  say "[0/9] Preparing release Python environment"
  if [[ ! -x "$RELEASE_VENV_DIR/bin/python" ]]; then
    command -v "$RELEASE_PYTHON_BIN" >/dev/null \
      || fail "Missing $RELEASE_PYTHON_BIN. Install Python 3.11+ first."
    "$RELEASE_PYTHON_BIN" -m venv "$RELEASE_VENV_DIR"
    ok "  created venv: $RELEASE_VENV_DIR"
  fi
  # shellcheck disable=SC1090
  source "$RELEASE_VENV_DIR/bin/activate"
  VENV_ACTIVATED=1

  if ! python -c "import build, twine, pytest, pytest_asyncio" >/dev/null 2>&1; then
    say "  Installing release toolchain into venv"
    python -m pip install -U pip build twine pytest pytest-asyncio >/dev/null
  fi

  ok "  python: $(python --version 2>&1)"
  ok "  venv: $RELEASE_VENV_DIR"
  echo
}

build_frontend_vite() {
  if [[ ! -d frontend/node_modules ]]; then
    fail "frontend/node_modules missing — run 'cd frontend && pnpm install' before releasing."
  fi
  ( cd frontend && npx tsc -b --noEmit ) || fail "frontend tsc failed"
  ( cd frontend && npx vite build ) || fail "frontend vite build failed"
}

run_backend_full_tests() {
  # L1 backend gate runs INSIDE the Docker test image (scripts/test-in-docker.sh)
  # so it is fully isolated from the host: a separate filesystem + network
  # namespace mean the suite can never touch the real ~/.clawsomeflow /
  # ~/.openclaw or a live gateway on :18789. The image bundles real openclaw +
  # clawteam and auth-free fake agent CLIs; its default entrypoint runs
  # backend/tests (which already includes the anti-loop invariant tests
  # test_clawteam_cli / test_dispatch_prompts).
  # No args → the image entrypoint runs `pytest -q backend/tests` (the L1 gate).
  # Do NOT pass a bare `-q` here: that would be treated as the full pytest
  # argv and pull in the repo-root tests/ dir, whose conftest collides with
  # backend/tests under importlib mode.
  "$REPO/scripts/test-in-docker.sh" || fail "docker pytest failed"
}

run_frontend_build_gates() {
  if [[ ! -d frontend/node_modules ]]; then
    warn "  frontend/node_modules missing — skipping front-end gate."
    warn "  Run 'cd frontend && pnpm install' to enable."
    return 0
  fi
  build_frontend_vite
}

deactivate_release_env() {
  if [[ "$VENV_ACTIVATED" == "1" ]]; then
    deactivate >/dev/null 2>&1 || true
  fi
}

snapshot_release_mutation_files() {
  [[ -n "$RELEASE_SNAPSHOT_DIR" ]] && return 0
  RELEASE_SNAPSHOT_DIR="$(mktemp -d)"
  for rel in "${RELEASE_MUTATED_FILES[@]}"; do
    mkdir -p "$RELEASE_SNAPSHOT_DIR/$(dirname "$rel")"
    cp "$REPO/$rel" "$RELEASE_SNAPSHOT_DIR/$rel"
  done
}

begin_release_mutations() {
  [[ "$RELEASE_TESTS_GATE_PASSED" == "1" ]] \
    || fail "Internal error: release mutations started before the test gate passed."
  snapshot_release_mutation_files
  RELEASE_MUTATIONS_STARTED=1
  if [[ "$DRY_RUN" == "1" ]]; then
    warn "  [dry-run] release files snapshotted — auto-restored on exit"
  fi
}

restore_release_mutation_files() {
  [[ "$RELEASE_MUTATIONS_STARTED" == "1" ]] || return 0
  # Real releases keep mutations once pushed; dry-run always rolls back.
  if [[ "$RELEASE_SUCCEEDED" == "1" && "$DRY_RUN" != "1" ]]; then
    return 0
  fi
  if [[ "$PUSH_COMPLETED" == "1" ]]; then
    return 0
  fi
  [[ -n "$RELEASE_SNAPSHOT_DIR" && -d "$RELEASE_SNAPSHOT_DIR" ]] || return 0
  for rel in "${RELEASE_MUTATED_FILES[@]}"; do
    if [[ -f "$RELEASE_SNAPSHOT_DIR/$rel" ]]; then
      cp "$RELEASE_SNAPSHOT_DIR/$rel" "$REPO/$rel"
    fi
  done
  if [[ "$DRY_RUN" == "1" ]]; then
    warn "  [dry-run] restored release-mutation files"
  elif [[ "$RELEASE_MUTATIONS_STARTED" == "1" ]]; then
    warn "  restored release-mutation files after abort"
  fi
}

rollback_local_git_release() {
  [[ "$RELEASE_SUCCEEDED" == "1" && "$DRY_RUN" != "1" ]] && return 0
  [[ "$PUSH_COMPLETED" == "1" ]] && return 0
  [[ -n "$LOCAL_RELEASE_TAG" ]] || return 0

  local head_msg=""
  head_msg="$(git log -1 --format=%s 2>/dev/null || true)"
  if [[ "$head_msg" == "release: ${NEXT}" ]]; then
    git reset --hard HEAD~1 >/dev/null 2>&1 \
      && warn "  rolled back local release commit"
  fi
  if git rev-parse "$LOCAL_RELEASE_TAG" >/dev/null 2>&1; then
    git tag -d "$LOCAL_RELEASE_TAG" >/dev/null 2>&1 \
      && warn "  removed local tag ${LOCAL_RELEASE_TAG}"
  fi
}

cleanup_on_exit() {
  local exit_code=$?
  set +e
  restore_release_mutation_files
  rollback_local_git_release
  if [[ -n "$RELEASE_SNAPSHOT_DIR" && -d "$RELEASE_SNAPSHOT_DIR" ]]; then
    rm -rf "$RELEASE_SNAPSHOT_DIR"
    RELEASE_SNAPSHOT_DIR=""
  fi
  deactivate_release_env
  return "$exit_code"
}

trap cleanup_on_exit EXIT INT TERM

# ── arg parsing ────────────────────────────────────────────────────
YES=0
DRY_RUN=0
SKIP_TESTS=0
SKIP_FULL_TESTS=0
SKIP_BUILD=0
SKIP_UPLOAD=0
SKIP_PUSH=0
SKIP_INSTALL_SYNC=0
SYNC_SITE_REPO=0
SITE_PUSH=0
SITE_REPO_DIR="${CSFLOW_PUBLIC_SITE_REPO:-$HOME/clawsomeflow.com}"
PYPI_REPO="pypi"          # `pypi` or `testpypi`
BUMP_PART=""
CHANNEL=""

while (( $# )); do
  case "$1" in
    -y|--yes) YES=1 ;;
    --dry-run) DRY_RUN=1 ;;
    --skip-tests) SKIP_TESTS=1 ;;
    --skip-full-tests) SKIP_FULL_TESTS=1 ;;
    --skip-build) SKIP_BUILD=1 ;;
    --skip-upload) SKIP_UPLOAD=1 ;;
    --skip-push) SKIP_PUSH=1 ;;
    --skip-install-sync) SKIP_INSTALL_SYNC=1 ;;
    --sync-site-repo) SYNC_SITE_REPO=1 ;;
    --site-push) SYNC_SITE_REPO=1; SITE_PUSH=1 ;;
    --testpypi) PYPI_REPO="testpypi" ;;
    --pypi) PYPI_REPO="pypi" ;;
    -h|--help)
      sed -n '2,46p' "$0"; exit 0 ;;
    patch|minor|major) BUMP_PART="$1" ;;
    release|beta|rc) CHANNEL="$1" ;;
    *) fail "Unknown argument: $1" ;;
  esac
  shift
done

# Beta pre-releases skip the test matrix by default (faster iteration).
if [[ "$CHANNEL" == "beta" ]]; then
  SKIP_TESTS=1
fi

# Hard gate: release flow only runs on main.
command -v git >/dev/null || fail "git not found"
require_main_branch

activate_release_env

# ── 1. Pre-flight ──────────────────────────────────────────────────
say "🦞 ClawsomeFlow release runner"
echo
say "[1/9] Pre-flight checks"

command -v git    >/dev/null || fail "git not found"
command -v python >/dev/null || fail "python not found"
command -v gh     >/dev/null || warn "gh not found (skip GitHub Release step)"
python -c "import build" 2>/dev/null || fail "Missing build: pip install build"
python -c "import twine" 2>/dev/null || fail "Missing twine: pip install twine"

# Branch + clean tree
require_main_branch
if [[ -n "$(git status --porcelain)" ]]; then
  fail "Working tree not clean. Commit or stash before releasing."
fi
git fetch --tags --quiet
ok "  git: on main + clean tree"

# Versions consistent
python scripts/_bump_version.py check >/dev/null || fail "Version drift; run scripts/_bump_version.py check"
CURRENT="$(python scripts/_bump_version.py current)"
ok "  current version: $CURRENT"

python scripts/_cut_changelog.py --check \
  || fail "CHANGELOG [Unreleased] is empty. Add entries under ### Added / Changed / Fixed / ... before releasing."
ok "  CHANGELOG [Unreleased] has release notes"
echo

# ── 2. Tests + build ───────────────────────────────────────────────
if [[ "$SKIP_TESTS" == "0" ]]; then
  say "[2/9] Running test matrix"
  if [[ "$SKIP_FULL_TESTS" == "0" ]]; then
    run_backend_full_tests
  else
    warn "  Skipping full backend pytest (--skip-full-tests)"
  fi
  run_frontend_build_gates
  ok "  tests + builds green"
  RELEASE_TESTS_GATE_PASSED=1
  echo
else
  if [[ "$CHANNEL" == "beta" ]]; then
    warn "[2/9] Skipping test matrix (beta channel; frontend build runs before wheel)"
  else
    warn "[2/9] Skipping test matrix (--skip-tests; frontend build runs before wheel)"
  fi
  RELEASE_TESTS_GATE_PASSED=1
  echo
fi

# ── 3. Choose new version ──────────────────────────────────────────
say "[3/9] Choose the new version"
[[ -n "$BUMP_PART" ]] || BUMP_PART="$(ask_choice 'Bump which part?' 'patch' patch minor major)"
[[ -n "$CHANNEL"   ]] || CHANNEL="$(ask_choice 'Channel?' 'release' release beta rc)"
NEXT="$(python scripts/_bump_version.py next "$BUMP_PART" --channel "$CHANNEL")"
TAG="v$NEXT"
say "  $CURRENT  →  $NEXT   (tag: $TAG)"
confirm "Proceed?" || fail "Aborted."
begin_release_mutations
echo

# ── 4. Cut CHANGELOG ───────────────────────────────────────────────
say "[4/9] Cutting CHANGELOG.md"
RELEASE_BODY="$(python scripts/_cut_changelog.py "$NEXT")" \
  || fail "CHANGELOG cut failed (probably an empty [Unreleased] section)."
ok "  CHANGELOG updated."
echo

# ── 5. Sync version literal ────────────────────────────────────────
say "[5/9] Syncing version literals (SSOT + pyproject + package.json)"
python scripts/_bump_version.py set "$NEXT"
python scripts/_bump_version.py check >/dev/null || fail "Post-bump drift?!"
git --no-pager diff --stat
echo

# ── 6. Sync public install endpoint ────────────────────────────────
say "[6/10] Syncing public install endpoint"
if [[ "$SKIP_INSTALL_SYNC" == "1" ]]; then
  warn "  --skip-install-sync: skip syncing public install.sh"
elif [[ "$DRY_RUN" == "1" ]]; then
  warn "  [dry-run] skipping public install.sh sync"
else
  ( bash scripts/publish-install-endpoint.sh ) \
    || fail "Failed to sync public install endpoint."
  ok "  public install.sh + upgrade.sh synced → ${SITE_REPO_DIR}/site/"
  if [[ "$SYNC_SITE_REPO" == "1" ]]; then
    CSFLOW_PUBLIC_SITE_REPO="${SITE_REPO_DIR}" \
      CSFLOW_SITE_COMMIT=1 \
      CSFLOW_SITE_PUSH="$([[ "$SITE_PUSH" == "1" ]] && echo 1 || echo 0)" \
      CSFLOW_RELEASE_VERSION="${NEXT}" \
      bash scripts/publish-site-repo.sh \
      || fail "Failed to commit/push website repository."
    if [[ "$SITE_PUSH" == "1" ]]; then
      ok "  website repo synced to git (pushed when changes existed)"
    else
      ok "  website repo sync attempted (pass --site-push to publish git)"
    fi
    warn "  Production URLs still need: cd ${SITE_REPO_DIR} && ./scripts/deploy-site.sh"
  fi
fi
echo

# ── 7. Commit + tag + push ─────────────────────────────────────────
say "[7/10] Committing + tagging $TAG"
if [[ "$DRY_RUN" == "1" ]]; then
  warn "  [dry-run] skipping git commit / tag / push"
else
  # NOTE: CHANGELOG.md is intentionally private (see .gitignore) — it is cut
  # locally and reused as the GitHub Release body, but never committed to the
  # public repo. Only the version-literal files are committed here.
  git add backend/app/__init__.py backend/pyproject.toml frontend/package.json
  git commit -m "release: $NEXT"
  LOCAL_RELEASE_TAG="$TAG"
  git tag -a "$TAG" -m "ClawsomeFlow $NEXT"
  if [[ "$SKIP_PUSH" == "0" ]]; then
    git push origin main
    git push origin "$TAG"
    PUSH_COMPLETED=1
    ok "  pushed commit + tag to origin"
  else
    warn "  --skip-push: not pushing to origin (commit + tag are local)."
  fi
fi
echo

# ── 8. Build wheel + sdist ─────────────────────────────────────────
if [[ "$SKIP_BUILD" == "0" ]]; then
  say "[8/10] Building wheel + sdist into ./dist/"
  if [[ "$SKIP_TESTS" == "1" ]]; then
    say "  Building frontend/dist (required — wheel bundles prebuilt SPA)"
    if [[ ! -d frontend/node_modules ]]; then
      fail "frontend/node_modules missing — run 'cd frontend && pnpm install' before releasing."
    fi
    ( cd frontend && npx vite build ) || fail "frontend vite build failed"
    ok "  frontend/dist refreshed"
  fi
  rm -rf dist/
  ( cd backend && python -m build --outdir "$REPO/dist" ) || fail "build failed"
  ls -lh dist/ | tail -n +2
  echo
fi

# ── 9. Upload to PyPI ──────────────────────────────────────────────
if [[ "$SKIP_UPLOAD" == "0" ]]; then
  say "[9/10] twine upload → $PYPI_REPO"
  if [[ "$DRY_RUN" == "1" ]]; then
    warn "  [dry-run] skipping twine upload"
  else
    if ! twine check dist/*; then
      fail "twine check rejected the artifacts."
    fi
    if [[ "$PYPI_REPO" == "testpypi" ]]; then
      python -m twine upload --repository testpypi dist/*
    else
      python -m twine upload dist/*
    fi
    ok "  uploaded to $PYPI_REPO"
  fi
  echo
fi

# ── 10. GitHub Release ─────────────────────────────────────────────
say "[10/10] Creating GitHub Release"
if ! command -v gh >/dev/null; then
  warn "  gh CLI not installed — skipping. Create release manually at"
  warn "  https://github.com/clawsomeflow/clawsomeflow/releases/new?tag=$TAG"
elif [[ "$DRY_RUN" == "1" ]]; then
  warn "  [dry-run] would run: gh release create $TAG ..."
else
  IS_PRE=$([[ "$NEXT" == *b* || "$NEXT" == *rc* || "$NEXT" == *a* ]] && echo "--prerelease" || echo "")
  body_file="$(mktemp)"
  printf "## ClawsomeFlow %s\n\n%s\n" "$NEXT" "$RELEASE_BODY" > "$body_file"
  gh release create "$TAG" \
    --title "ClawsomeFlow $NEXT" \
    --notes-file "$body_file" \
    $IS_PRE \
    dist/*
  rm -f "$body_file"
  ok "  GitHub Release published"
fi
echo

# ── Done ──────────────────────────────────────────────────────────
RELEASE_SUCCEEDED=1
ok "✅ Released $NEXT"
case "$CHANNEL" in
  release)
    say "  Users will get this via:  pip install -U clawsomeflow"
    ;;
  beta|rc)
    say "  Pre-release — users will need:  pip install --pre -U clawsomeflow"
    ;;
esac
