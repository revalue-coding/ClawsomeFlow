"""ClawsomeFlow data models.

Layer split (deliberate, see DEV.md §6):

* **Top-level entities** (``Flow`` / ``FlowRun`` / ``RunEvent`` / ``OpenclawAgent``)
  are :class:`SQLModel` tables — persisted via the storage backend.
* **Nested value objects** (``FlowAgent`` / ``FlowTask`` / ``FlowSpec`` /
  ``PendingMerge``) are plain :class:`pydantic.BaseModel` — embedded in the
  ``Flow.spec`` JSON column; never have their own table.

Naming convention:
* Python field names are ``snake_case``.
* JSON serialisation aliases are ``camelCase`` (front-end friendly), set via
  ``model_config = {"populate_by_name": True, "alias_generator": ...}`` on
  models exposed to the API.

All enums inherit ``(str, Enum)`` so they JSON-serialise as their value (matches
ClawTeam convention).
"""

from __future__ import annotations

import uuid
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    field_serializer,
    field_validator,
    model_validator,
)
from sqlmodel import JSON, Column, Field as SQLField, SQLModel

# ──────────────────────────────────────────────────────────────────────
# Helpers
# ──────────────────────────────────────────────────────────────────────

# Default merge / spawn target for non-OpenClaw agents when unset in the spec.
# After `ensure-git-repo` creates a repository, callers must use the actual
# primary branch name returned by git (see system.ensure_git_repo).
DEFAULT_TARGET_BRANCH = "master"


def _now() -> datetime:
    """UTC timestamp; consistent with the rest of the codebase."""
    return datetime.now(timezone.utc)


def iso_utc(dt: datetime) -> str:
    """Serialize a datetime as ISO-8601 with explicit UTC offset.

    Why: SQLite drops tzinfo on roundtrip, so DB-loaded datetimes come back
    naive. A bare ``isoformat()`` then yields a string with no tz marker,
    which JS ``new Date()`` interprets as local time — displaying UTC values
    as if they were local. Always emit ``+00:00`` so the frontend's
    ``toLocaleString()`` converts correctly.
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _new_id(prefix: str = "") -> str:
    """Generate a short UUID4 hex id, optionally prefixed (e.g. 'flow-abcdef12')."""
    sid = uuid.uuid4().hex[:12]
    return f"{prefix}-{sid}" if prefix else sid


# Camel ↔ snake helper for API-facing models.
def _to_camel(s: str) -> str:
    head, *tail = s.split("_")
    return head + "".join(w.capitalize() for w in tail)


# ──────────────────────────────────────────────────────────────────────
# Task description ↔ output-summary-requirement codec
# ──────────────────────────────────────────────────────────────────────
#
# The Web UI splits a task's prompt body into two boxes:
#
#   1. "Task detailed description"  — what the worker must do
#   2. "Output summary requirement" — what shape the worker's reply
#                                     (inbox to leader) must take, so
#                                     downstream consumers can rely on it
#
# We deliberately keep ONE persisted column (``FlowTask.description``) for
# backward compatibility and so the dispatch prompt stays readable to the
# worker LLM. The two parts are joined by a stable marker line that's
# unlikely to appear in user text and that renders cleanly in markdown.
#
# Round-trip: split_description(merge_description(a, b)) == (a, b).

OUTPUT_SUMMARY_MARKER = "## Output Summary Requirement"
# Backward-compat marker used by older persisted tasks.
_LEGACY_OUTPUT_SUMMARY_MARKER = "## \u8f93\u51fa\u6458\u8981\u8981\u6c42"
_OUTPUT_SUMMARY_MARKERS = {
    OUTPUT_SUMMARY_MARKER,
    _LEGACY_OUTPUT_SUMMARY_MARKER,
}


def merge_description(body: str, requirement: str | None) -> str:
    """Concatenate the two UI fields into the canonical persisted form.

    ``body`` is the user's "task detailed description"; ``requirement`` is the
    user's "output summary requirement" (may be ``None`` / empty). Returned
    text is what gets
    stored on ``FlowTask.description`` and rendered into the dispatch prompt.
    """
    body = (body or "").rstrip()
    req = (requirement or "").strip()
    if not req:
        return body
    if not body:
        return f"{OUTPUT_SUMMARY_MARKER}\n{req}"
    return f"{body}\n\n{OUTPUT_SUMMARY_MARKER}\n{req}"


def split_description(text: str) -> tuple[str, str | None]:
    """Inverse of :func:`merge_description`.

    Returns ``(body, requirement_or_None)``. If the marker line is absent
    the entire text is the body and the requirement is ``None``.
    Multiple marker lines: only the first is honoured (defensive — a
    reasonable user never writes the marker themselves).
    """
    if not text:
        return "", None
    lines = text.split("\n")
    for i, line in enumerate(lines):
        if line.strip() in _OUTPUT_SUMMARY_MARKERS:
            body = "\n".join(lines[:i]).rstrip()
            req = "\n".join(lines[i + 1:]).strip()
            return body, (req or None)
    return text, None


# ──────────────────────────────────────────────────────────────────────
# Enums
# ──────────────────────────────────────────────────────────────────────


class AgentKind(str, Enum):
    """Supported agent runtime kinds (must match ClawTeam NativeCliAdapter coverage)."""

    claude = "claude"
    codex = "codex"
    cursor = "cursor"
    openclaw = "openclaw"
    kimi = "kimi"
    nanobot = "nanobot"
    gemini = "gemini"
    qwen = "qwen"
    opencode = "opencode"
    pi = "pi"
    hermes = "hermes"
    custom = "custom"


# Agents that don't have a long-lived TUI session (special-cased in scheduler).
NON_TUI_KINDS: frozenset[AgentKind] = frozenset({AgentKind.openclaw})


class MergeStrategy(str, Enum):
    """How a worker's worktree is merged back to its main repo."""

    manual = "manual"            # TUI default; user decides via UI
    auto = "auto"                # TUI option; scheduler executes automatic merge
    skip = "skip"                # Don't merge; don't cleanup
    agent_self = "agent_self"    # OpenClaw default; agent merges in each task's completion steps


