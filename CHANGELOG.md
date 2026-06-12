




# Changelog

All notable changes to **ClawsomeFlow** are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Pre-release identifiers (`X.Y.Zb1`, `X.Y.ZrcN`) follow [PEP 440](https://peps.python.org/pep-0440/);
`pip install clawsomeflow` will skip them by default — see the README's
"Pre-release channel" section if you want to track them.

## [Unreleased]

### Added
### Changed
### Fixed
- **"to Hermes" dashboard launch** (`backend/app/services/hermes_dashboard.py`) — replaced the
  bare TCP port probe with an **HTTP health check** that confirms the listener is actually the
  Hermes dashboard (matches its index-HTML markers), so an unrelated service squatting on
  `127.0.0.1:9119` is no longer mistaken for Hermes (which sent the jump to the wrong page).
  When the default port is held by a foreign service the launcher now **auto-switches** to the
  next free port in `9119–9128` (the returned URL follows), and raises a clear error if none is
  free. The WebUI is unchanged — it opens whatever URL the backend returns.
- **"to OpenClaw" / "to Hermes" from a remote browser** (`frontend/src/pages/OpenclawChat.tsx`,
  `HermesChat.tsx`) — both runtimes live on the server's local machine, so opening them from a
  remote/SSH browser session never works. "to OpenClaw" was an unguarded link that opened a dead
  tab; it now rejects up front (mirroring "to Hermes") with a message explaining the runtime is
  local-only and suggesting SSH local port forwarding. "to Hermes" no longer shows the unrelated
  "cannot open local folder" string — it uses a dashboard-specific reason
  (`hermes.dashboardRemoteUnavailable` / `chat.toOpenclawRemoteUnavailable`, en + zh). Detection
  covers direct-IP remote access; an `ssh -L` tunnel that presents as `localhost` is
  indistinguishable from a local browser and cannot be guarded client-side.
### Removed
### Deprecated
### Security

## [0.1.13b10] — 2026-06-12


### Changed
- **`scripts/release.sh`** — beta / `--skip-tests` releases now run `vite build`
  immediately before the wheel step so `frontend/dist` bundled into the artifact
  matches current source (beta no longer skips the SPA build).

## [0.1.13b9] — 2026-06-12


### Added
- **Flow list easy-mode badge** — Flows with 省心模式 enabled show a green dot + 「省心」
  label in the list (`FlowSummary.easyMode` from `spec.variables["csflow.easy_mode"]`).
- **Cross-process main-repo merge lock** — `~/.clawsomeflow/.locks/clawteam_main_repo/`
  flock files pair with in-process `clawteam_main_repo` locking; ClawsomeFlow
  `workspace_merge` and agent self-merge prompts use the same lock path.
- **`POST /api/hermes/agents/dashboard/open`** — ensures `hermes dashboard` is
  running on loopback `:9119` and returns the chat URL; WebUI **to Hermes** uses
  this instead of asking the user to start the dashboard manually.
- **`AgentPageToolbar`** — shared header action bar for OpenClaw / Hermes agent
  list pages (icon refresh + grouped secondary actions + primary CTA).

### Changed
- **省心模式 card** — Flow editor easy-mode block uses a light grass-green
  background.
- **Self-merge agent prompts** — concise flock-wrapped checkout/merge one-liner;
  on conflict, resolve without holding the lock, commit, then re-run the locked
  command (no long scheduler/policy prose).
- **OpenClaw remove modal** — opened from a card no longer shows a target-agent
  picker; hint is 「请选择移除方式」 / “Choose a removal mode.”
- **OpenClaw / Hermes list headers** — toolbar layout refresh (grouped actions,
  primary create button).
- **`workspace_merge`** — expands `~` repo paths; verifies branch after checkout,
  rejects dirty baseline / in-progress merge; `git pull --ff-only` before merge;
  holds flock file lock alongside the in-process repo lock.

### Fixed
- **Flow list Edit hover** — replaced `<Link>` with a button so the browser
  status bar no longer shows the flow URL on hover.
- **to Hermes link** — no longer inherits HTTPS/remote hostname (which broke
  `http://127.0.0.1:9119/chat`); backend starts dashboard when needed.

## [0.1.13b8] — 2026-06-12


### Added
- **省心模式 (Easy mode)** — a per-Flow toggle at the top of the Flow editor
  (persisted in the spec's `variables` under `csflow.easy_mode`). When ON, a
  **manually** triggered run behaves like a scheduled run — agents self-merge
  their changes into the baseline branch in-task and the user merge-review +
  complaint phases are skipped — while **human checkpoints are still reviewed by
  you**. Enabling it shows a confirmation notice. Mirrored in the log-portal
  "任务流编排" editor (the flag rides along in the saved spec).

### Changed
- The manual run-trigger (`POST /api/flows/{id}/runs`) now sets
  `FlowRun.is_scheduled` from the Flow's easy-mode flag. Timed schedules are
  unchanged (they already set the flag and never consult it).
- The manual-checkpoint **rerun** prompt now distinguishes auto-merge mode: in
  easy-mode/scheduled runs it instructs the agent to commit and self-merge the
  re-executed task into the baseline branch (with post-merge absolute paths);
  normal manual-merge runs are unchanged.

### Fixed
- **macOS / tilde repo paths**: a leading `~` in an agent `repo` (e.g.
  `~/342test`) is now expanded before it is used as a subprocess `cwd` or passed
  as `clawteam --repo`, at both the spawn boundary (`clawteam_cli`) and the
  leader-decompose CLI dispatch (`task_decompose`). Previously this raised
  `FileNotFoundError` (no shell expands `~`).
- **Crash recovery without a worktree**: an agent whose initial (prewarm) spawn
  failed sits in `Crashed` with no recorded worktree; the scheduler now does a
  fresh spawn (recreating the worktree) instead of dead-ending on resume with
  "cannot resume without recorded worktree".
- **macOS native directory picker**: "选择目录" / workspace-directory pickers now
  use AppleScript `choose folder` on macOS instead of failing with "No GUI
  display found for native directory picker" (macOS uses Aqua/Cocoa, not X11, so
  it has no `DISPLAY`/`WAYLAND_DISPLAY`). Linux X11/Wayland behavior is unchanged.
- **Upgrade `is_temporary` backfill**: an idempotent migration backfills
  `FlowAgent.is_temporary` on pre-0.1.13 Flow specs — unregistered Hermes and
  all Claude/Codex/Cursor agents become temporary; registered Hermes become
  non-temporary — so legacy temporary agents render in the editor and spawn
  correctly (no stale `-p <profile>` binding). The Flow editor also defaults a
  missing flag to temporary and the Hermes pickers fall back to showing the
  current id when it isn't in the managed list.

## [0.1.13b7] — 2026-06-12

### Added
- **省心模式 (Easy mode)** — a per-Flow toggle at the top of the Flow editor
  (persisted in the spec's `variables` under `csflow.easy_mode`). When ON, a
  **manually** triggered run behaves like a scheduled run — agents self-merge
  their changes into the baseline branch in-task and the user merge-review +
  complaint phases are skipped — while **human checkpoints are still reviewed by
  you**. Enabling it shows a confirmation notice. Mirrored in the log-portal
  "任务流编排" editor (the flag rides along in the saved spec).

### Changed
- The manual run-trigger (`POST /api/flows/{id}/runs`) now sets
  `FlowRun.is_scheduled` from the Flow's easy-mode flag. Timed schedules are
  unchanged (they already set the flag and never consult it).
- The manual-checkpoint **rerun** prompt now distinguishes auto-merge mode: in
  easy-mode/scheduled runs it instructs the agent to commit and self-merge the
  re-executed task into the baseline branch (with post-merge absolute paths);
  normal manual-merge runs are unchanged.

### Fixed
- **macOS / tilde repo paths**: a leading `~` in an agent `repo` (e.g.
  `~/342test`) is now expanded before it is used as a subprocess `cwd` or passed
  as `clawteam --repo`, at both the spawn boundary (`clawteam_cli`) and the
  leader-decompose CLI dispatch (`task_decompose`). Previously this raised
  `FileNotFoundError` (no shell expands `~`).
- **Crash recovery without a worktree**: an agent whose initial (prewarm) spawn
  failed sits in `Crashed` with no recorded worktree; the scheduler now does a
  fresh spawn (recreating the worktree) instead of dead-ending on resume with
  "cannot resume without recorded worktree".
- **macOS native directory picker**: "选择目录" / workspace-directory pickers now
  use AppleScript `choose folder` on macOS instead of failing with "No GUI
  display found for native directory picker" (macOS uses Aqua/Cocoa, not X11, so
  it has no `DISPLAY`/`WAYLAND_DISPLAY`). Linux X11/Wayland behavior is unchanged.
- **Upgrade `is_temporary` backfill**: an idempotent migration backfills
  `FlowAgent.is_temporary` on pre-0.1.13 Flow specs — unregistered Hermes and
  all Claude/Codex/Cursor agents become temporary; registered Hermes become
  non-temporary — so legacy temporary agents render in the editor and spawn
  correctly (no stale `-p <profile>` binding). The Flow editor also defaults a
  missing flag to temporary and the Hermes pickers fall back to showing the
  current id when it isn't in the managed list.

## [0.1.13b6] — 2026-06-12


### Added
- Timed Flow runs are now unattended end-to-end. A new `FlowRun.is_scheduled`
  flag marks schedule-triggered runs; every dispatched subtask (workers **and**
  the leader summary) is instructed to **self-merge its changes into the baseline
  branch** and to reference deliverables by their **post-merge absolute path**
  (under the baseline workspace, not the worktree).
- Timed Flows page now shows a note that scheduled runs auto-merge into the local
  baseline branch and skip the user-complaint phase.

### Changed
- For scheduled runs, finalize **skips** the user merge-review
  (`awaiting_user_review`) and user-complaint (`awaiting_user_complaint`) phases
  entirely and settles the run directly to `completed` — so timed schedules
  (especially serial ones) no longer block waiting on human input. Manual runs
  are unchanged.
- Additive DB column `flowrun.is_scheduled` (default `false`) added via an
  idempotent `init_schema` migration, so upgrade-only users converge with
  fresh deploys and old rows load as non-scheduled.

## [0.1.13b5] — 2026-06-11


### Added
- Flow editor now preserves unsaved edits across sidebar-tab navigation (per-flow
  session draft, restored on return; cleared on Save and on Cancel). Chat
  composer drafts (OpenClaw/Hermes) also persist across navigation.
- Auto-upgrade modal renders a full-screen, fully-opaque blocking backdrop that
  masks everything (including the sidebar) during the upgrade/restart.
- Hermes agent **Model** settings show example placeholders (model / provider /
  base URL / API key) and a note that each agent's model config is independent.
- Upgrade step `backfill_hermes_inference_config` — absent-only backfill of Hermes
  profile inference config (never overwrites a user's custom `config.yaml`/`.env`).
- Guidance in README (en/zh) and the docs-site FAQ: ensure each agent CLI is
  authenticated and runnable before use; on auth errors, check the CLI's own
  model/provider config first.

### Changed
- Backend responsiveness: blocking subprocess/filesystem work is offloaded off the
  asyncio event loop (OpenClaw agent create/delete/restore/import, `openclaw.json`
  gateway sync, hook enable/disable, per-chat-turn git commit, self-upgrade
  launch), so one operation no longer freezes every other UI tab.
- Hermes agent management page intro replaced with a model-config explanation
  (each agent's model config is independent and does not auto-update from the root
  profile).
- Flow editor: choosing an existing owner offers every supported CLI kind (incl.
  cursor); in-flow temporary agents remain selectable as sub-task owners.
- Hermes agent-list reconcile no longer fans out O(N) `hermes` CLI subprocesses
  (single `hermes profile list` per request).

### Fixed
- Flow editor "AI 拆解" stayed disabled for a freshly-created leader; the leader
  Agent name is now correctly required for all leader sources (temporary/existing).

### Removed
- **Persistent Claude/Codex (env-home managed) agent platform removed entirely**:
  `/api/managed/*` endpoints, the Claude/Codex management pages + routes + nav, the
  `ManagedAgent` model/storage, the `managed_runtime` config-home injection, and the
  `0.1.13b1` provision migration. Claude/Codex/Cursor are now **temporary-only**
  Flow agents (like Cursor): they spawn against the operator's own default CLI
  config (`~/.claude` / `~/.codex`), with no persistent management, no per-agent
  config home, and no managed-existence validation.

## [0.1.13b4] — 2026-06-11


### Added
- **Required "Agent Name" distinct from Profile ID** for Hermes, Claude and Codex
  agent creation. The create dialogs now have a separate **Agent Name** field
  (free text) and a **Profile ID** field (the technical id); the name is injected
  into the Hermes bootstrap prompt and the managed `CLAUDE.md`/`AGENTS.md` role
  doc. The agent card shows the name as title with the id beneath.
- **Real single-agent session continuity.** Direct (non-Flow) chat turns now
  resume the same conversation instead of starting fresh each turn: Claude uses a
  deterministic `--session-id`/`--resume`, Codex uses `exec resume --last`, Hermes
  uses `-c` (continue). Each turn still enters the agent's role (Hermes `-p`,
  managed via per-agent home dir). `/reset` clears history (managed rotates the
  session revision) so the next turn genuinely starts a new session.
- **Edit-team for Hermes/Claude/Codex agents** — the chat header now has a
  *Change team* control (modal) like OpenClaw, reusing the existing PATCH
  endpoints.
- **Two-tier runtime probe for Claude/Codex** (`?mode=fast|full`, mirroring
  Hermes): `fast` is presence-only (instant render), `full` runs `<cli> --version`
  in the background. The unavailable screen now has a **Refresh** button and CLI
  install instructions.
- **Modeless modals:** dialogs no longer grey out / block the whole app. The
  overlay covers only the content area (the sidebar stays clickable), so you can
  switch modules with a modal open. Modal open-state and form contents are
  persisted (sessionStorage) and restored on return; an in-flight agent creation
  that finishes while you're away is reconciled against the backend on return.

### Changed
- **Flow safety guards:** saving a Flow (`PUT /api/flows/{id}`) is now refused
  while it has an active Run (`409 RUNS_IN_PROGRESS`), matching delete; deleting a
  Flow is also refused when a scheduled task references it
  (`409 FLOW_HAS_SCHEDULES`, with the blocking schedule names).

### Fixed
- **OpenClaw "cancel creation" no longer takes ~20s.** The bootstrap subprocess
  is started in its own process group and cancellation/timeout now kills the
  whole group (`killpg`), so it stops writing artifacts immediately and cleanup
  converges in ~1s.
- **No leftover bootstrap/creation processes on shutdown.** Long-running
  bootstrap/create subprocesses are tracked in a registry and killed by group on
  graceful shutdown / `csflow stop` / restart / uninstall.
- **Connection errors show the real reason.** When the ClawsomeFlow backend is
  unreachable (`TypeError: Failed to fetch`), the agent pages now say "Can't reach
  the ClawsomeFlow service" instead of "platform not detected / unavailable".
- **Agent removal blocked-by-Flow reason** now lists the blocking Flow names in
  the dialog.

### Security
- **Settings-cache data race fixed.** The OpenClaw agent-settings cache, which is
  read/written/invalidated from sync (threadpool) request handlers, is now guarded
  by a lock — preventing lost updates and `dictionary changed size during
  iteration`. (A full concurrency audit found all other shared state already
  correctly synchronized.)

## [0.1.13b3] — 2026-06-10


### Added
- **Cancellable agent creation (取消创建) on every platform.** Hermes creation
  runs a self-definition bootstrap that can take minutes; the create dialog now
  exposes a working **Cancel creation** button throughout. Hermes bootstrap runs
  as a *killable* subprocess and `POST /api/hermes/agents/{id}/cancel-create`
  terminates it and rolls back the half-built profile + DB row
  (`409 AGENT_CREATE_CANCELLED` on the create path). Managed (Claude/Codex/
  Cursor) creation is fast (no bootstrap), so its
  `POST /api/managed/agents/{id}/cancel-create` rolls back the row if one was
  created. Both modals abort the in-flight request via `AbortController`,
  mirroring OpenClaw.
- **Two-tier Hermes runtime probe** (`GET /api/hermes/agents/runtime/status?mode=fast|full`):
  `fast` is a presence-only `which` check (instant) so the WebUI renders right
  away; `full` actually runs `hermes --version` (generous 30s timeout) to confirm
  the binary works. The UI shows on `fast`, verifies with `full` in the
  background, and re-blocks only on a genuine failure — with a **Refresh** retry
  button on the unavailable screen.
- **New managed agents inherit the operator's inference config.** A fresh Hermes
  profile now inherits `config.yaml` + `.env` (model + API keys) from the
  active/root profile; a fresh Codex home inherits `config.toml` + `auth.json`
  from `~/.codex`. Idempotent, never clobbers, never copies SOUL.md/memories
  (operator identity stays private; `.env`/auth files are chmod 0600). An upgrade
  backfill (`run_upgrade` step 0b) seeds existing managed Codex homes.
- **Chat UX polish:** reusable `ChatBubble` rendering, per-platform agent
  avatars, and Enter-to-send (Shift+Enter for newline, IME-composition aware)
  across the OpenClaw/Hermes/managed chat pages.

### Changed
- **Hermes is the single source of truth for which agents exist.** The
  management list now reconciles **bidirectionally** against live Hermes
  profiles — adopting new profiles *and* pruning DB rows whose profile is gone —
  instead of only adopting. Pruning is guarded: it runs only when
  `hermes profile list` succeeds, so a transient CLI failure never wipes valid
  rows. The DB row remains a metadata layer (team/owner/display-name/nl_prompt),
  not a second source of truth.

### Fixed
- **Local Hermes no longer wrongly reported "Hermes 不可用".** Availability was
  gated on `hermes --version` finishing within 5s, but that command runs a
  synchronous update-check that can take longer, so a perfectly usable binary was
  marked unusable. Presence on PATH is now the availability signal; the version
  string is best-effort.
- **New Hermes agents now actually write their SOUL.md.** The self-definition
  bootstrap previously failed with "No inference provider configured" because the
  freshly-created profile had no model/keys — `-p <id>` correctly selected the
  new (empty) profile. Seeding the inference config (see Added) fixes the
  bootstrap and makes the agent usable for later chat/task dispatch.
- **"Ghost" agents removed.** An agent whose Hermes profile was deleted outside
  the platform no longer lingers in the management list (see the bidirectional
  reconcile in Changed).

## [0.1.13b2] — 2026-06-10


### Added
- **Temporary (ad-hoc) agents in the Flow editor.** The owner/leader picker now
  has a source toggle: **"Choose existing agent"** selects a registered
  persistent agent (the owner-type select acts as a filter across
  OpenClaw/Claude/Codex/Hermes, with a per-kind dropdown of registered agents);
  **"Create new agent"** spins up a *temporary* agent of any non-OpenClaw kind by
  free-typed name. Temporary agents (`FlowAgent.is_temporary`, additive/default
  `false`) are not registered, get **no** ClawTeam profile and **no** `-p`/env
  injection at run time, and skip the managed/Hermes existence check; the
  no-duplicate-name rule still applies. OpenClaw can never be temporary.
- **Create-skill for Hermes / Claude / Codex agents** (Agent settings → Skills):
  `POST /api/hermes/agents/{id}/settings/skills` and
  `POST /api/managed/agents/{id}/settings/skills` write a user-defined
  `skills/<name>/SKILL.md`; the settings UI gains a "New skill" form.

### Changed
- **Hermes management loads every profile uniformly** — the separate "Claim
  existing" step is gone; on listing, any on-disk Hermes profile not yet in the
  DB is auto-adopted. The create form is a single required **"Agent name
  (Profile id)"** field used verbatim as the profile id, and the team field can
  create a new team inline.
- **Hermes / Claude / Codex management pages aligned with the OpenClaw Agent
  page**: full-width brand layout, card/list view toggle, team-grouped cards;
  the chat working-directory gains a manual text input defaulting to `~`.
- Flow-editor "pick an existing agent" copy is now kind-neutral (was
  Hermes-specific).
- Agent Store stays in the sidebar but is **not open to the public yet** —
  clicking it shows a "Coming soon" notice instead of routing to `/store`.
- Managed-agent removal warning no longer mentions the ClawTeam profile.

### Fixed
- **Sidebar logo & favicon now render in the packaged wheel.** Root-level static
  files (`/logo.png`, `/agent-icons/*`, favicon) were falling through the SPA
  catch-all and returning `index.html`; they are now served from the bundle
  root via `FileResponse` (path-traversal guarded), with `/assets/*` unchanged.

## [0.1.13b1] — 2026-06-10


### Added
- **"My Team" — Hermes Agent management**: create/claim/delete managed Hermes
  agents (each maps to a `hermes -p <profile>`), single-agent chat with a
  working-directory picker, a `my-profile` button, and settings (SOUL.md /
  model & secrets / skills / cron). Flow tasks bind the executor via `-p <id>`.
- **"My Team" — Claude / Codex Agent management**: managed agents whose
  identity/skills/MCP live in a relocatable config home (`CLAUDE_CONFIG_DIR` /
  `CODEX_HOME`), injected at spawn through a ClawTeam runtime profile so tools
  follow the agent independent of the working directory. Pages, CRUD, chat
  (working-dir picker), and settings (role doc / MCP / skills).
- New backend modules + APIs: `/api/hermes/agents`, `/api/managed/agents`
  (CRUD, runtime status, settings, SSE chat), `HermesAgent` / `ManagedAgent`
  tables + storage.
- Direction-safe upgrade architecture: applied-migrations ledger
  (`.csflow-migrations.json`) + high-watermark version marker, so switching
  between stable and beta builds (or downgrade-then-reupgrade) runs each
  migration exactly once. Non-fatal repair failures are surfaced to the
  terminal (`repair_warnings`) without aborting the upgrade.
- Upgrade migration that provisions managed-agent records/profiles for
  Hermes/Claude/Codex agents already referenced by existing Flow templates.

### Changed
- **Subtask timeout default & floor raised to 4h (14400s)** (`FlowTask`
  default, scheduler floor `max(task.timeout_seconds, 14400)`, Flow-editor
  default + en/zh hint copy).
- Flow editor restricts `hermes`/`claude`/`codex` task owners to **already-managed
  agents** (dropdown, not free-typed ids); the working directory is still set per
  task. Save-time validation rejects unmanaged ids.
- Complaint phase now also dispatches fixes to **Hermes** agents (besides
  OpenClaw); only OpenClaw prompts ask for in-task self-merge — the leader and
  all other prompts contain no merge wording.
- Worktree cleanup unified: manual merge/dismiss no longer deletes the worktree
  immediately; deletion is deferred to the end of the complaint phase
  (manual-merge failures are preserved for manual resolution).
- Runs in `complaint_processing` are shown in the history list (not as an
  in-progress card).

### Fixed
- Upgrade is resilient: a failing migration/repair no longer aborts the whole
  flow (schema init etc. still run so the service stays usable); "already fixed"
  data is treated as success, never an error.

### Removed
- `csflow agents create` / `csflow agents chat` are hidden from the CLI — agent
  creation and chat are done in the Web UI ("My Team"). (Commands still exist,
  just not advertised.)

## [0.1.12] — 2026-06-08


### Changed
- Raised the per-subtask timeout default and runtime safety floor to 7200s
  (120 min): `FlowTask.timeout_seconds` default, the scheduler timeout floor
  (`effective_timeout = max(task.timeout_seconds, 7200)`), the Flow editor
  default, and the timeout hint copy (en/zh). Long-running subtasks are no
  longer falsely flagged as timed out before the two-hour mark.

## [0.1.11] — 2026-06-06


### Added
- Flow template import/export REST API: `GET /api/flows/{id}/export`,
  `GET /api/flows/export` (bulk), and `POST /api/flows/import` (upsert by id,
  optimistic-locked; `?overwrite=true` forces last-write-wins) so an external
  same-host service can pull, edit, and write back Flow definitions.
- `csflow api-token [--rotate]` to read/rotate the local API token.
### Changed
- The public `/api` surface is now guarded by a loopback + bearer-token check
  (OpenClaw gateway paradigm). A long-lived `api_token` is auto-generated at
  init/upgrade and stored privately in `~/.clawsomeflow/config.json`; the
  bundled WebUI keeps working (same-origin) while same-host external callers
  must send `Authorization: Bearer <token>` (see `csflow api-token`). The guard
  is a no-op until the token exists, so behavior is unchanged until a managed
  upgrade provisions it.
### Fixed
- Restored the `ci-fast` gate (red since 0.1.9): build the frontend before the
  editable backend install (hatchling force-includes `frontend/dist`), and stop
  two tracked tests from requiring maintainer-only tooling — `test_release_bump`
  skips when the private `_bump_version.py` is absent, and the run-loop finalize
  test stubs the ClawTeam MCP client instead of spawning the absent
  `clawteam-mcp` binary on CI.
### Security
- `/api` rejects cross-machine (non-loopback `Host`) and unauthenticated
  non-browser callers once an `api_token` is present, reducing exposure of the
  local API to other hosts/processes.

## [0.1.10] — 2026-06-05

### Added
- Added `scripts/stop-contributor.sh` — an isolated stop command for the
  contributor profile started by `scripts/deploy-contributor.sh`. It only acts
  on the contributor dev ports (`17117` / `5174` / `17118`) and refuses to run
  if any dev port collides with the end-user port, so it can never affect the
  managed user service (`17017` / `~/.clawsomeflow`). Documented in the
  contributor section of `readme.md` / `readme.zh.md`.
- Added `reclaim_stale_port_listeners()` (public wrapper around
  `_cleanup_stale_port_conflicts`) in `app.cli._user_service`.
### Changed
- Changed `csflow stop` to also reclaim an orphaned manual `uvicorn app.main:app`
  listener on the configured port as a final fallback (no managed service / no
  PID file), so the official install path can rebind the port cleanly.
- Changed stale-port reclaim to reap the whole descendant tree of a stale dev
  uvicorn (reload worker, multiprocessing resource tracker, and detached
  children such as the `clawteam-mcp` board subprocess that inherited the
  listening-socket fd). Without this, a detached child could keep the socket
  bound and block the managed service from rebinding.
- Changed the Web UI upgrade-failed notice to show the manual upgrade command
  (`csflow upgrade`) so users can recover when self-upgrade can't start.
### Fixed
- Fixed end-user service recovery after a developer runs `deploy.sh source`
  (which leaves an editable install plus a raw uvicorn on `17017`): the official
  install/upgrade CLI now fully reclaims the orphaned dev backend and restores
  the managed systemd/launchd service on `17017`.
### Removed
### Deprecated
### Security

## [0.1.9] — 2026-06-05


### Added
- Added `SUMMARY_NO_DEPENDENCY` flow validation so leader summary tasks must
  depend on at least one upstream task; API create/update paths now reject
  dependency-less summaries with a dedicated validation error.
### Changed
- Changed leader-summary scheduling semantics: the summary task now dispatches
  only after all non-summary tasks reach `completed`, while `depends_on` on the
  summary is used only to select which upstream outputs feed review/report
  context.
- Changed leader summary dispatch inputs to be dependency-scoped: worker
  reports/worktrees now include only configured first-level summary
  dependencies instead of all workers.
- Changed FlowEditor summary behavior from "auto-depend on all worker tasks" to
  explicit dependency selection, with row/form warnings and save-time blocking
  validation when the summary has no dependencies.
- Changed task-id rename handling in FlowEditor to remap `dependsOn` references
  so dependency links stay consistent after task ID edits.
- Changed OpenClaw worker completion guidance to clarify that workspace writes
  are required only when the task actually needs file/content changes.
### Fixed
- Fixed summary prompt context leakage where unrelated worker reports/worktrees
  could be injected into leader summary dispatch when not listed in summary
  dependencies.

## [0.1.8] — 2026-06-04


### Added
- Added per-agent worktree preservation tracking
  (`_csflow_preserve_worktree_agent_ids` on `FlowRun.inputs`): a successful
  manual merge decision now cleans up that agent's worktree immediately, while a
  failed merge keeps its worktree for follow-up. Terminal cleanup of
  `completed_with_conflicts` runs preserves only the still-needed worktrees and
  cleans the rest.
- Added acknowledgements and a WeChat community section (Star ask + discussion
  group QR + OPC interest) to both `readme.md` and `readme.zh.md`, with a
  "WeChat Community / 微信交流群" quick-access nav link. QR asset lives at
  `docs/assets/wechat-group-qr.png`.
- Added a "Skill configuration" row to the ClawTeam-vs-ClawsomeFlow comparison
  table (ClawsomeFlow needs no extra Agent-platform skill setup).

### Changed
- Leader summary dispatch prompts no longer name the baseline workspace and only
  surface each worker's worktree path/branch. Naming the baseline tempted the
  leader to pre-copy its report into the project tree, which aborted
  `clawteam workspace merge` ("untracked working tree files would be
  overwritten") and ended runs as `completed_with_conflicts`. The deliverable
  step is now kind-specific (OpenClaw `my-desktop/` convention vs TUI in-worktree
  structure).
- The complaint phase now instructs **every** leader (OpenClaw and TUI) to
  self-merge into the baseline, since it is the final stage with no subsequent
  user-review/merge step.
- Codex TUI spawns now apply per-process `-c` overrides
  (`notice.model_migrations={}`, `tui.model_availability_nux={}`,
  `disable_paste_burst=true`) so the unattended TUI skips startup onboarding
  menus and submits injected dispatches instead of stalling on paste-burst.
- Stopped hardcoding per-CLI skip-permission/bypass flags that ClawTeam's
  adapter already injects (avoids duplicate-argv hard-errors on codex); `claude`
  now only carries `--permission-mode bypassPermissions` (root-safe).
- TUI readiness detection now recognizes the modern codex banner
  (`>_ OpenAI Codex (vX.Y.Z)`) and `›` composer prompt, preventing
  `session_prewarm_failed` timeouts on newer codex builds.
- Reworked the decompose modal close/cancel UX: a single confirm dialog gates
  both cancel and close while a request is in flight.

### Fixed
- Fixed the decompose modal cancelling its own in-flight request before the
  request id was stored: `useSessionBackedState` now keeps `isClosed` behind a
  ref so the returned setter is referentially stable and dependent effects no
  longer re-run every render.
- Fixed stale decompose status by polling `GET /api/flows/decompose/{id}` with
  `cache: "no-store"`.

## [0.1.7] — 2026-06-03


### Fixed
- Fixed Flow deletion failing with foreign-key constraint errors when historical
  terminal runs existed: delete now purges terminal `FlowRun`/`RunEvent` history
  first, and returns `409 RUNS_IN_PROGRESS` on active-run race conditions.

## [0.1.6] — 2026-06-03


### Changed
- Reworked the Chinese `readme.md` with a centered, tech-styled title card,
  a complete product overview (including the ClawTeam capabilities it inherits),
  a "Full compatibility with ..." line, an "其他 Agent 编排平台对比" table, and a
  ClawTeam-vs-ClawsomeFlow comparison table. Removed architecture diagrams and
  kept formal-release install instructions only (no upgrade commands).
- Migrated the canonical Git remote to
  `git@github.com:revalue-coding/ClawsomeFlow.git` with a fresh repository
  history; `main` and `dev` each start from a single initial commit.
### Fixed
- Fixed the maintainer release script (`scripts/release.sh`) aborting at the
  commit step when it tried to `git add` the intentionally-private
  `CHANGELOG.md`. The release commit now stages only the version-literal files
  (`backend/app/__init__.py`, `backend/pyproject.toml`,
  `frontend/package.json`); the changelog is still cut locally and reused as the
  GitHub Release body, but never committed to the public repository.
### Removed
- Stopped tracking maintainer-only docs in the public repository
  (`API.md`, `DEV.md`, `OPEN_SOURCE_DOC_EXPOSURE_CHECKLIST.md`,
  `SERVER_MODE_MULTIUSER_AUDIT.md`, `readme-copy.md`, and this `CHANGELOG.md`);
  they are retained locally via `.gitignore` and not published externally for now.

## [0.1.6b4] — 2026-06-01


### Added
- Added `backend/tests/test_openclaw_tmux_session.py` to cover
  platform-specific OpenClaw tmux dispatch paths (macOS message-file
  injection, Linux inline injection) and temp-file cleanup on inject failure.
- Added readiness/restore regression tests for TUI resume behavior
  (`No conversation found to continue` fast-fail signal and
  resume->fresh fallback verification).
### Changed
- Changed OpenClaw tmux dispatch strategy to be platform-specific:
  macOS now uses message-file substitution for risky payloads (multi-line
  or >=4096 chars), while Linux/non-macOS keeps historical inline
  `--message '<quoted>'` injection.
- Changed scheduler resume recovery for TUI agents: when native
  `--continue/--resume` cannot restore a session, recovery now falls back
  to a fresh CLI spawn on the existing worktree instead of failing the run.
- Changed run completion timestamp semantics: `FlowRun.finished_at` is now
  stamped at orchestration completion (leader summary done), including
  transitions to `awaiting_user_review` and `awaiting_user_complaint`.
- Changed session-backed frontend state writes to immediate write-through
  persistence to avoid modal state races during fast route transitions.
### Fixed
- Fixed "Start execution" and related modal re-open issues after navigation
  by eliminating delayed sessionStorage persistence races.
- Fixed macOS OpenClaw task-dispatch stalls where shell input got stuck in
  continuation mode (`>` prompt) when injecting long quoted payloads.
- Fixed crash-recovery runs terminating on resume startup failure when no
  prior conversation exists for `--continue`.
- Fixed run-loop error reporting so session-startup failures are logged as
  explicit startup failures instead of generic unhandled-loop exceptions.

## [0.1.6b3] — 2026-06-01


### Added
- Added shared `task_decompose_validation` module so decompose proposal
  invariants are enforced in one place for both commit callbacks and
  non-OpenClaw stdout parsing.
- Added non-OpenClaw decomposition stdout JSON extraction (plain JSON,
  fenced blocks, and embedded object fallbacks) with inline validation
  before marking requests succeeded.
- Added in-modal decompose close/cancel confirmation (replacing
  `window.confirm`) and decompose result count i18n strings.
- Added scrollable modal layout with viewport max-height and safe-area
  padding so long decompose/create dialogs stay usable on small screens.
- Added Claude Code and Codex rows to installer runtime capability
  summaries, aligned with `csflow start` / `doctor` agent checks.
### Changed
- Changed non-OpenClaw AI decomposition from a curl callback protocol to
  direct stdout JSON: the leader CLI now returns one JSON object, the
  server parses/validates it, and the request transitions to `succeeded`
  without waiting for `/api/internal/task-decompose/commit`.
- Changed non-OpenClaw decompose prompts to forbid shell/file/curl side
  effects and require a single JSON payload on stdout.
- Changed Cursor runtime detection to probe the `agent` binary with
  actionable bootstrap hints (`cursor agent --help`, macOS app-bundle
  fallback) instead of treating a missing/unresponsive binary as installed.
- Changed agent-runtime summary copy from "Not installed" /
  "install required" to "Unavailable" / "setup required" when a binary is
  missing or fails command probes.
- Changed Profiles, OpenClaw Chat list, and Scheduled Flows execution
  detail strings to use i18n keys (Base URL column, agent ID column, run
  label, profile name placeholder).
### Fixed
- Fixed Cursor dependency checks reporting available when `agent` exists
  in PATH but `--version` / `--help` probes fail.
- Fixed non-OpenClaw decomposition leaving requests stuck at `dispatched`
  when leaders could not execute callback curl steps under restricted
  permissions.

## [0.1.6b2] — 2026-06-01


### Added
- Added session-backed persistence for AI decomposition `requestId` and OpenClaw
  create-popup progress/cancel state so in-flight operations survive refresh and
  cross-route navigation.
- Added contextual in-progress button labels (`Deleting…`, `Removing…`,
  `Restoring…`, `Cancelling…`) across Flow delete, Profile delete, agent
  remove/restore, and decompose/create-cancel actions.
### Changed
- Changed AI decomposition cancel UX: the cancel action is available whenever a
  request is in flight (removed the 30-minute unlock countdown panel), and
  closing the modal while decomposition is active now prompts for confirmation
  and cancels the backend request when confirmed.
- Changed OpenClaw agent-create popup cancel UX to match: removed the 5-minute
  unlock hint panel, kept the cancel button visible for the full create window,
  and tightened busy/dismiss rules so the modal cannot be dismissed while create
  or cancel cleanup is running.
- Changed decomposition startup to wait for hydrated Flow fields (goal, leader,
  repo) before firing the backend request, and to re-attach polling on refresh
  instead of starting a duplicate run when a persisted `requestId` exists.
### Fixed
- Fixed decomposition modal accidentally starting duplicate backend requests
  after refresh or React StrictMode effect replay by keying startup on the full
  logical run signature and skipping auto-start when a persisted request is
  already active.
- Fixed OpenClaw create-progress popup showing blank text after refresh by
  restoring session-backed status copy and falling back to the running label
  while the popup is open.
### Removed
- Removed decomposition and OpenClaw create cancel countdown hint panels from
  the modal footers (cancel remains one click away without a timer gate).

## [0.1.6b1] — 2026-06-01


### Added
- Added first-class `cursor` agent support across runtime dependency checks,
  non-OpenClaw decomposition dispatch, TUI session readiness detection, and UI
  owner/agent selectors.
- Added session-backed modal state hooks and wired them into major frontend
  surfaces so dialog/form state survives refreshes and cross-module navigation.
- Added OpenClaw managed-agent create parity helpers to write `agentDir`,
  prepare per-agent `sessions/`, and seed portable auth profiles from the
  default source agent.
### Changed
- Changed non-OpenClaw decomposition execution to direct one-shot CLI dispatch
  with full-permission invocation flags for Claude, Codex, Hermes, and Cursor.
- Changed the update badge to keep showing the currently installed version and
  append an upgrade arrow while retaining highlight/click-to-upgrade behavior.
- Changed update notice dismissal/open persistence to track
  `currentVersion -> latestVersion` pairs so modal reminders recover correctly
  after version transitions.
### Fixed
- Fixed OpenClaw `memory_search` bootstrap failures on newly created managed
  agents by aligning auth portability copy rules (`copyToAgents`, OAuth opt-in,
  default source agent selection, and non-overwrite target behavior).
- Fixed primary module hover URL previews by switching sidebar navigation away
  from anchor links to programmatic route buttons.
- Fixed modal disappearance across page refresh and module switching by
  restoring persisted session-scoped state (including per-module last sub-route
  memory).
- Fixed AI decomposition/create-agent cancellation UX so Cancel buttons are
  immediately clickable (no delayed unlock timer).

## [0.1.5] — 2026-05-31


### Changed
- Re-scoped `csflow upgrade` as the user-facing stable upgrade entrypoint: it
  now delegates directly to the hosted upgrader (`upgrade.sh`) instead of
  acting as a local-only reconcile command.
- Split the previous local reconcile behavior into hidden
  `csflow upgrade-runtime`, and migrated installer/deploy/dev call sites
  (`install-user.sh`, `upgrade-user.sh`, `install_clawsomeflow.sh`,
  `deploy.sh`, `run-dev-bg.sh`) to use that internal command explicitly.
- Updated upgrade guidance/help copy to make the stable remote CLI path
  explicit and to clarify PEP 668 safety (`~/.clawsomeflow/.venv/bin/pip`).
### Fixed
- Fixed in-app upgrade modal UX by removing the Ubuntu/Debian script hint and
  shell command block, and by adding a timeout-backed failure fallback for
  "Upgrade now" health polling so the page no longer stays blocked indefinitely
  in the upgrading state.
### Removed
- Removed the temporary delegated recursion-guard path for `csflow upgrade`
  (`CSFLOW_UPGRADE_DELEGATED`) after all managed scripts were switched to
  `csflow upgrade-runtime`.

## [0.1.4] — 2026-05-31


### Added
- Added create/decompose cancellation controls: OpenClaw agent creation now
  supports explicit cancel with rollback cleanup, and AI decomposition supports
  user-triggered cancellation for long-running requests.
### Changed
- Promoted the 0.1.4 beta train to the stable release line, including
  gateway-first OpenClaw managed-agent sync (`config.get`/`config.set`) with
  cross-platform (Linux/macOS) preference plus atomic file fallback.
- Updated non-OpenClaw AI decomposition dispatch to one-shot direct CLI
  execution (`claude -p`, `codex exec`, `hermes -z`) instead of temporary
  ClawTeam team/tmux orchestration in design-time flows.
### Fixed
- Fixed OpenClaw managed-agent bootstrap reliability by combining stricter
  post-registration readiness checks, protocol-compatible gateway config writes,
  and a shorter fail-fast readiness timeout window.
- Fixed cancellation and cleanup race windows to keep registration state,
  workspace artifacts, and UI-visible agent/run state consistent after aborts.

## [0.1.4b5] — 2026-05-31


### Changed
- Updated OpenClaw config sync policy to prefer gateway online updates
  (`config.get` + `config.set`) on both Linux and macOS, with existing
  atomic file persistence kept as fallback.
- Reduced the OpenClaw "agent ready" wait timeout during create bootstrap from
  60s to 15s (environment override clamp now 5s~15s).
### Fixed
- Fixed OpenClaw gateway config write compatibility: `config.set` now sends
  `raw` as JSON text (string) and reuses `baseHash` extracted from real
  `config.get` payload shapes where `raw` can be null.
- Fixed potential runtime/registry divergence around managed-agent
  create/remove updates by ensuring the same gateway-first save path is used
  before file fallback.

## [0.1.4b4] — 2026-05-31


### Added
- Added `POST /api/openclaw/agents/{agent_id}/cancel-create` plus OpenClaw Chat
  create-modal controls to unlock "Cancel create" after 5 minutes, with
  best-effort rollback/purge of registration, DB row, and residual workspace
  artifacts before the modal closes.
- Added `POST /api/flows/decompose/{request_id}/cancel` plus FlowEditor
  decompose-modal controls to unlock "Cancel decomposition" after 10 minutes so
  users can terminate long-running decomposition conversations.
### Changed
- Changed OpenClaw managed-agent bootstrap to perform a strict
  "gateway-recognizes-agent" readiness probe on macOS only (up to 60s) before
  bootstrap chat begins; Linux and other platforms keep the non-blocking path.
- Changed non-OpenClaw AI decomposition dispatch from ClawTeam team/tmux
  orchestration to direct one-shot CLI sessions (`claude -p`, `codex exec`,
  `hermes -z`) with no ClawTeam team lifecycle during Flow design time.
### Fixed
- Fixed create-cancel race handling so early cancel requests are latched and
  honored even when cancellation arrives before the backend registers the
  in-flight create event.
- Fixed non-OpenClaw decomposition abnormal convergence by cancelling in-flight
  subprocesses on user cancel/TTL timeout and avoiding team/workspace cleanup
  side-effects for decomposition cancellation.

## [0.1.4b3] — 2026-05-30


### Fixed
- Reduced OpenClaw managed-agent create/import bootstrap race failures by
  retrying short-lived ``unknown agent id`` responses during the
  post-registration gateway hot-reload window, while keeping regular chat
  sessions fail-fast for truly unknown agents.

## [0.1.4b2] — 2026-05-30


### Changed
- Prioritized OpenClaw runtime/CLI binding to the actively running service on
  default gateway port `18789`: runtime status probes and executable resolution
  now prefer that service first, and only fall back to npm-prefix/PATH
  candidates when no usable `18789` service is found.
- Aligned `deploy.sh` OpenClaw readiness checks with the same `18789`-first
  policy (`/health` first, listener-to-CLI path inference second, environment
  fallback last) so deployment-time behavior matches backend runtime behavior.
### Fixed
- Reduced OpenClaw runtime false positives in fast checks by removing the
  previous socket-only success path and requiring health-confirmed probe
  outcomes before unlocking runtime-available UX flows.

## [0.1.4b1] — 2026-05-30


### Fixed
- Reduced scheduler test/runtime noise by closing startup coroutine objects when
  task creation fails (prevents intermittent
  `RuntimeWarning: coroutine ... was never awaited` from leaked startup coroutines).
- Improved stable-channel installer UX: pinned-latest attempt failures are now
  retried quietly with a friendly explanation instead of exposing transient
  pip `No matching distribution found` errors before a successful fallback.
- Added deploy/upgrade compatibility gating for OpenClaw: `csflow install/init`
  and `csflow upgrade` now fail fast with an upgrade hint when installed
  OpenClaw is below the minimum required version (`2026.5.12`).
- Made OpenClaw runtime probing and OpenClaw Chat link generation port-adaptive:
  when users change OpenClaw gateway port, ClawsomeFlow now resolves the
  effective gateway URL from OpenClaw config (with config override support)
  instead of assuming `18789`.
- Updated OpenClaw CLI resolution to prefer `$(npm prefix -g)/bin/openclaw`
  in the current service-process environment before generic PATH lookup, which
  keeps runtime command selection aligned with the user's actively installed npm
  OpenClaw channel in most real deployments.
- Removed automatic OpenClaw gateway start/restart from ClawsomeFlow runtime
  guards and `deploy.sh`; deployment/upgrade now fail fast with manual guidance
  so ClawsomeFlow never auto-launches a potentially different OpenClaw version
  than the one the user is currently operating in WebUI.

## [0.1.3] — 2026-05-30


### Fixed
- Fixed stable-channel install/upgrade targeting so scripts now resolve the
  latest stable version from package metadata and install
  `clawsomeflow==<latest-stable>` when available (with fallback to default
  stable upgrade if metadata lookup fails).

## [0.1.2] — 2026-05-30


### Changed
- Unified user install/upgrade channel policy for remote scripts:
  `install.sh` and `upgrade.sh` now default to the stable channel (no `--pre`)
  and only follow beta/rc when `--pre` is explicitly provided.
### Fixed
- Fixed default remote install behavior for beta deployments:
  `curl -fsSL https://clawsomeflow.com/install.sh | bash` now always runs the
  stable upgrade/install flow directly instead of aborting on pre-release
  candidate checks.
- Fixed manual upgrade channel control by adding `upgrade.sh --pre` support so
  operators can explicitly move to the latest beta/rc, while plain `upgrade.sh`
  remains stable-only.

## [0.1.1] — 2026-05-30


### Changed
- Changed `csflow upgrade` confirmation behavior to default auto-confirm
  (equivalent to `--yes`); use `--no-yes` if interactive confirmation is needed.
- Updated user upgrade guidance/script to use
  `pip install -U clawsomeflow && csflow upgrade` (without mandatory `--yes`).
- Localized user-facing and agent-facing runtime copy to English across dispatch
  prompts, OpenClaw bootstrap prompts, common agent rules/skills/cron templates,
  and deployment/runtime CLI output.
- Updated AI decomposition preflight for non-OpenClaw leaders to validate
  workspace repo readiness earlier and trigger guided auto-remediation when needed.
- Changed `scripts/run-dev-bg.sh` so Vite is opt-in (`CSFLOW_START_VITE=1`)
  instead of auto-starting by default.
### Fixed
- Fixed AI decomposition timeout handling by aligning the backend callback token
  TTL to 10 minutes and adding a frontend 10-minute timeout failure state with retry.
### Removed
- Removed the terminal tail output "Agent runtime capability summary" (and its OpenClaw/Hermes
  shell checks) from `scripts/install-user.sh`.

## [0.1.1b15] — 2026-05-30


### Fixed
- Fixed agent commit/cron flows failing with ``common rules content is empty`` when
  the runtime ``~/.clawsomeflow/.common-agent-source/`` mirror was stale or blank
  by always loading ``agent-common-rules.md`` from the bundled source tree.

## [0.1.1b14] — 2026-05-29


### Added
- Added `scripts/publish-site-repo.sh` and `release.sh --sync-site-repo` /
  `--site-push` to optionally commit (and push) synced `install.sh` /
  `upgrade.sh` into the clawsomeflow.com website repository after each release.
### Fixed
- Fixed `test_install_user_script` failures when port 17017 is already in use by
  binding installer tests to an ephemeral local port via `CSFLOW_PORT`.

## [0.1.1b13] — 2026-05-29

### Added
- Added in-app upgrade notices with PyPI update checks, one-click upgrade script
  links, and `scripts/upgrade-user.sh` for isolated-runtime upgrades.
- Added `POST /api/system/open-directory` so local-mode users can open agent
  worktrees in the system file manager from the UI.
- Added `complaint_failed` run status for complaint-phase terminal failures,
  with worktree-preserving tail cleanup when that status is reached.
- Added ordered background TUI session prewarm after the first successful task
  dispatch, plus `session_prewarm_failed` / `task_session_start_failed` events
  for observability without failing the run on prewarm alone.
### Changed
- Made OpenClaw deploy optional and runtime-gated: install/upgrade no longer
  auto-restore OpenClaw registrations; the OpenClaw page unlocks via fast-then-
  strict runtime probes instead of blocking deploy paths.
- Speeded up OpenClaw runtime gating and kept Flow saves non-blocking with
  save-time warnings plus upgrade/cron refresh hardening.
- Prioritized first pending task dispatch before warming other owners; owners in
  `Spawning` skip only their own ready tasks for the current tick.
- Tightened complaint merge routing: non-complained agents receive merge
  requirements; leader dispatch failures no longer emit user-visible skip events.
- Improved run-board visibility for complaint and terminal states, including
  `complaint_failed` labels in the UI.
- Supported non-OpenClaw leader workflows with safer review handoff semantics.
- Clarified manual checkpoint API/DEV documentation for post-task checkpoint
  lifecycle, checkpoint event decision enums, and abort-time cleanup behavior.
- Hardened checkpoint settings responsiveness and post-task checkpoint flows.
- Passed `--replace` on crashed TUI `spawn_resume` recovery to clear stale
  ClawTeam runtime records.
### Fixed
- Fixed manual checkpoint rerun rollback: when custom rerun dispatch fails, the
  controller now reverts both ClawTeam and local task state from `in_progress`
  back to `completed`, and clears dispatch timeout bookkeeping to avoid a
  stale ~30 minute timeout window.
- Fixed abort behavior during `awaiting_user_checkpoint`: the scheduler now
  emits `task_checkpoint_cleared` with `decision=cancelled` when cancellation
  closes an active checkpoint.
- Preserved custom OpenClaw cron jobs across unregister/restore cycles.

## [0.1.1b12] — 2026-05-21


### Changed
- Deploy/upgrade/bootstrap now enforce OpenClaw readiness before proceeding:
  if OpenClaw is missing, CLI and installer flows fail fast with clear guidance,
  offer auto-install, and stop immediately on install failure.
- OpenClaw checks are now ordered before ClawTeam runtime checks in deployment
  scripts so prerequisite failures surface earlier with actionable errors.
- `csflow start`/`install`/`upgrade` now share a strict OpenClaw runtime guard:
  when OpenClaw is installed but not running, they auto-start the gateway and
  abort if start or health verification fails.
- `install-user.sh`, `deploy.sh`, and `install_clawsomeflow.sh` now include
  macOS-specific bootstrap branches (Homebrew Python/Node fallback + launchd
  startup path) while preserving Linux behavior.
### Fixed
- Improved macOS launchd reliability for service management by handling both
  `gui/<uid>` and `user/<uid>` launch domains, reducing failures in headless
  or SSH-triggered sessions.

## [0.1.1b11] — 2026-05-21


### Changed
- Deployment completion output now includes the currently deployed
  ClawsomeFlow version in `csflow start`, `deploy.sh`, `install-user.sh`, and
  `install_clawsomeflow.sh` to make post-install verification explicit.

## [0.1.1b10] — 2026-05-21


### Added
- Added upstream source fallback for ClawTeam installation by cloning
  `https://github.com/HKUDS/ClawTeam.git` when package-based installation still
  cannot provide the required runtime subcommand.
### Changed
- Unified ClawTeam bootstrap strategy across `csflow start`, `deploy.sh`,
  `scripts/install-user.sh`, and `scripts/install_clawsomeflow.sh` with
  multi-step resolution: configured source override, local `~/ClawTeam`, PyPI,
  then upstream clone fallback.
- Updated dependency install guidance for ClawTeam to point users to a
  source-based install path that guarantees `clawteam runtime` availability.
### Fixed
- Fixed repeated "clawteam installed but missing runtime" loops during
  one-command installs on hosts where PyPI only resolves to `clawteam` 0.2.0.

## [0.1.1b9] — 2026-05-21


### Changed
- Keep `readme.md` as the canonical packaging metadata file for release builds
  while retaining the long-form project documentation in `readme-copy.md`
  during the docs transition.
### Fixed
- Restore top-level `readme.md` in source control so isolated `hatchling`
  builds can always resolve the package readme when creating sdist/wheel in a
  clean checkout.

## [0.1.1b8] — 2026-05-21


### Changed
- Clarified the beta installation path for "one-command" onboarding by
  documenting the PEP 668-safe remote installer entrypoint and when to use
  direct `pip install` vs isolated runtime venv bootstrap.

## [0.1.1b7] — 2026-05-21


### Added
- Introduced deterministic runtime binary resolution (`runtime_bins`) so
  `csflow`/`clawteam` lookups prefer the active runtime entrypoint/venv instead
  of stale PATH leftovers.
### Changed
- Deployment/startup scripts and managed service generation now pin
  `csflow`/`clawteam` execution to the runtime venv path, keeping deploy-time
  and run-time environments consistent without requiring users to activate venvs.
- Installation docs now promote a one-liner `install-user.sh` entrypoint for
  Linux/macOS and explicitly document the PEP 668-safe isolated venv path.
### Fixed
- `clawteam` dependency checks now resolve against the active runtime binary,
  reducing false negatives caused by older globally installed binaries.
- Board proxy startup now performs version-aware handling for existing
  `clawteam board serve` listeners: reuse when compatible, auto-replace
  user-owned mismatched versions, and fail safely on non-owned conflicts.

## [0.1.1b6] — 2026-05-20


### Changed
- During redeploy/upgrade, ClawsomeFlow now bulk re-registers restorable
  managed agents from `~/.clawsomeflow/agents/*/workspace` back into OpenClaw runtime
  before reinstalling skills.
### Fixed
- Agent board visibility now follows runtime registration state: after
  "remove agent (unregister only)", the agent disappears from the main board
  list immediately but remains available in restore candidates.
- Uninstall cleanup now removes legacy ClawsomeFlow runtime agent entries even
  when managed registry metadata is missing, by matching managed workspace paths.
- Reindex no longer backfills "ghost" agents when the referenced workspace path
  does not exist on disk.

## [0.1.1b5] — 2026-05-20


### Changed
- Clean up `csflow start` terminal output by hiding structured JSON deployment
  logs from user-facing CLI output and making Web UI/Service lines the final
  summary.
- Rename "Non-OpenClaw agent tool check" to "Agent runtime check", list OpenClaw first,
  and show its callable state inline with other runtimes.
### Fixed
- Auto-recover service restart failures caused by stale user-owned
  `uvicorn app.main:app` listeners occupying the target port.

## [0.1.1b4] — 2026-05-20


### Changed
- Lower runtime baseline from Python 3.11+ to 3.10+ so users on Python 3.10
  can install via the standard `pip install` flow.

## [0.1.1b3] — 2026-05-20


### Fixed
- Ensure `scripts/release.sh --dry-run` leaves no tracked file edits by
  auto-restoring `CHANGELOG.md` and version files on exit.

## [0.1.1b2] — 2026-05-20


### Fixed
- Fix wheel-from-sdist packaging by force-including repo-level frontend and
  openclaw-agent-source assets in the sdist.

## [0.1.1b1] — 2026-05-20


### Added
- _Add new entries here while developing on `main`. They are cut into a
  versioned section by `scripts/release.sh` at release time._

## [0.1.0] — 2026-05-07

Initial alpha release. Brings the full MVP architecture online:

### Added
- Local mode (`pip install clawsomeflow` + `csflow start`) — SQLite + in-process
  scheduler, zero external infrastructure.
- Server mode (`csflow init --mode server` + systemd) — your own
  PostgreSQL + Redis + nginx, no docker required.
- 13-command `csflow` CLI: lifecycle (`start`/`stop`/`status`/`init`/
  `serve`/`doctor`/`uninstall`) + ops (`flows`/`runs`/`agents`/`logs`).
- Web UI (React 18 + Tailwind): FlowList / FlowEditor (list layout + AI
  Decompose) / RunList / RunDetail (live WebSocket + ClawTeam Board iframe
  + pending merges) / OpenclawAgents (NL create) / OpenclawChat / Profiles.
- Scheduling engine: `FlowScheduler` + `RunController` + `WorkerSession`
  state machine; 4 anti-loop defences hard-enforced in `ClawTeamCli`.
- OpenClaw integration: `clawsomeflow-agent-manager` registration +
  per-agent skills installer + HTTP gateway client + short-lived HMAC
  callback tokens.
- AI Task Decompose: `csflow-task-decomposer` skill in every user
  OpenClaw workspace; `POST /api/flows/decompose` async pipeline.
- 379 backend tests, frontend tsc + vite build clean.

[Unreleased]: https://github.com/clawsomeflow/clawsomeflow/compare/v0.1.13b10...HEAD
[0.1.10]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.10
[0.1.0]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.0
[0.1.1b1]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b1
[0.1.1b2]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b2
[0.1.1b3]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b3
[0.1.1b4]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b4
[0.1.1b5]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b5
[0.1.1b6]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b6
[0.1.1b7]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b7
[0.1.1b8]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b8
[0.1.1b9]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b9
[0.1.1b10]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b10
[0.1.1b11]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b11
[0.1.1b12]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b12
[0.1.1b14]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b14
[0.1.1b15]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1b15
[0.1.1]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.1
[0.1.2]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.2
[0.1.3]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.3
[0.1.4b1]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4b1
[0.1.4b2]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4b2
[0.1.4b3]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4b3
[0.1.4b4]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4b4
[0.1.4b5]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4b5
[0.1.4]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.4
[0.1.5]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.5
[0.1.6b1]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.6b1
[0.1.6b2]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.6b2
[0.1.6b3]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.6b3
[0.1.6b4]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.6b4
[0.1.6]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.6
[0.1.7]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.7
[0.1.8]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.8
[0.1.9]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.9
[0.1.11]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.11
[0.1.12]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.12
[0.1.13b1]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b1
[0.1.13b2]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b2
[0.1.13b3]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b3
[0.1.13b4]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b4
[0.1.13b5]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b5
[0.1.13b6]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b6
[0.1.13b8]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b8
[0.1.13b9]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b9
[0.1.13b10]: https://github.com/clawsomeflow/clawsomeflow/releases/tag/v0.1.13b10