class OnFailure(str, Enum):
    retry = "retry"
    skip = "skip"
    abort = "abort"


class RunStatus(str, Enum):
    pending = "pending"
    compiling = "compiling"
    running = "running"
    awaiting_user_checkpoint = "awaiting_user_checkpoint"
    awaiting_user_review = "awaiting_user_review"
    awaiting_user_complaint = "awaiting_user_complaint"
    complaint_processing = "complaint_processing"
    complaint_failed = "complaint_failed"
    completed = "completed"
    completed_with_conflicts = "completed_with_conflicts"
    failed = "failed"
    aborted = "aborted"


class OpenclawRequestStatus(str, Enum):
    """Lifecycle of a legacy async OpenClaw agent request.

    Flow:
        pending → dispatched → succeeded
                            ↘ failed
                            ↘ timed_out
    """

    pending = "pending"        # row created
    dispatched = "dispatched"  # request dispatched to async worker
    succeeded = "succeeded"    # agent created successfully
    failed = "failed"          # explicit failure
    timed_out = "timed_out"    # exceeded request TTL with no callback


class AgentStoreAcquisitionMode(str, Enum):
    """How a user obtained a Store listing entitlement."""

    join = "join"          # free listing
    purchase = "purchase"  # paid listing


class AgentStoreOrderStatus(str, Enum):
    """Lifecycle of a Store order."""

    pending = "pending"
    succeeded = "succeeded"
    failed = "failed"


class TaskDecomposeStatus(str, Enum):
    """Lifecycle of an async "AI decompose this Flow goal into tasks" request.

    Same shape as :class:`OpenclawRequestStatus` — we keep the two enums
    distinct so adding decompose-specific transitions later (e.g.
    ``rejected_by_user``) doesn't muddy the NL-create flow.
    """

    pending = "pending"
    dispatched = "dispatched"
    succeeded = "succeeded"
    failed = "failed"
    timed_out = "timed_out"


# ──────────────────────────────────────────────────────────────────────
# Nested value objects (live inside Flow.spec)
# ──────────────────────────────────────────────────────────────────────


class _ApiBase(BaseModel):
    """Base for API-facing nested models (camelCase aliases)."""

    model_config = ConfigDict(
        populate_by_name=True,
        alias_generator=_to_camel,
        use_enum_values=False,  # keep enum members for type checks; serializer dumps .value
    )


class FlowAgent(_ApiBase):
    """One agent within a Flow (= a long-lived session, see plan §7).

    Strict invariants (enforced by :class:`Flow.validate_consistent`):

    * ``id`` is unique within the Flow.
    * For ``kind=openclaw``: ``id`` MUST equal the OpenClaw agent id (=
      :class:`OpenclawAgent.id`); ``repo`` is null and auto-resolved from
      OpenClaw's main workspace.
    * For non-OpenClaw kinds: ``repo`` is required and must be a valid git repo
      (validated at :class:`Flow` save time).
    * Exactly one ``is_leader=True`` agent per Flow.
    * ``merge_strategy`` must be compatible with ``kind`` (see
      :func:`_validate_merge_strategy`).
    """

    id: str
    kind: AgentKind
    profile: str | None = None
    command: list[str] | None = None  # only when kind == AgentKind.custom
    repo: str | None = None  # required for non-OpenClaw; auto for openclaw
    target_branch: str | None = None  # required for non-OpenClaw; default master
    is_leader: bool = False
    merge_strategy: MergeStrategy | None = None  # default resolved by `default_merge_strategy`
    on_failure: OnFailure = OnFailure.retry
    max_retries: int = 2
    dispose_after_done: bool = True

    @field_validator("id")
    @classmethod
    def _id_charset(cls, v: str) -> str:
        # Reject anything that wouldn't survive as a ClawTeam agent_name.
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError(
                "agent id must be non-empty and contain only [A-Za-z0-9_-]"
            )
        return v

    @field_validator("target_branch")
    @classmethod
    def _target_branch_charset(cls, v: str | None) -> str | None:
        if v is None:
            return None
        text = v.strip()
        if not text:
            return None
        if any(ch.isspace() for ch in text):
            raise ValueError("target_branch must not contain whitespace")
        return text

    @model_validator(mode="after")
    def _resolve_defaults(self) -> "FlowAgent":
        # Default merge_strategy depends on kind.
        if self.merge_strategy is None:
            self.merge_strategy = (
                MergeStrategy.agent_self
                if self.kind == AgentKind.openclaw
                else MergeStrategy.manual
            )
        # Compatibility check (TUI cannot use agent_self; OpenClaw cannot use manual/auto).
        _validate_merge_strategy(self.kind, self.merge_strategy)
        # OpenClaw uses its own main workspace; reject explicit repo.
        if self.kind == AgentKind.openclaw and self.repo not in (None, ""):
            raise ValueError(
                f"agent {self.id!r}: kind=openclaw must NOT set 'repo' "
                "(auto-derived from ~/.clawsomeflow/agents/{id}/workspace/)"
            )
        if self.kind == AgentKind.openclaw and self.target_branch not in (None, ""):
            raise ValueError(
                f"agent {self.id!r}: kind=openclaw must NOT set 'target_branch'"
            )
        if self.kind != AgentKind.openclaw and not self.target_branch:
            self.target_branch = DEFAULT_TARGET_BRANCH
        # Custom kind requires a command.
        if self.kind == AgentKind.custom and not self.command:
            raise ValueError(
                f"agent {self.id!r}: kind=custom requires a non-empty 'command'"
            )
        return self


def _validate_merge_strategy(kind: AgentKind, strategy: MergeStrategy) -> None:
    """Reject incompatible merge_strategy / kind combinations."""
    if kind == AgentKind.openclaw:
        if strategy in (MergeStrategy.manual, MergeStrategy.auto):
            raise ValueError(
                f"OpenClaw agent cannot use merge_strategy={strategy.value!r} "
                "(only 'agent_self' or 'skip')"
            )
    else:
        if strategy == MergeStrategy.agent_self:
            raise ValueError(
                f"Non-OpenClaw agent (kind={kind.value!r}) cannot use "
                "merge_strategy='agent_self' (reserved for OpenClaw)"
            )


class FlowTask(_ApiBase):
    """One task node in the Flow DAG.

    The ``description`` field is the **canonical** persisted form. The
    UI helper ``output_summary_requirement`` is folded INTO ``description``
    on parse via :func:`merge_description` (separated by the
    :data:`OUTPUT_SUMMARY_MARKER` line). On serialisation we split it
    back out so the front-end can render two distinct text boxes.

    See DEV.md §6.1 "Task description codec".
    """

    id: str
    owner_agent_id: str
    subject: str
    description: str = ""
    output_summary_requirement: str | None = None
    depends_on: list[str] = Field(default_factory=list)
    is_leader_summary: bool = False
    requires_human_checkpoint: bool = False
    timeout_seconds: int = 14400  # 240 min (4h) default

    @field_validator("id")
    @classmethod
    def _id_charset(cls, v: str) -> str:
        if not v or not all(c.isalnum() or c in "-_" for c in v):
            raise ValueError("task id must be non-empty and contain only [A-Za-z0-9_-]")
        return v

    @field_validator("timeout_seconds")
    @classmethod
    def _timeout_positive(cls, v: int) -> int:
        if v <= 0:
            raise ValueError("timeout_seconds must be positive")
        return v

    @model_validator(mode="after")
    def _normalise_description(self) -> "FlowTask":
        """Fold the helper field into description (canonical persisted form).

        Idempotent: re-validating an already-merged FlowTask is safe:
        if ``output_summary_requirement`` is empty we pass through; if
        it's set we merge ONCE and clear the helper. The split form is
        re-derived on serialisation by the field_serializers below so
        callers (REST / WebSocket) get back two clean fields.
        """
        if self.output_summary_requirement:
            merged = merge_description(self.description, self.output_summary_requirement)
            object.__setattr__(self, "description", merged)
            object.__setattr__(self, "output_summary_requirement", None)
        if self.is_leader_summary and self.requires_human_checkpoint:
            raise ValueError(
                "leader summary task cannot enable requires_human_checkpoint"
            )
        return self

    # Pydantic v2 invokes field_serializers even during nested serialisation
    # (FlowSpec → list[FlowTask]), unlike a ``model_dump`` override. We use
    # them to re-split the canonical merged ``description`` back into the
    # two UI-friendly fields without ever storing the helper separately.

    @field_serializer("description")
    def _ser_description(self, value: str) -> str:
        body, _ = split_description(value)
        return body

    @field_serializer("output_summary_requirement")
    def _ser_output_summary(self, _value: str | None) -> str | None:
        _body, req = split_description(self.description)
        return req


class FlowSpec(_ApiBase):
    """Full spec of a Flow: agents + tasks + variables."""

    agents: list[FlowAgent]
    tasks: list[FlowTask]
    variables: dict[str, str] = Field(default_factory=dict)


class PendingMerge(_ApiBase):
    """Per-agent merge waiting for user review (manual mode)."""

    agent_id: str
    branch: str
    target_branch: str = DEFAULT_TARGET_BRANCH
    diff_summary: dict[str, Any] = Field(default_factory=dict)
    leader_suggestion: str = ""


# ──────────────────────────────────────────────────────────────────────
# Top-level persisted entities (SQLModel tables)
# ──────────────────────────────────────────────────────────────────────


class _SQLBase(SQLModel):
    """Base for top-level SQLModel tables (snake_case columns)."""

    pass


class Flow(_SQLBase, table=True):
    """A Flow definition (DAG of agents + tasks).

    Persistence layout: top-level fields → SQL columns; ``spec`` is stored as
    a JSON blob (sqlite TEXT). The Flow JSON file under
    ``~/.clawsomeflow/.flows/{id}.json`` is treated as a backup / portability
    copy; the SQL row is the source of truth.
    """

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("flow"))
    name: str = SQLField(index=True)
    description: str = ""
    version: int = SQLField(default=1)  # optimistic locking; bumped on every PUT
    cleanup_team_on_finish: bool = True
    spec: dict[str, Any] = SQLField(sa_column=Column(JSON, nullable=False), default_factory=dict)
    owner_user: str = SQLField(index=True)
    created_at: datetime = SQLField(default_factory=_now, nullable=False)
    updated_at: datetime = SQLField(default_factory=_now, nullable=False)

    def parsed_spec(self) -> FlowSpec:
        """Parse and validate the raw ``spec`` JSON into a :class:`FlowSpec`."""
        return FlowSpec.model_validate(self.spec)

    def with_spec(self, spec: FlowSpec) -> "Flow":
        """Return *self* with ``spec`` set from a validated :class:`FlowSpec`."""
        self.spec = spec.model_dump(mode="json", by_alias=False)
        return self


class FlowRun(_SQLBase, table=True):
    """A single execution instance of a Flow."""

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("run"))
    flow_id: str = SQLField(index=True, foreign_key="flow.id")
    flow_version: int
    team_name: str = SQLField(index=True, unique=True)  # csflow-{run_id_short}
    status: RunStatus = SQLField(default=RunStatus.pending, index=True)
    inputs: dict[str, Any] = SQLField(sa_column=Column(JSON), default_factory=dict)
    user: str = SQLField(index=True)
    started_at: datetime = SQLField(default_factory=_now, nullable=False)
    finished_at: datetime | None = None
    pending_merges: list[dict[str, Any]] | None = SQLField(
        sa_column=Column(JSON, nullable=True), default=None
    )


class FlowRunSchedule(_SQLBase, table=True):
    """User-defined timed workflow trigger configuration."""

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("sched"))
    user: str = SQLField(index=True)
    name: str = ""
    # ``parallel`` -> trigger all configured Flows together.
    # ``serial`` -> wait each triggered Run reaches terminal before next.
    run_mode: str = SQLField(default="serial")
    # ``once`` -> auto-delete after one execution.
    # ``recurring`` -> keep and shift ``next_run_at`` by ``interval_days``.
    execute_mode: str = SQLField(default="once")
    interval_days: int | None = None
    next_run_at: datetime = SQLField(index=True)
    items: list[dict[str, Any]] = SQLField(sa_column=Column(JSON), default_factory=list)
    created_at: datetime = SQLField(default_factory=_now, nullable=False)
    updated_at: datetime = SQLField(default_factory=_now, nullable=False)


class RunEvent(_SQLBase, table=True):
    """Append-only Run event log (drives the WebSocket stream)."""

    id: int | None = SQLField(default=None, primary_key=True)
    run_id: str = SQLField(index=True, foreign_key="flowrun.id")
    ts: datetime = SQLField(default_factory=_now, nullable=False, index=True)
    type: str = SQLField(index=True)  # see API.md "events" list
    agent_id: str | None = None
    task_id: str | None = None
    payload: dict[str, Any] = SQLField(sa_column=Column(JSON), default_factory=dict)


class FlowRunScheduleExecution(_SQLBase, table=True):
    """One execution attempt record of a timed Flow schedule."""

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("schedx"))
    schedule_id: str = SQLField(index=True)
    schedule_name: str = ""
    user: str = SQLField(index=True)
    run_mode: str = SQLField(default="serial")
    execute_mode: str = SQLField(default="once")
    status: str = SQLField(default="running", index=True)
    total_items: int = 0
    succeeded_items: int = 0
    failed_items: int = 0
    skipped_items: int = 0
    run_ids: list[str] = SQLField(sa_column=Column(JSON), default_factory=list)
    item_results: list[dict[str, Any]] = SQLField(sa_column=Column(JSON), default_factory=list)
    started_at: datetime = SQLField(default_factory=_now, nullable=False, index=True)
    finished_at: datetime | None = None


class OpenclawAgent(_SQLBase, table=True):
    """A user OpenClaw agent governed by ClawsomeFlow.

    ``id`` here is BOTH the OpenClaw agent id AND the :class:`FlowAgent.id`
    used when this agent participates in a Flow. There's exactly one
    canonical id per OpenClaw agent.
    """

    id: str = SQLField(primary_key=True)  # = openclaw agent id (no auto prefix)
    name: str
    description: str = ""
    team_id: str = SQLField(default="", index=True)
    workspace_path: str  # = ~/.clawsomeflow/agents/{id}/workspace/
    openclaw_config_snapshot: dict[str, Any] = SQLField(
        sa_column=Column(JSON), default_factory=dict
    )
    created_by_user: str = SQLField(index=True)
    nl_prompt: str = ""
    created_at: datetime = SQLField(default_factory=_now, nullable=False)


class OpenclawTeam(_SQLBase, table=True):
    """Internal team grouping for user-managed OpenClaw agents.

    Despite the ``Openclaw`` prefix this is a ClawsomeFlow-only grouping
    concept shared across agent platforms (OpenClaw + Hermes). A unified
    cross-platform board is future work.
    """

    id: str = SQLField(primary_key=True)  # generated as ``csfow-group-xx``
    name: str
    created_by_user: str = SQLField(index=True)
    created_at: datetime = SQLField(default_factory=_now, nullable=False)


class HermesAgent(_SQLBase, table=True):
    """A user Hermes agent governed by ClawsomeFlow.

    ``id`` here is BOTH the Hermes **profile name** (``hermes -p <id>``) AND
    the :class:`FlowAgent.id` used when this agent participates in a Flow.
    There's exactly one canonical id per Hermes agent. Hermes owns its own
    state under ``~/.hermes/profiles/{id}/`` (the ``profile_root``); we only
    drive it through the ``hermes`` CLI and never write into that home for our
    own bookkeeping. ``team_id`` references :class:`OpenclawTeam.id` (the
    shared grouping).
    """

    id: str = SQLField(primary_key=True)  # = hermes profile name (no auto prefix)
    name: str
    description: str = ""
    team_id: str = SQLField(default="", index=True)
    profile_root: str  # = ~/.hermes/profiles/{id}
    created_by_user: str = SQLField(index=True)
    nl_prompt: str = ""
    created_at: datetime = SQLField(default_factory=_now, nullable=False)


class ManagedAgent(_SQLBase, table=True):
    """A user-managed env-home TUI agent (Claude Code / Codex / Cursor).

    Unlike OpenClaw (session-id) and Hermes (``-p`` profile), these platforms
    carry their identity/skills/MCP in a relocatable config home selected via an
    environment variable (``CLAUDE_CONFIG_DIR`` / ``CODEX_HOME`` /
    ``CURSOR_CONFIG_DIR``). That env is injected at spawn through a ClawTeam
    runtime profile (``clawteam profile set --env``), so tools follow the agent
    regardless of the per-task working directory.

    ``id`` is BOTH the canonical agent id AND the :class:`FlowAgent.id` used when
    this agent participates in a Flow (one canonical id per managed agent).
    """

    id: str = SQLField(primary_key=True)  # = FlowAgent.id
    kind: str = SQLField(index=True)  # "claude" | "codex" | "cursor"
    name: str
    description: str = ""
    team_id: str = SQLField(default="", index=True)
    config_home: str  # = ~/.clawsomeflow/agents/{id}/{kind}-home
    clawteam_profile: str  # = csflow-{kind}-{id}
    created_by_user: str = SQLField(index=True)
    nl_prompt: str = ""
    created_at: datetime = SQLField(default_factory=_now, nullable=False)


class AgentStoreOwnership(_SQLBase, table=True):
    """One user entitlement for one Agent Store listing."""

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("storeown"))
    owner_user: str = SQLField(index=True)
    listing_id: str = SQLField(index=True)
    listing_type: str = SQLField(index=True)  # "single" | "team"
    title: str = ""
    acquired_via: AgentStoreAcquisitionMode = SQLField(
        default=AgentStoreAcquisitionMode.join,
        index=True,
    )
    source_repo: str = ""          # "{owner}/{repo}@{ref}"
    source_manifest_path: str = ""  # listing manifest path inside repo
    listing_snapshot: dict[str, Any] = SQLField(
        sa_column=Column(JSON),
        default_factory=dict,
    )
    acquired_at: datetime = SQLField(default_factory=_now, nullable=False)


class AgentStoreOrder(_SQLBase, table=True):
    """Order record for paid Store listings (payment integration placeholder)."""

    id: str = SQLField(primary_key=True, default_factory=lambda: _new_id("storeord"))
    owner_user: str = SQLField(index=True)
    listing_id: str = SQLField(index=True)
    status: AgentStoreOrderStatus = SQLField(default=AgentStoreOrderStatus.pending, index=True)
    currency: str = "USD"
    amount: float = 0.0
    is_mock: bool = False
    payment_provider: str | None = None
    external_payment_id: str | None = None
    created_at: datetime = SQLField(default_factory=_now, nullable=False)
    updated_at: datetime = SQLField(default_factory=_now, nullable=False)


class TaskDecomposeRequest(_SQLBase, table=True):
    """Tracks an async "AI decompose Flow goal into tasks" request.

    Created when a user clicks "🤖 AI Decompose" in the Flow editor.
    Updated by the ``csflow-task-decomposer`` skill via
    ``POST /api/internal/task-decompose/commit``.

    The request targets one leader agent selected in Flow editor. OpenClaw
    leaders use the decomposer skill path; non-OpenClaw leaders receive an
    explicit callback protocol prompt.

    Result fields hold the **proposed** spec; the front-end then renders
    it into the Flow editor for the user to review / edit. We don't
    write a Flow row from this — the user must hit Save.
    """

    request_id: str = SQLField(primary_key=True, default_factory=lambda: _new_id(""))
    user: str = SQLField(index=True)
    goal: str = ""                              # user prose ("I want a flow that…")
    leader_agent_id: str                        # selected leader id in editor
    existing_agents: list[dict[str, Any]] = SQLField(
        sa_column=Column(JSON), default_factory=list,
    )                                           # hint: agents already in editor
    existing_tasks: list[dict[str, Any]] = SQLField(
        sa_column=Column(JSON), default_factory=list,
    )                                           # hint: tasks already in editor
    status: TaskDecomposeStatus = SQLField(
        default=TaskDecomposeStatus.pending, index=True,
    )
    result_agents: list[dict[str, Any]] | None = SQLField(
        sa_column=Column(JSON, nullable=True), default=None,
    )
    result_tasks: list[dict[str, Any]] | None = SQLField(
        sa_column=Column(JSON, nullable=True), default=None,
    )
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = SQLField(default_factory=_now, nullable=False)
    updated_at: datetime = SQLField(default_factory=_now, nullable=False)
    expires_at: datetime                        # = created_at + token TTL


class OpenclawAgentRequest(_SQLBase, table=True):
    """Legacy table for historical async OpenClaw creation requests."""

    request_id: str = SQLField(primary_key=True, default_factory=lambda: _new_id(""))
    user: str = SQLField(index=True)
    nl_prompt: str  # raw user prompt (for audit / retry)
    status: OpenclawRequestStatus = SQLField(
        default=OpenclawRequestStatus.pending, index=True,
    )
    requested_agent_id: str | None = None  # set on commit when known
    error_code: str | None = None
    error_message: str | None = None
    created_at: datetime = SQLField(default_factory=_now, nullable=False)
    updated_at: datetime = SQLField(default_factory=_now, nullable=False)
    expires_at: datetime  # = created_at + token TTL; rows past this go to timed_out


__all__ = [
    # enums
    "AgentKind",
    "MergeStrategy",
    "OnFailure",
    "RunStatus",
    "OpenclawRequestStatus",
    "AgentStoreAcquisitionMode",
    "AgentStoreOrderStatus",
    "TaskDecomposeStatus",
    "NON_TUI_KINDS",
    # nested
    "FlowAgent",
    "FlowTask",
    "FlowSpec",
    "PendingMerge",
    # tables
    "Flow",
    "FlowRun",
    "FlowRunSchedule",
    "RunEvent",
    "OpenclawAgent",
    "OpenclawTeam",
    "HermesAgent",
    "ManagedAgent",
    "AgentStoreOwnership",
    "AgentStoreOrder",
    "OpenclawAgentRequest",
    "TaskDecomposeRequest",
    # helpers
    "_new_id",  # re-exported for tests
]
