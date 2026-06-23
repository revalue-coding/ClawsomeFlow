/**
 * Flow Editor — leader-first list-style task editor.
 *
 * Layout:
 *  - Flow basics: name + overall goal + leader picker +
 *    inline "AI Decompose" button. Leader selection auto-creates (and
 *    keeps in sync) a non-deletable summary task whose owner is the
 *    chosen leader.
 *  - Execution pre-specification (optional).
 *  - Tasks: compact one-row-per-task list with Details / Edit / Delete /
 *    Up / Down buttons. A modal opens the full task form for add or edit.
 *
 * Invariants preserved (server-side validators):
 *  - The leader owns exactly one task, and that task is the summary
 *    (`is_leader_summary=true`).
 *  - cleanupTeamOnFinish is always true (the previous UI checkbox
 *    expressed product policy; users no longer see or change it).
 *  - Task timeouts default to 14400s (4h); users no longer set this.
 */

import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";
import {
  ApiError,
  AgentKind,
  DecomposeStatus,
  FlowAgent,
  FlowDetail,
  FlowSaveWarning,
  FlowSpec,
  FlowSummary,
  FlowTask,
  HermesAgentSummary,
  OpenclawAgentSummary,
  api,
} from "@/lib/api";
import { Card, CardTitle, ErrorBox, Loading, Modal, StatusPill } from "@/components/ui";
import { useDialog } from "@/components/dialog";
import { ChatIcon } from "@/components/icons";
import { cn } from "@/lib/cn";
import { useTheme } from "@/lib/theme";
import {
  DEFAULT_TARGET_BRANCH,
  getRunInputFields,
  setRunInputFields,
  getEasyMode,
  setEasyMode,
  getDevMode,
  setDevMode,
} from "@/lib/flowRuntime";
import { branchAfterRepoCheck, ensureRepoAndListBranches } from "@/lib/flowRepoBranch";
import { alertIfNativeDirectoryBlocked } from "@/lib/remoteClient";
import {
  clearSessionBackedKeys,
  useSessionBackedModalFlag,
  useSessionBackedState,
} from "@/lib/sessionState";

// NOTE: nanobot is intentionally NOT listed — it is supported at the runtime
// layer but temporarily not exposed to users (no UI option). Re-add it here (and
// to NEW_OWNER_KINDS / ownerKindLabel / i18n / task_decompose / deps) to surface.
type NonOpenclawOwnerKind =
  | "claude"
  | "codex"
  | "cursor"
  | "gemini"
  | "kimi"
  | "qwen"
  | "opencode"
  | "qoder"
  | "codebuddy"
  | "hermes";
type OwnerKind = "openclaw" | NonOpenclawOwnerKind;
type OwnerKindDraft = OwnerKind | "";
type OwnerMode = "existing" | "new";
type DeploymentMode = "local" | "server";

interface ExistingOwnerOption {
  key: string;
  id: string;
  kind: OwnerKind;
  repo: string;
  targetBranch: string;
  label: string;
}

interface TaskRow {
  rowKey: string;
  id: string;
  subject: string;
  description: string;
  outputSummaryRequirement: string;
  requiresHumanCheckpoint: boolean;
  /** Developer-mode per-task auto-merge switch. Default true. Ignored unless
   *  the Flow is in developer mode; OpenClaw owners are forced to auto-merge. */
  autoMerge: boolean;
  ownerKind: OwnerKindDraft;
  ownerId: string;
  ownerRepo: string;
  ownerTargetBranch: string;
  /** True when the owner is a temporary (ad-hoc) agent created inline ("new"
   *  source), false when it references a persistent/managed agent ("existing"
   *  source). Drives backend FlowAgent.is_temporary. */
  ownerIsTemporary: boolean;
  dependsOn: string[];
  isLeaderSummary: boolean;
  timeoutSeconds: number;
}

interface ValidationMessages {
  leaderKindRequired: string;
  leaderKindUnavailable: (kindLabel: string) => string;
  pickOneSummary: string;
  onlyOneSummary: string;
  taskIdRequired: string;
  taskIdPattern: string;
  duplicateTaskId: (taskId: string) => string;
  subjectRequired: string;
  ownerKindRequired: string;
  ownerKindUnavailable: (args: { subject: string; kindLabel: string }) => string;
  pickOpenclawAgent: string;
  ownerAgentNameRequired: string;
  ownerAgentIdPattern: string;
  claudeRepoRequired: string;
  claudeTargetBranchRequired: string;
  leaderCannotOwnNonSummary: string;
  summaryNeedsDependency: string;
  ownerRepoBranchMismatch: (agentId: string) => string;
  /** Same agent id used by two different platforms/kinds in one flow. A
   *  FlowAgent id is global within a flow regardless of platform; without this
   *  the two rows silently collapse into one agent (rowsToSpec keys by id). */
  duplicateAgentIdCrossKind: (agentId: string) => string;
  cycleDetected: (cyclePath: string) => string;
  /** Per-task: description (instruction body) must be non-empty so the
   *  worker dispatch prompt actually contains an instruction. */
  descriptionRequired: (subject: string) => string;
  /** Per-task: the OpenClaw agent picked as owner no longer exists in
   *  the user's agent list (was deleted in another tab / pulled flow
   *  from a different user, etc). Compile would fail with
   *  ``OPENCLAW_AGENT_NOT_FOUND`` — catch it client-side first. */
  openclawAgentMissing: (subject: string, agentId: string) => string;
  /** Per-task: a persistent Hermes owner no longer exists in the user's Hermes
   *  agent list (deleted elsewhere / pulled from another user). */
  hermesAgentMissing: (subject: string, agentId: string) => string;
}

interface FlowSavePayload {
  name: string;
  description: string;
  cleanupTeamOnFinish: boolean;
  spec: FlowSpec;
}

interface PersistFlowResult {
  warnings?: FlowSaveWarning[];
}

type RepoIssueReason =
  | "path_not_found"
  | "not_directory"
  | "not_git_repo"
  | "no_initial_commit"
  | "unknown";

interface RepoIssue {
  agentId: string;
  repo: string;
  reason: RepoIssueReason;
}

const DEFAULT_TIMEOUT_SECONDS = 14400;
const ID_PATTERN = /^[A-Za-z0-9_-]+$/;

const newRowKey = () => Math.random().toString(36).slice(2, 10);

/** Auto-generated, hidden-from-user task ID. Stable across edits within the
 *  editor session. */
const newTaskId = () => `task-${newRowKey()}`;

function repoBranchMessages(t: (key: string, opts?: Record<string, unknown>) => string) {
  return {
    reasonPathMissing: t("flowEditor.repoIssue.reasonPathMissing"),
    reasonNotGitRepo: t("flowEditor.repoIssue.reasonNotGitRepo"),
    reasonNoInitialCommit: t("flowEditor.repoIssue.reasonNoInitialCommit"),
    reasonUnknown: t("flowEditor.repoIssue.reasonUnknown"),
    confirmCreate: (args: { agentId: string; repo: string; reason: string }) =>
      t("flowEditor.taskRepoCheck.confirmCreate", args),
    reselectHint: t("flowEditor.taskRepoCheck.reselectHint"),
    checkFailed: (args: { message: string }) =>
      t("flowEditor.taskRepoCheck.checkFailed", args),
    fetchFailed: (args: { message: string }) =>
      t("flowEditor.taskBranchCheck.fetchFailed", args),
    stillInvalid: t("flowEditor.taskRepoCheck.stillInvalid"),
    pathNotAbsolute: t("flowEditor.taskRepoCheck.pathNotAbsolute"),
  };
}

function blankRow(): TaskRow {
  return {
    rowKey: newRowKey(),
    id: newTaskId(),
    subject: "",
    description: "",
    outputSummaryRequirement: "",
    requiresHumanCheckpoint: false,
    autoMerge: true,
    ownerKind: "",
    ownerId: "",
    ownerRepo: "",
    ownerTargetBranch: "",
    // Blank rows default to the "new" source → a temporary agent.
    ownerIsTemporary: true,
    dependsOn: [],
    isLeaderSummary: false,
    timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
  };
}

// Owner-kind option sets, split by the two owner-source CATEGORIES the UI now
// exposes: "持久化Agent" (persistent) vs "临时Agent" (temporary).
//
// PERSISTENT_OWNER_KINDS — kinds backed by a real persistent management
// platform: OpenClaw + Hermes ONLY. Picked from that platform's managed
// dropdown; in-flow temporary agents are NEVER offered here. Used for BOTH the
// leader and worker tasks.
//
// NEW_OWNER_KINDS — kinds available as temporary, ad-hoc agents (any
// non-OpenClaw kind). In the temporary category the agent name is free-typed
// (create new) OR picked from a dropdown of temporary agents already created in
// THIS flow (worker tasks only; the leader never reuses an in-flow worker agent
// and so gets the input alone).
// Hermes first: it is the default persistent platform (P3) and the first option
// shown in the persistent kind dropdown.
const PERSISTENT_OWNER_KINDS: OwnerKind[] = ["hermes", "openclaw"];
const NEW_OWNER_KINDS: NonOpenclawOwnerKind[] = [
  "claude",
  "codex",
  "cursor",
  "gemini",
  "kimi",
  "qwen",
  "opencode",
  "qoder",
  "codebuddy",
  "hermes",
];

function isOwnerKind(kind: OwnerKindDraft): kind is OwnerKind {
  return kind !== "";
}

function isOpenclawKind(kind: OwnerKindDraft): kind is "openclaw" {
  return kind === "openclaw";
}

function isNonOpenclawKind(kind: OwnerKindDraft): kind is NonOpenclawOwnerKind {
  return kind !== "" && kind !== "openclaw";
}

function needsRepoBranchFields(kind: OwnerKindDraft): boolean {
  return !isOpenclawKind(kind);
}

function isPersistentOwnerKind(kind: OwnerKindDraft): kind is OwnerKind {
  return kind === "openclaw" || kind === "hermes";
}

function toPersistentOwnerKind(raw: string): OwnerKind | null {
  return (PERSISTENT_OWNER_KINDS as readonly string[]).includes(raw)
    ? (raw as OwnerKind)
    : null;
}

function toTempOwnerKind(raw: string): NonOpenclawOwnerKind | null {
  return (NEW_OWNER_KINDS as readonly string[]).includes(raw)
    ? (raw as NonOpenclawOwnerKind)
    : null;
}

// Kinds whose agent id must be picked from a managed-agent dropdown (not free
// text). Only Hermes has a persistent management platform; Claude/Codex/Cursor
// are temporary/ad-hoc (free-text), like Cursor. KEEP their repo/branch.
const MANAGED_PICK_KINDS = new Set<OwnerKind>(["hermes"]);

function isManagedPickKind(kind: OwnerKind): boolean {
  return MANAGED_PICK_KINDS.has(kind);
}

/** Managed-agent picklist for a kind (Hermes only). */
function pickAgentsForKind(
  kind: OwnerKind,
  hermes: HermesAgentSummary[],
): { id: string; name: string }[] {
  if (kind === "hermes") return hermes.map((a) => ({ id: a.id, name: a.name }));
  return [];
}

/** Field separator used to encode a temp-agent selection (id + repo + branch)
 *  into one combobox option value. NUL never appears in an id/path/branch. */
const TEMP_AGENT_VALUE_SEP = "\u001f";
const HOME_PATH_SENTINEL = "__CSFLOW_HOME__";

/** Normalize repo paths for identity comparisons only (not for display).
 *  Treat ``~/foo`` and common home-absolute forms like ``/Users/x/foo`` /
 *  ``/home/x/foo`` as equivalent, and ignore trailing slashes. */
function normalizeRepoPathForCompare(repo: string): string {
  const trimmed = repo.trim();
  if (!trimmed) return "";
  const slashNormalized = trimmed.replace(/\\/g, "/").replace(/\/{2,}/g, "/");
  const noTrailingSlash = slashNormalized.length > 1
    ? slashNormalized.replace(/\/+$/g, "")
    : slashNormalized;
  if (noTrailingSlash === "~") return HOME_PATH_SENTINEL;
  if (noTrailingSlash.startsWith("~/")) {
    return `${HOME_PATH_SENTINEL}/${noTrailingSlash.slice(2)}`;
  }
  const homeAbs = noTrailingSlash.match(/^\/(?:Users|home)\/[^/]+(\/.*)?$/);
  if (homeAbs) {
    return `${HOME_PATH_SENTINEL}${homeAbs[1] ?? ""}`;
  }
  return noTrailingSlash;
}

function sameRepoPathForCompare(a: string, b: string): boolean {
  return normalizeRepoPathForCompare(a) === normalizeRepoPathForCompare(b);
}

function tempAgentValue(a: { id: string; repo: string; targetBranch: string }): string {
  return [
    a.id.trim(),
    normalizeRepoPathForCompare(a.repo),
    a.targetBranch.trim(),
  ].join(TEMP_AGENT_VALUE_SEP);
}

/** "Existing"-source picklist for a temporary (non-OpenClaw) kind: the
 *  temporary agents the user already created in another task of THIS flow. Each
 *  option carries its repo+targetBranch so that SELECTING it re-uses the exact
 *  same worktree identity (the user-confirmed exception to block independence —
 *  picking an enumerated definition adopts its repo/branch; free-typing a name
 *  stays independent). Leader-summary rows and the editing row are excluded;
 *  results are deduped by (id, repo, targetBranch). */
function flowTempAgentsForKind(
  kind: NonOpenclawOwnerKind,
  rows: TaskRow[],
  excludeRowKey?: string,
): { id: string; repo: string; targetBranch: string; label: string }[] {
  const out: { id: string; repo: string; targetBranch: string; label: string }[] = [];
  const seen = new Set<string>();
  for (const r of rows) {
    if (r.isLeaderSummary) continue;
    if (excludeRowKey && r.rowKey === excludeRowKey) continue;
    if (r.ownerKind !== kind) continue;
    const id = r.ownerId.trim();
    if (!id) continue;
    const repo = r.ownerRepo.trim();
    const targetBranch = r.ownerTargetBranch.trim();
    const key = tempAgentValue({ id, repo, targetBranch });
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      id,
      repo,
      targetBranch,
      label: `${id} (${repo || "—"} @ ${targetBranch || "—"})`,
    });
  }
  return out;
}

/** Find an existing in-flow workspace binding for a non-OpenClaw owner id. Used
 *  when SELECTING a persistent owner already referenced by another task of this
 *  flow: we overwrite repo/branch from that existing binding (the user-confirmed
 *  exception — selecting an enumerated name adopts the same-name agent's
 *  repo/branch). */
function findFlowOwnerWorkspace(
  rows: TaskRow[],
  owner: Pick<TaskRow, "ownerKind" | "ownerId">,
  excludeRowKey?: string,
): { repo: string; targetBranch: string } | null {
  if (!isNonOpenclawKind(owner.ownerKind)) return null;
  const ownerId = owner.ownerId.trim();
  if (!ownerId) return null;
  for (const r of rows) {
    if (excludeRowKey && r.rowKey === excludeRowKey) continue;
    if (r.ownerKind !== owner.ownerKind) continue;
    if (r.ownerId.trim() !== ownerId) continue;
    const repo = r.ownerRepo.trim();
    if (!repo) continue;
    return {
      repo: r.ownerRepo,
      targetBranch: r.ownerTargetBranch.trim(),
    };
  }
  return null;
}

function ownerKey(
  row: Pick<TaskRow, "ownerKind" | "ownerId" | "ownerRepo" | "ownerTargetBranch">,
) {
  if (isOpenclawKind(row.ownerKind)) return `openclaw:${row.ownerId.trim()}`;
  if (!isOwnerKind(row.ownerKind)) return `unset:${row.ownerId.trim()}`;
  return `${row.ownerKind}:${normalizeRepoPathForCompare(row.ownerRepo)}:${row.ownerTargetBranch.trim()}:${row.ownerId.trim()}`;
}

interface OwnerBinding {
  ownerKind: OwnerKindDraft;
  ownerRepo: string;
  ownerTargetBranch: string;
  ownerIsTemporary: boolean;
}

function normalizedOwnerBinding(
  row: Pick<TaskRow, "ownerKind" | "ownerRepo" | "ownerTargetBranch" | "ownerIsTemporary">,
): OwnerBinding {
  if (isOpenclawKind(row.ownerKind)) {
    return {
      ownerKind: "openclaw",
      ownerRepo: "",
      ownerTargetBranch: "",
      ownerIsTemporary: false,
    };
  }
  return {
    ownerKind: row.ownerKind,
    ownerRepo: row.ownerRepo.trim(),
    ownerTargetBranch: row.ownerTargetBranch.trim(),
    ownerIsTemporary: !!row.ownerIsTemporary,
  };
}

function sameOwnerBinding(
  a: Pick<TaskRow, "ownerKind" | "ownerRepo" | "ownerTargetBranch" | "ownerIsTemporary">,
  b: Pick<TaskRow, "ownerKind" | "ownerRepo" | "ownerTargetBranch" | "ownerIsTemporary">,
): boolean {
  const left = normalizedOwnerBinding(a);
  const right = normalizedOwnerBinding(b);
  if (left.ownerKind !== right.ownerKind) return false;
  if (left.ownerIsTemporary !== right.ownerIsTemporary) return false;
  if (left.ownerKind === "openclaw") return true;
  return (
    sameRepoPathForCompare(left.ownerRepo, right.ownerRepo)
    && left.ownerTargetBranch === right.ownerTargetBranch
  );
}

function applyOwnerBinding(row: TaskRow, binding: OwnerBinding): TaskRow {
  const normalized = normalizedOwnerBinding(binding);
  if (
    row.ownerKind === normalized.ownerKind
    && row.ownerRepo === normalized.ownerRepo
    && row.ownerTargetBranch === normalized.ownerTargetBranch
    && row.ownerIsTemporary === normalized.ownerIsTemporary
  ) {
    return row;
  }
  return {
    ...row,
    ownerKind: normalized.ownerKind,
    ownerRepo: normalized.ownerRepo,
    ownerTargetBranch: normalized.ownerTargetBranch,
    ownerIsTemporary: normalized.ownerIsTemporary,
  };
}

function syncOwnerBindingAcrossSubtasks(
  rows: TaskRow[],
  ownerId: string,
  bindingRow: Pick<TaskRow, "ownerKind" | "ownerRepo" | "ownerTargetBranch" | "ownerIsTemporary">,
  excludeRowKey?: string,
): { rows: TaskRow[]; syncedCount: number } {
  const normalizedOwnerId = ownerId.trim();
  if (!normalizedOwnerId) return { rows, syncedCount: 0 };
  const binding = normalizedOwnerBinding(bindingRow);
  let syncedCount = 0;
  const nextRows = rows.map((row) => {
    if (row.isLeaderSummary) return row;
    if (excludeRowKey && row.rowKey === excludeRowKey) return row;
    if (row.ownerId.trim() !== normalizedOwnerId) return row;
    const nextRow = applyOwnerBinding(row, binding);
    if (nextRow !== row) syncedCount += 1;
    return nextRow;
  });
  return { rows: nextRows, syncedCount };
}

/** OpenClaw subtasks always auto-merge in developer mode (matches backend
 *  ``task_self_merges``). Keep row state and persisted spec aligned. */
function enforceOpenclawAutoMerge(row: TaskRow): TaskRow {
  if (row.ownerKind !== "openclaw" || row.autoMerge) return row;
  return { ...row, autoMerge: true };
}

function enforceOpenclawAutoMergeAll(rows: TaskRow[]): TaskRow[] {
  return rows.map(enforceOpenclawAutoMerge);
}

function persistentAgentExists(
  kind: OwnerKind,
  ownerId: string,
  openclawOptions: OpenclawAgentSummary[],
  hermesOptions: HermesAgentSummary[],
): boolean {
  const normalizedOwnerId = ownerId.trim();
  if (!normalizedOwnerId) return false;
  if (kind === "openclaw") {
    return openclawOptions.some((agent) => agent.id === normalizedOwnerId);
  }
  if (kind === "hermes") {
    return hermesOptions.some((agent) => agent.id === normalizedOwnerId);
  }
  return false;
}

function ownerIdAfterPlatformChange({
  sourceMode,
  previousKind,
  nextKind,
  ownerId,
  openclawOptions,
  hermesOptions,
}: {
  sourceMode: OwnerMode;
  previousKind: OwnerKindDraft;
  nextKind: OwnerKindDraft;
  ownerId: string;
  openclawOptions: OpenclawAgentSummary[];
  hermesOptions: HermesAgentSummary[];
}): string {
  const normalizedOwnerId = ownerId.trim();
  if (!normalizedOwnerId) return "";
  if (!isOwnerKind(nextKind)) return "";
  if (!isOwnerKind(previousKind)) return normalizedOwnerId;
  if (sourceMode !== "existing") return normalizedOwnerId;
  if (previousKind === nextKind) return normalizedOwnerId;
  if (
    !isPersistentOwnerKind(previousKind)
    || !isPersistentOwnerKind(nextKind)
  ) {
    return normalizedOwnerId;
  }
  return persistentAgentExists(nextKind, normalizedOwnerId, openclawOptions, hermesOptions)
    ? normalizedOwnerId
    : "";
}

function ownerKindLabel(
  kind: OwnerKindDraft,
  t: (key: string) => string,
): string {
  if (!kind) return t("flowEditor.taskFields.ownerKindPlaceholder");
  if (kind === "openclaw") return t("flowEditor.taskFields.ownerKindOpenclaw");
  if (kind === "codex") return t("flowEditor.taskFields.ownerKindCodex");
  if (kind === "cursor") return t("flowEditor.taskFields.ownerKindCursor");
  if (kind === "gemini") return t("flowEditor.taskFields.ownerKindGemini");
  if (kind === "kimi") return t("flowEditor.taskFields.ownerKindKimi");
  if (kind === "qwen") return t("flowEditor.taskFields.ownerKindQwen");
  if (kind === "opencode") return t("flowEditor.taskFields.ownerKindOpencode");
  if (kind === "qoder") return t("flowEditor.taskFields.ownerKindQoder");
  if (kind === "codebuddy") return t("flowEditor.taskFields.ownerKindCodebuddy");
  if (kind === "hermes") return t("flowEditor.taskFields.ownerKindHermes");
  return t("flowEditor.taskFields.ownerKindClaude");
}

// All owner kinds the editor understands. Used to coerce an arbitrary
// backend/proposal `kind` string into a known OwnerKind (unknown → "claude").
const ALL_OWNER_KINDS: readonly OwnerKind[] = [
  "openclaw",
  ...NEW_OWNER_KINDS,
];

function toOwnerKind(raw: unknown): OwnerKind {
  const k = String(raw ?? "").trim();
  return (ALL_OWNER_KINDS as readonly string[]).includes(k)
    ? (k as OwnerKind)
    : "claude";
}

interface OwnerKindsAvailability {
  persistentKinds: OwnerKind[];
  temporaryKinds: NonOpenclawOwnerKind[];
}

const EMPTY_OWNER_KINDS: OwnerKindsAvailability = {
  persistentKinds: [],
  temporaryKinds: [],
};

function dedupeKindsInKnownOrder<T extends string>(
  knownOrder: readonly T[],
  values: Iterable<string>,
): T[] {
  const picked = new Set<T>();
  for (const raw of values) {
    const normalized = raw.trim();
    if (!normalized) continue;
    if ((knownOrder as readonly string[]).includes(normalized)) {
      picked.add(normalized as T);
    }
  }
  return knownOrder.filter((kind) => picked.has(kind));
}

function usedOwnerKinds(
  rows: TaskRow[],
  leaderKind: OwnerKindDraft,
): OwnerKindsAvailability {
  const persistent = new Set<string>();
  const temporary = new Set<string>();
  for (const row of rows) {
    if (!isOwnerKind(row.ownerKind)) continue;
    if (isPersistentOwnerKind(row.ownerKind)) persistent.add(row.ownerKind);
    if (isNonOpenclawKind(row.ownerKind)) temporary.add(row.ownerKind);
  }
  if (isOwnerKind(leaderKind)) {
    if (isPersistentOwnerKind(leaderKind)) persistent.add(leaderKind);
    if (isNonOpenclawKind(leaderKind)) temporary.add(leaderKind);
  }
  return {
    persistentKinds: dedupeKindsInKnownOrder(PERSISTENT_OWNER_KINDS, persistent),
    temporaryKinds: dedupeKindsInKnownOrder(NEW_OWNER_KINDS, temporary),
  };
}

function mergeOwnerKindAvailability(
  detected: OwnerKindsAvailability,
  used: OwnerKindsAvailability,
): OwnerKindsAvailability {
  return {
    persistentKinds: dedupeKindsInKnownOrder(
      PERSISTENT_OWNER_KINDS,
      [...detected.persistentKinds, ...used.persistentKinds],
    ),
    temporaryKinds: dedupeKindsInKnownOrder(
      NEW_OWNER_KINDS,
      [...detected.temporaryKinds, ...used.temporaryKinds],
    ),
  };
}

function ownerKindAvailableForSource(
  kind: OwnerKindDraft,
  isTemporary: boolean,
  availability: OwnerKindsAvailability,
): boolean {
  if (!isOwnerKind(kind)) return false;
  if (isTemporary) {
    return isNonOpenclawKind(kind) && availability.temporaryKinds.includes(kind);
  }
  return isPersistentOwnerKind(kind) && availability.persistentKinds.includes(kind);
}

/** Editable combobox for the temporary-agent NAME field. A single input that
 *  both free-types a brand-new temporary agent name AND opens a dropdown of the
 *  temporary agent names already defined in THIS flow (same kind). Picking one
 *  sets only the name — repo/target branch are an independent block (P2) and
 *  stay under their own fields; cross-task binding consistency is reconciled at
 *  保存子任务. */
function TempAgentCombobox({
  value,
  options,
  selectedValue,
  disabled,
  placeholder,
  onType,
  onPick,
}: {
  value: string;
  options: { id: string; repo: string; targetBranch: string; label: string }[];
  selectedValue: string;
  disabled?: boolean;
  placeholder?: string;
  onType: (text: string) => void;
  onPick: (opt: { id: string; repo: string; targetBranch: string }) => void;
}) {
  const [open, setOpen] = useState(false);
  const [active, setActive] = useState(-1);
  const rootRef = useRef<HTMLDivElement | null>(null);

  const q = value.trim().toLowerCase();
  // While the text is a committed selection (matches a candidate) show the full
  // list; once the user types something new, filter to substring matches.
  const filtered = useMemo(() => {
    if (!q || selectedValue) return options;
    return options.filter(
      (o) =>
        o.id.toLowerCase().includes(q) || o.label.toLowerCase().includes(q),
    );
  }, [options, q, selectedValue]);

  useEffect(() => {
    if (!open) return;
    const onDocMouseDown = (e: MouseEvent) => {
      if (rootRef.current && !rootRef.current.contains(e.target as Node)) {
        setOpen(false);
        setActive(-1);
      }
    };
    document.addEventListener("mousedown", onDocMouseDown);
    return () => document.removeEventListener("mousedown", onDocMouseDown);
  }, [open]);

  const showDropdown = open && !disabled && filtered.length > 0;

  const pick = (opt: { id: string; repo: string; targetBranch: string }) => {
    onPick(opt);
    setOpen(false);
    setActive(-1);
  };

  return (
    <div className="relative" ref={rootRef}>
      <input
        className="input pr-8"
        placeholder={placeholder}
        value={value}
        readOnly={disabled}
        role="combobox"
        aria-expanded={showDropdown}
        autoComplete="off"
        onChange={(e) => {
          onType(e.target.value);
          setOpen(true);
          setActive(-1);
        }}
        onFocus={() => !disabled && setOpen(true)}
        onClick={() => !disabled && setOpen(true)}
        onKeyDown={(e) => {
          if (disabled) return;
          if (e.key === "ArrowDown") {
            e.preventDefault();
            if (!open) {
              setOpen(true);
              return;
            }
            setActive((i) => Math.min(i + 1, filtered.length - 1));
          } else if (e.key === "ArrowUp") {
            e.preventDefault();
            setActive((i) => Math.max(i - 1, 0));
          } else if (e.key === "Enter") {
            if (showDropdown && active >= 0 && active < filtered.length) {
              e.preventDefault();
              pick(filtered[active]);
            }
          } else if (e.key === "Escape") {
            setOpen(false);
            setActive(-1);
          }
        }}
      />
      <span className="pointer-events-none absolute right-2 top-1/2 -translate-y-1/2 text-ink-400">
        <svg width="14" height="14" viewBox="0 0 20 20" fill="none" aria-hidden="true">
          <path
            d="M6 8l4 4 4-4"
            stroke="currentColor"
            strokeWidth="1.5"
            strokeLinecap="round"
            strokeLinejoin="round"
          />
        </svg>
      </span>
      {showDropdown && (
        <ul
          // Raised popover: in dark mode the base surface matches the form
          // behind it, so lift the dropdown to a lighter surface (ink-200) with
          // a crisper border + shadow so it clearly stands off the background.
          className="absolute left-0 right-0 z-20 mt-1 max-h-56 overflow-auto rounded-md border border-ink-200 bg-surface py-1 shadow-card dark:border-ink-400 dark:bg-ink-200 dark:shadow-lg"
          role="listbox"
        >
          {filtered.map((o, i) => {
            const v = tempAgentValue(o);
            const isSelected = v === selectedValue;
            const isActive = i === active;
            return (
              <li
                key={v}
                role="option"
                aria-selected={isSelected}
                className={cn(
                  "cursor-pointer px-3 py-2 text-sm",
                  // White-veil highlight reads on the lighter dark popover too.
                  isActive
                    ? "bg-ink-100 dark:bg-white/10"
                    : "hover:bg-ink-50 dark:hover:bg-white/5",
                  isSelected && "text-brand-700",
                )}
                // onMouseDown (not onClick) so it fires before the input blur.
                onMouseDown={(e) => {
                  e.preventDefault();
                  pick(o);
                }}
                onMouseEnter={() => setActive(i)}
              >
                {o.label}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}

function buildExistingOwnerOptions(
  rows: TaskRow[],
  openclawOptions: OpenclawAgentSummary[],
  opts: {
    leaderId: string;
    leaderKind: OwnerKindDraft;
    leaderRepo: string;
    leaderTargetBranch: string;
    isSummary: boolean;
    t: (key: string) => string;
  },
): ExistingOwnerOption[] {
  const {
    leaderId,
    leaderKind,
    leaderRepo,
    leaderTargetBranch,
    isSummary,
    t,
  } = opts;
  const normalizedLeaderId = leaderId.trim();
  const normalizedLeaderRepo = normalizeRepoPathForCompare(leaderRepo);
  const normalizedLeaderTargetBranch = leaderTargetBranch.trim();
  const leaderKey = normalizedLeaderId && isOwnerKind(leaderKind)
    ? (leaderKind === "openclaw"
        ? `openclaw:${normalizedLeaderId}`
        : `${leaderKind}:${normalizedLeaderId}:${normalizedLeaderRepo}:${normalizedLeaderTargetBranch}`)
    : null;
  const out: ExistingOwnerOption[] = [];
  const seen = new Set<string>();
  const add = (opt: ExistingOwnerOption) => {
    if (seen.has(opt.key)) return;
    seen.add(opt.key);
    out.push(opt);
  };

  for (const a of openclawOptions) {
    const key = `openclaw:${a.id}`;
    if (!isSummary && leaderKey && key === leaderKey) continue;
    add({
      key,
      id: a.id,
      kind: "openclaw",
      repo: "",
      targetBranch: "",
      label: `${ownerKindLabel("openclaw", t)} · ${a.name} (${a.id})`,
    });
  }

  for (const r of rows) {
    const id = r.ownerId.trim();
    if (!id) continue;
    const kind = r.ownerKind;
    if (!isOwnerKind(kind)) continue;
    const repoRaw = isNonOpenclawKind(kind) ? r.ownerRepo.trim() : "";
    const repo = isNonOpenclawKind(kind) ? normalizeRepoPathForCompare(repoRaw) : "";
    const targetBranch = isNonOpenclawKind(kind) ? r.ownerTargetBranch.trim() : "";
    const key = isOpenclawKind(kind)
      ? `openclaw:${id}`
      : `${kind}:${id}:${repo}:${targetBranch}`;
    if (!isSummary && leaderKey && key === leaderKey) continue;
    add({
      key,
      id,
      kind,
      repo: repoRaw,
      targetBranch,
      label: isOpenclawKind(kind)
        ? `${ownerKindLabel(kind, t)} · ${id}`
        : `${ownerKindLabel(kind, t)} · ${id} (${repoRaw || "—"} @ ${targetBranch || "—"})`,
    });
  }

  return out;
}

function collectKnownAgentIds(
  rows: TaskRow[],
  excludeRowKey?: string,
): Set<string> {
  const ids = new Set<string>();
  for (const r of rows) {
    if (excludeRowKey && r.rowKey === excludeRowKey) continue;
    const id = r.ownerId.trim();
    if (id) ids.add(id);
  }
  return ids;
}

function uniqueSummaryId(existing: TaskRow[]): string {
  const taken = new Set(existing.map((r) => r.id.trim()).filter(Boolean));
  if (!taken.has("summary")) return "summary";
  let i = 2;
  while (taken.has(`summary-${i}`)) i += 1;
  return `summary-${i}`;
}

// ──────────────────────────────────────────────────────────────────────


export function FlowEditor() {
  const { id } = useParams();
  const { t } = useTranslation();
  const { confirm, alert } = useDialog();
  const isNew = !id || id === "new";
  const navigate = useNavigate();

  // Editable fields are mirrored to sessionStorage (keyed per flow id) so a
  // user's in-progress edits survive switching to another sidebar tab and
  // returning — the editor unmounts on navigation, and plain useState would be
  // lost. The draft is seeded from the server exactly once (see `hydrated`
  // below) and cleared on a successful save (`clearFlowEditorDraft`).
  const idKey = id ?? "new";
  const draftKey = (field: string) => `flow-editor:${idKey}:${field}`;
  const clearFlowEditorDraft = () => clearSessionBackedKeys(`flow-editor:${idKey}:`);

  const [name, setName] = useSessionBackedState(draftKey("name"), "");
  const [description, setDescription] = useSessionBackedState(draftKey("description"), "");
  const [leaderId, setLeaderId] = useSessionBackedState(draftKey("leaderId"), "");
  // Leader kind starts empty; user must pick explicitly.
  const [leaderKind, setLeaderKind] = useSessionBackedState<OwnerKindDraft>(
    draftKey("leaderKind"),
    "",
  );
  // Leader source: false = existing/persistent agent, true = temporary ad-hoc.
  // OpenClaw default → existing.
  const [leaderIsTemporary, setLeaderIsTemporary] = useSessionBackedState(
    draftKey("leaderIsTemporary"),
    false,
  );
  const [leaderRepo, setLeaderRepo] = useSessionBackedState(draftKey("leaderRepo"), "");
  const [leaderTargetBranch, setLeaderTargetBranch] = useSessionBackedState(
    draftKey("leaderTargetBranch"),
    "",
  );
  const [leaderBranchOptions, setLeaderBranchOptions] = useState<string[]>([]);
  const [leaderBranchEditable, setLeaderBranchEditable] = useState(false);
  const [leaderBranchLoading, setLeaderBranchLoading] = useState(false);
  /** Repo path last committed for validation (blur / pick / select), not every keystroke. */
  const [leaderRepoToCheck, setLeaderRepoToCheck] = useState("");
  const leaderRepoCheckSeededRef = useRef(false);
  /** Repo + branch before the current edit (focus / select / pick); restored on check failure. */
  const leaderRepoBeforeEditRef = useRef({ repo: "", branch: "" });

  function resetLeaderRepoCheck() {
    setLeaderRepoToCheck("");
    leaderRepoCheckSeededRef.current = false;
  }

  function snapshotLeaderRepoBeforeEdit() {
    leaderRepoBeforeEditRef.current = {
      repo: leaderRepo,
      branch: leaderTargetBranch,
    };
  }

  function revertLeaderRepoAfterEdit() {
    const snap = leaderRepoBeforeEditRef.current;
    setLeaderRepo(snap.repo);
    setLeaderTargetBranch(snap.branch);
    setLeaderRepoToCheck(snap.repo.trim());
  }

  function commitLeaderRepoCheck(repo: string) {
    setLeaderRepoToCheck(repo.trim());
    leaderRepoCheckSeededRef.current = true;
  }
  const [leaderPickingRepo, setLeaderPickingRepo] = useState(false);
  const [runInputFields, setRunInputFieldsState] = useSessionBackedState<string[]>(
    draftKey("runInputFields"),
    [],
  );
  // "省心模式" (easy mode) / "开发者模式" (developer mode): persisted in
  // spec.variables; default OFF; mutually exclusive (see toggle handlers).
  const [easyMode, setEasyModeState] = useSessionBackedState<boolean>(
    draftKey("easyMode"),
    false,
  );
  const [easyModeNoticeOpen, setEasyModeNoticeOpen] = useState(false);
  const [devMode, setDevModeState] = useSessionBackedState<boolean>(
    draftKey("devMode"),
    false,
  );
  const [devModeNoticeOpen, setDevModeNoticeOpen] = useState(false);
  const [autoMergeSyncNoticeOpen, setAutoMergeSyncNoticeOpen] = useState(false);
  const autoMergeSyncNoticeTimerRef = useRef<number | null>(null);
  const [runInputFieldDraft, setRunInputFieldDraft] = useState("");
  const [runInputFieldError, setRunInputFieldError] = useState<string | null>(null);
  const [version, setVersion] = useSessionBackedState<number | null>(draftKey("version"), null);
  const [tasks, setTasks] = useSessionBackedState<TaskRow[]>(draftKey("tasks"), []);
  // Seed-from-server guard: true once the existing flow has been loaded into
  // the draft, so remounts (tab switches) never re-fetch over unsaved edits.
  const [hydrated, setHydrated] = useSessionBackedState<boolean>(
    draftKey("hydrated"),
    false,
    { isClosed: (v) => !v },
  );
  const [openclawOptions, setOpenclawOptions] = useState<OpenclawAgentSummary[]>([]);
  const [hermesOptions, setHermesOptions] = useState<HermesAgentSummary[]>([]);
  const [detectedOwnerKinds, setDetectedOwnerKinds] = useState<OwnerKindsAvailability>(
    EMPTY_OWNER_KINDS,
  );
  const [deploymentMode, setDeploymentMode] = useState<DeploymentMode>("local");
  const [workspaceDirOptions, setWorkspaceDirOptions] = useState<string[]>([]);
  const ownerKindsFetchRef = useRef<Promise<OwnerKindsAvailability> | null>(null);
  const detectedOwnerKindsRef = useRef<OwnerKindsAvailability>(EMPTY_OWNER_KINDS);
  const [error, setError] = useState<string | null>(null);
  /** Aggregated list of unmet validation rules surfaced when the user
   *  clicks Save. Rendered as a bulleted list above the form so the
   *  user can fix every issue in one pass instead of resubmitting
   *  once per error. */
  const [saveBlockers, setSaveBlockers] = useState<string[] | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [repoIssue, setRepoIssue] = useSessionBackedState<RepoIssue | null>(
    "flow-editor:repo-issue",
    null,
    { isClosed: (value) => value === null },
  );
  const [pendingSavePayload, setPendingSavePayload] = useSessionBackedState<FlowSavePayload | null>(
    "flow-editor:pending-save-payload",
    null,
    { isClosed: (value) => value === null },
  );
  const [fixingRepo, setFixingRepo] = useState(false);
  const [decomposeOpen, setDecomposeOpen] = useSessionBackedModalFlag(
    "flow-editor:decompose-open",
  );
  const [mergeOpen, setMergeOpen] = useSessionBackedModalFlag(
    "flow-editor:merge-open",
  );
  /**
   * Editing target. ``mode="create"`` shows a blank draft and only commits
   * the new row on Save; ``edit`` / ``view`` show the existing row by
   * ``rowKey``. ``view`` is read-only.
   */
  const [editing, setEditing] = useSessionBackedState<
    | { mode: "create"; draft: TaskRow }
    | { mode: "edit" | "view"; rowKey: string }
    | null
  >("flow-editor:editing", null, { isClosed: (value) => value === null });

  function showAutoMergeSyncNotice() {
    setAutoMergeSyncNoticeOpen(true);
    if (autoMergeSyncNoticeTimerRef.current !== null) {
      window.clearTimeout(autoMergeSyncNoticeTimerRef.current);
    }
    autoMergeSyncNoticeTimerRef.current = window.setTimeout(() => {
      setAutoMergeSyncNoticeOpen(false);
      autoMergeSyncNoticeTimerRef.current = null;
    }, 1000);
  }

  useEffect(() => {
    return () => {
      if (autoMergeSyncNoticeTimerRef.current !== null) {
        window.clearTimeout(autoMergeSyncNoticeTimerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    detectedOwnerKindsRef.current = detectedOwnerKinds;
  }, [detectedOwnerKinds]);

  async function refreshOwnerKindsFast(
    opts: { silent?: boolean } = {},
  ): Promise<OwnerKindsAvailability> {
    if (ownerKindsFetchRef.current) return ownerKindsFetchRef.current;
    const job = (async () => {
      try {
        const out = await api.getOwnerKindsFast();
        const next: OwnerKindsAvailability = {
          persistentKinds: dedupeKindsInKnownOrder(
            PERSISTENT_OWNER_KINDS,
            out.persistentKinds
              .map((kind) => toPersistentOwnerKind(String(kind)))
              .filter((kind): kind is OwnerKind => kind !== null),
          ),
          temporaryKinds: dedupeKindsInKnownOrder(
            NEW_OWNER_KINDS,
            out.temporaryKinds
              .map((kind) => toTempOwnerKind(String(kind)))
              .filter((kind): kind is NonOpenclawOwnerKind => kind !== null),
          ),
        };
        setDetectedOwnerKinds(next);
        return next;
      } catch (e) {
        if (!opts.silent) {
          const msg = e instanceof ApiError ? e.message : String(e);
          setError(msg);
        }
        return detectedOwnerKindsRef.current;
      } finally {
        ownerKindsFetchRef.current = null;
      }
    })();
    ownerKindsFetchRef.current = job;
    return job;
  }

  // Load option lists.
  useEffect(() => {
    api
      .listOpenclawAgents()
      .then((r) => setOpenclawOptions(r.items))
      .catch(() => {});
    api
      .listHermesAgents("fast")
      .then((r) => setHermesOptions(r.items))
      .catch(() => {});
    api
      .listWorkspaceDirectories()
      .then((r) => {
        setDeploymentMode(r.deploymentMode);
        setWorkspaceDirOptions(r.items);
      })
      .catch(() => {});
    void refreshOwnerKindsFast({ silent: true });
  }, []);

  // Load existing flow when editing — but only once per flow. If a draft has
  // already been hydrated (e.g. the user is returning to this tab with unsaved
  // edits), skip the fetch so we never clobber their in-progress changes.
  useEffect(() => {
    if (isNew || hydrated) return;
    api
      .getFlow(id!)
      .then((flow) => {
        setName(flow.name);
        setDescription(flow.description);
        setRunInputFieldsState(getRunInputFields(flow.spec));
        setEasyModeState(getEasyMode(flow.spec));
        setDevModeState(getDevMode(flow.spec));
        setVersion(flow.version);
        const rows = specToRows(flow.spec);
        setTasks(rows);
        const summary = rows.find((r) => r.isLeaderSummary);
        if (summary) {
          setLeaderId(summary.ownerId);
          setLeaderKind(summary.ownerKind);
          setLeaderIsTemporary(summary.ownerKind !== "openclaw" && summary.ownerIsTemporary);
          setLeaderRepo(summary.ownerRepo);
          setLeaderTargetBranch(summary.ownerTargetBranch.trim());
          commitLeaderRepoCheck(summary.ownerRepo);
        }
        setHydrated(true);
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
      });
  }, [id, isNew, hydrated]);

  // Keep the summary task in sync with leader fields.
  // Important: when leader fields are in an intermediate state (for example
  // switching kind and leaderId is temporarily empty), preserve the existing
  // summary row instead of deleting/recreating it.
  useEffect(() => {
    const normalizedLeaderId = leaderId.trim();
    const normalizedLeaderRepo = needsRepoBranchFields(leaderKind)
      ? leaderRepo.trim()
      : "";
    const normalizedLeaderTargetBranch = needsRepoBranchFields(leaderKind)
      ? leaderTargetBranch.trim()
      : "";
    const normalizedLeaderTemporary = leaderKind !== "openclaw" && leaderIsTemporary;
    setTasks((rows) => {
      const idx = rows.findIndex((r) => r.isLeaderSummary);
      if (idx >= 0) {
        const cur = rows[idx];
        const nextOwnerId = normalizedLeaderId;
        if (
          cur.ownerKind === leaderKind
          && cur.ownerId === nextOwnerId
          && cur.ownerRepo === normalizedLeaderRepo
          && cur.ownerTargetBranch === normalizedLeaderTargetBranch
          && cur.ownerIsTemporary === normalizedLeaderTemporary
        ) {
          return rows;
        }
        const next = rows.slice();
        next[idx] = {
          ...cur,
          ownerKind: leaderKind,
          ownerId: nextOwnerId,
          ownerRepo: normalizedLeaderRepo,
          ownerTargetBranch: normalizedLeaderTargetBranch,
          ownerIsTemporary: normalizedLeaderTemporary,
          requiresHumanCheckpoint: false,
          autoMerge: leaderKind === "openclaw" ? true : cur.autoMerge,
        };
        return next;
      }
      if (!normalizedLeaderId) {
        // New flow and no leader yet: do not auto-create a summary row.
        return rows;
      }
      const summary: TaskRow = {
        ...blankRow(),
        id: uniqueSummaryId(rows),
        subject: t("flowEditor.summaryTaskDefaultSubject"),
        ownerKind: leaderKind,
        ownerId: normalizedLeaderId,
        ownerRepo: normalizedLeaderRepo,
        ownerTargetBranch: normalizedLeaderTargetBranch,
        ownerIsTemporary: normalizedLeaderTemporary,
        // New summary rows start with empty dependencies and require explicit
        // user selection of upstream tasks.
        dependsOn: [],
        isLeaderSummary: true,
        requiresHumanCheckpoint: false,
      };
      return [...rows, summary];
    });
  }, [leaderId, leaderKind, leaderIsTemporary, leaderRepo, leaderTargetBranch, t]);

  useEffect(() => {
    if (isOpenclawKind(leaderKind)) {
      setLeaderBranchOptions([]);
      setLeaderBranchEditable(false);
      return;
    }
    const repo = leaderRepoToCheck.trim();
    if (!repo) {
      setLeaderBranchOptions([]);
      setLeaderBranchEditable(false);
      setLeaderTargetBranch((prev) => (prev ? "" : prev));
      return;
    }
    let cancelled = false;
    setLeaderBranchLoading(true);
    const branchBeforeCheck = leaderTargetBranch;
    void (async () => {
      const out = await ensureRepoAndListBranches({
        repo,
        preserveBranch: branchBeforeCheck,
        agentLabel: leaderId.trim() || t("flowEditor.repoIssue.unknownAgent"),
        confirmCreate: confirm,
        messages: repoBranchMessages(t),
      });
      if (cancelled) return;
      if (!out.ok) {
        setLeaderBranchOptions([]);
        setLeaderBranchEditable(false);
        setLeaderBranchLoading(false);
        if (out.cancelled || out.invalidPath) {
          revertLeaderRepoAfterEdit();
          if (out.invalidPath) {
            void alert(out.error);
          }
        }
        return;
      }
      setLeaderBranchOptions(out.result.branches);
      setLeaderBranchEditable(out.result.editable);
      if (out.result.path && out.result.path !== leaderRepo) {
        setLeaderRepo(out.result.path);
      }
      setLeaderTargetBranch(
        branchAfterRepoCheck(branchBeforeCheck, out.result.branches),
      );
      setLeaderBranchLoading(false);
    })();
    return () => {
      cancelled = true;
    };
    // P2: keyed off repo + kind only; editing the leader NAME (used only as a
    // confirm-dialog label) must not re-trigger the repo/branch check.
  }, [alert, confirm, leaderKind, leaderRepoToCheck, t]);

  // Seed repo validation once when restoring a saved Flow or session draft — not
  // while the user is still typing into the repo text field (that commits on blur).
  useEffect(() => {
    if (leaderRepoCheckSeededRef.current) return;
    if (isOpenclawKind(leaderKind)) return;
    const repo = leaderRepo.trim();
    if (!repo) return;
    commitLeaderRepoCheck(repo);
  }, [leaderKind, hydrated]);

  // ── derived state -------------------------------------------------

  const summaryTask = tasks.find((r) => r.isLeaderSummary);
  const leaderKey = summaryTask ? ownerKey(summaryTask) : null;
  const mergedOwnerKinds = useMemo(
    () => mergeOwnerKindAvailability(detectedOwnerKinds, usedOwnerKinds(tasks, leaderKind)),
    [detectedOwnerKinds, tasks, leaderKind],
  );

  function ownerKindLabelText(kind: OwnerKindDraft): string {
    return ownerKindLabel(kind, (key) => t(key));
  }

  function leaderKindIssueText(
    kind: OwnerKindDraft,
    temporary: boolean,
    availability: OwnerKindsAvailability,
  ): string | null {
    if (!isOwnerKind(kind)) return t("flowEditor.validation.leaderKindRequired");
    if (!ownerKindAvailableForSource(kind, temporary, availability)) {
      return t("flowEditor.validation.leaderKindUnavailable", {
        kindLabel: ownerKindLabelText(kind),
      });
    }
    return null;
  }

  function taskOwnerKindIssueText(
    row: TaskRow,
    availability: OwnerKindsAvailability,
  ): string | null {
    if (!isOwnerKind(row.ownerKind)) {
      return t("flowEditor.validation.ownerKindRequired");
    }
    if (!ownerKindAvailableForSource(row.ownerKind, row.ownerIsTemporary, availability)) {
      return t("flowEditor.validation.ownerKindUnavailable", {
        subject: row.subject.trim() || row.id || t("flowEditor.rowUntitled"),
        kindLabel: ownerKindLabelText(row.ownerKind),
      });
    }
    return null;
  }

  const validationMessages = useMemo<ValidationMessages>(
    () => ({
      leaderKindRequired: t("flowEditor.validation.leaderKindRequired"),
      leaderKindUnavailable: (kindLabel: string) =>
        t("flowEditor.validation.leaderKindUnavailable", { kindLabel }),
      pickOneSummary: t("flowEditor.validation.pickOneSummary"),
      onlyOneSummary: t("flowEditor.validation.onlyOneSummary"),
      taskIdRequired: t("flowEditor.validation.taskIdRequired"),
      taskIdPattern: t("flowEditor.validation.taskIdPattern"),
      duplicateTaskId: (taskId: string) =>
        t("flowEditor.validation.duplicateTaskId", { taskId }),
      subjectRequired: t("flowEditor.validation.subjectRequired"),
      ownerKindRequired: t("flowEditor.validation.ownerKindRequired"),
      ownerKindUnavailable: ({ subject, kindLabel }: { subject: string; kindLabel: string }) =>
        t("flowEditor.validation.ownerKindUnavailable", { subject, kindLabel }),
      pickOpenclawAgent: t("flowEditor.validation.pickOpenclawAgent"),
      ownerAgentNameRequired: t("flowEditor.validation.ownerAgentNameRequired"),
      ownerAgentIdPattern: t("flowEditor.validation.ownerAgentIdPattern"),
      claudeRepoRequired: t("flowEditor.validation.claudeRepoRequired"),
      claudeTargetBranchRequired: t("flowEditor.validation.claudeTargetBranchRequired"),
      leaderCannotOwnNonSummary: t(
        "flowEditor.validation.leaderCannotOwnNonSummary",
      ),
      summaryNeedsDependency: t("flowEditor.validation.summaryNeedsDependency"),
      ownerRepoBranchMismatch: (agentId: string) =>
        t("flowEditor.validation.ownerRepoBranchMismatch", { agentId }),
      duplicateAgentIdCrossKind: (agentId: string) =>
        t("flowEditor.validation.duplicateAgentIdCrossKind", { agentId }),
      cycleDetected: (cyclePath: string) =>
        t("flowEditor.validation.cycleDetected", { cyclePath }),
      descriptionRequired: (subject: string) =>
        t("flowEditor.validation.descriptionRequired", { subject }),
      openclawAgentMissing: (subject: string, agentId: string) =>
        t("flowEditor.validation.openclawAgentMissing", { subject, agentId }),
      hermesAgentMissing: (subject: string, agentId: string) =>
        t("flowEditor.validation.hermesAgentMissing", { subject, agentId }),
    }),
    [t],
  );
  // The "stale OpenClaw owner" check only runs once we've actually loaded
  // the user's agent list — otherwise a slow fetch would falsely flag
  // every reference. Cheapest proxy for "loaded": at least one option
  // present. Empty list means either truly zero agents (UI already
  // surfaces that) or the request hasn't returned yet.
  const openclawIds = useMemo(
    () => new Set(openclawOptions.map((a) => a.id)),
    [openclawOptions],
  );
  // Same loaded-yet guard as openclawIds: only flag a stale persistent Hermes
  // owner once the Hermes list has actually loaded (non-empty).
  const hermesIds = useMemo(
    () => new Set(hermesOptions.map((a) => a.id)),
    [hermesOptions],
  );
  const issues = useMemo(
    () => validate(tasks, validationMessages, openclawIds, hermesIds),
    [tasks, validationMessages, openclawIds, hermesIds],
  );
  // Auto-dismiss the save-blockers rail as soon as the user starts
  // fixing things — otherwise a stale list lingers until the next
  // Save click and feels broken.
  useEffect(() => {
    setSaveBlockers(null);
  }, [name, description, leaderId, leaderKind, leaderIsTemporary, leaderRepo, leaderTargetBranch, tasks]);
  const globalIssues = useMemo(
    () => Array.from(new Set(issues.filter((i) => !i.rowKey).map((i) => i.message))),
    [issues],
  );

  const orderedTasks = useMemo(() => {
    const summary = tasks.filter((r) => r.isLeaderSummary);
    const rest = tasks.filter((r) => !r.isLeaderSummary);
    return [...rest, ...summary];
  }, [tasks]);

  const decomposeDisabledReason = useMemo(() => {
    // The leader Agent NAME (leaderId) is ALWAYS required to run "AI 拆解",
    // regardless of source — a new/temporary leader and an existing one both
    // need it. Keep this as the unconditional first guard; never gate it behind
    // leaderIsTemporary. (For a temporary leader the only thing we relax below
    // is the managed-picklist *membership* check, not the name requirement.)
    if (!leaderId.trim()) {
      return t("flowEditor.validation.pickLeader");
    }
    const kindIssue = leaderKindIssueText(leaderKind, leaderIsTemporary, mergedOwnerKinds);
    if (kindIssue) return kindIssue;
    if (leaderKind === "openclaw") {
      if (openclawOptions.length === 0) {
        return t("flowEditor.decompose.leaderEmpty");
      }
      if (!openclawOptions.some((a) => a.id === leaderId.trim())) {
        return t("flowEditor.validation.pickLeader");
      }
      return null;
    }
    // A temporary (ad-hoc / "new") leader is typed as free text and never
    // appears in the managed-agent picklist, so only require picklist
    // membership for existing/persistent leaders.
    if (!leaderIsTemporary && isOwnerKind(leaderKind) && isManagedPickKind(leaderKind)) {
      // Managed agent required (no ad-hoc creation), plus a working dir.
      const opts = pickAgentsForKind(leaderKind, hermesOptions);
      if (!opts.some((a) => a.id === leaderId.trim())) {
        return t("flowEditor.validation.pickLeader");
      }
    }
    if (!leaderRepo.trim()) {
      return t("flowEditor.decompose.leaderRepoRequired");
    }
    if (!leaderTargetBranch.trim()) {
      return t("flowEditor.validation.claudeTargetBranchRequired");
    }
    return null;
  }, [
    leaderId,
    leaderKind,
    leaderIsTemporary,
    leaderRepo,
    leaderTargetBranch,
    mergedOwnerKinds,
    openclawOptions,
    hermesOptions,
    t,
  ]);

  const editingRow: TaskRow | null = (() => {
    if (!editing) return null;
    if (editing.mode === "create") return editing.draft;
    return tasks.find((r) => r.rowKey === editing.rowKey) ?? null;
  })();

  // ── mutators ------------------------------------------------------

  function openNewTask() {
    // Leader gates everything: it owns the auto-summary, supplies the
    // exclusion target for the sub-task agent picker, and is referenced
    // by validation. Without one, opening the editor would let users
    // create invalid drafts.
    if (!leaderId.trim()) {
      void alert(t("flowEditor.pickLeaderFirst"));
      return;
    }
    setEditing({ mode: "create", draft: blankRow() });
  }

  function openMerge() {
    if (!leaderId.trim()) {
      void alert(t("flowEditor.mergePickLeaderFirst"));
      return;
    }
    setMergeOpen(true);
  }

  function addRunInputField(raw: string) {
    const cleaned = raw.trim();
    if (!cleaned) {
      setRunInputFieldError(t("flowEditor.runInputRequirementRequired"));
      return;
    }
    setRunInputFieldError(null);
    setRunInputFieldsState((prev) => {
      if (prev.includes(cleaned)) return prev;
      return [...prev, cleaned];
    });
    setRunInputFieldDraft("");
  }

  function removeRunInputField(field: string) {
    setRunInputFieldsState((prev) => prev.filter((x) => x !== field));
  }

  /**
   * Commit a merge: append the non-summary tasks of each chosen source
   * Flow into the current task list (with fresh ids + remapped
   * ``dependsOn``), and concatenate any per-source execution
   * pre-specification text under a section header.
   *
   * Caller (the modal) has already verified that no sub-task in any
   * source Flow uses the current leader as its OpenClaw owner.
   */
  function applyMerge(details: FlowDetail[]) {
    const appended: TaskRow[] = [];
    const mergedFieldSet = new Set(runInputFields.map((x) => x.trim()).filter(Boolean));
    for (const flow of details) {
      const sourceRows = specToRows(flow.spec).filter((r) => !r.isLeaderSummary);
      // Old-task-id → new-task-id map so dependsOn references survive
      // the rewrite. Source flows that reference an upstream task
      // outside this batch lose those links (they're filtered out).
      const idMap = new Map<string, string>();
      for (const r of sourceRows) idMap.set(r.id, newTaskId());
      for (const r of sourceRows) {
        appended.push({
          ...r,
          rowKey: newRowKey(),
          id: idMap.get(r.id)!,
          dependsOn: r.dependsOn
            .map((d) => idMap.get(d))
            .filter((x): x is string => !!x),
          // Defensive: a merged flow's summary owner snuck into a worker
          // row is impossible per the conflict pre-check, but the
          // server-side invariant says leaders never own worker tasks
          // either way, so we don't need to rewrite ownerId here.
        });
      }
      for (const field of getRunInputFields(flow.spec)) {
        mergedFieldSet.add(field.trim());
      }
    }

    setTasks((rows) => {
      const others = rows.filter((r) => !r.isLeaderSummary);
      const summary = rows.filter((r) => r.isLeaderSummary);
      return [...others, ...appended, ...summary];
    });

    setRunInputFieldsState(Array.from(mergedFieldSet).filter(Boolean));

    setMergeOpen(false);
    setError(null);
    if (details.length > 0) {
      // Soft confirmation — alert is annoying if many merges in a row,
      // but a tiny success line in the page-level error rail keeps
      // it visible without a popup.
      void alert(
        t("flowEditor.mergeModal.mergedSummary", {
          flowCount: details.length,
          taskCount: appended.length,
        }),
      );
    }
  }

  /** Guard the leader picker: if the candidate is currently set as the
   *  Owner of any non-summary task, refuse to switch (leader cannot
   *  double as a worker; this matches the server-side validator). */
  function tryChangeLeader(nextId: string) {
    const normalized = nextId.trim();
    if (!normalized) {
      setLeaderId("");
      return;
    }
    const conflicting = tasks.some(
      (r) =>
        !r.isLeaderSummary &&
        r.ownerId === normalized,
    );
    if (conflicting) {
      const agent =
        openclawOptions.find((a) => a.id === normalized) ??
        hermesOptions.find((a) => a.id === normalized);
      void alert(
        t("flowEditor.leaderInUseByTask", {
          name: agent ? `${agent.name} (${agent.id})` : normalized,
        }),
      );
      return; // keep leaderId untouched — select snaps back via controlled value
    }
    if (isOwnerKind(leaderKind) && isManagedPickKind(leaderKind)) {
      // Managed kinds (Hermes/Claude/Codex) bind identity via the managed id,
      // but the working directory (repo/branch) is chosen separately — preserve it.
      setLeaderId(normalized);
      return;
    }
    setLeaderId(normalized);
  }

  /** Commit a draft task from the create-mode modal, inserting it BEFORE
   *  the summary task so the summary stays pinned at the end. */
  function commitNewTask(row: TaskRow) {
    setTasks((rows) => {
      const rowForInsert = enforceOpenclawAutoMerge(
        applyOwnerBinding(row, normalizedOwnerBinding(row)),
      );
      const ownerId = rowForInsert.ownerId.trim();
      const baseRows = ownerId
        ? syncOwnerBindingAcrossSubtasks(rows, ownerId, rowForInsert).rows
        : rows;
      const summaryIdx = baseRows.findIndex((r) => r.isLeaderSummary);
      if (summaryIdx === -1) return [...baseRows, rowForInsert];
      return [
        ...baseRows.slice(0, summaryIdx),
        rowForInsert,
        ...baseRows.slice(summaryIdx),
      ];
    });
  }

  /** Apply an entire row replacement from edit-mode modal save. */
  function applyEditedRow(rowKey: string, replacement: TaskRow) {
    setTasks((rows) => {
      const prev = rows.find((r) => r.rowKey === rowKey);
      const prevId = prev?.id.trim() || "";
      const nextId = replacement.id.trim();
      const renamed = prevId.length > 0 && nextId.length > 0 && prevId !== nextId;
      const normalizedReplacement = enforceOpenclawAutoMerge(
        applyOwnerBinding(
          replacement,
          normalizedOwnerBinding(replacement),
        ),
      );
      let nextRows = rows.map((r) =>
        r.rowKey === rowKey
          ? { ...normalizedReplacement, rowKey }
          : r,
      );
      const replacementOwnerId = normalizedReplacement.ownerId.trim();
      if (replacementOwnerId) {
        nextRows = syncOwnerBindingAcrossSubtasks(
          nextRows,
          replacementOwnerId,
          normalizedReplacement,
          rowKey,
        ).rows;
      }
      if (!renamed) return nextRows;
      return nextRows.map((nextRow) => {
        const mapped = nextRow.dependsOn.map((dep) => (dep === prevId ? nextId : dep));
        const deduped = Array.from(new Set(mapped));
        const same =
          deduped.length === nextRow.dependsOn.length
          && deduped.every((dep, idx) => dep === nextRow.dependsOn[idx]);
        if (same) return nextRow;
        return { ...nextRow, dependsOn: deduped };
      });
    });
  }

  function removeRow(rowKey: string) {
    setTasks((rows) => {
      const target = rows.find((r) => r.rowKey === rowKey);
      if (!target || target.isLeaderSummary) return rows;
      const remaining = rows.filter((r) => r.rowKey !== rowKey);
      const removedId = target.id;
      return remaining.map((r) => ({
        ...r,
        dependsOn: removedId
          ? r.dependsOn.filter((d) => d !== removedId)
          : r.dependsOn,
      }));
    });
  }

  /**
   * Reorder non-summary tasks. The summary row pins to the end visually
   * and is excluded from the swap.
   */
  function moveRow(rowKey: string, dir: -1 | 1) {
    setTasks((rows) => {
      const nonSummary = rows.filter((r) => !r.isLeaderSummary);
      const summary = rows.filter((r) => r.isLeaderSummary);
      const i = nonSummary.findIndex((r) => r.rowKey === rowKey);
      const j = i + dir;
      if (i < 0 || j < 0 || j >= nonSummary.length) return rows;
      const out = nonSummary.slice();
      [out[i], out[j]] = [out[j], out[i]];
      return [...out, ...summary];
    });
  }

  // ── submit --------------------------------------------------------

  function buildSavePayload(): FlowSavePayload {
    return {
      name,
      description,
      cleanupTeamOnFinish: true,
      spec: setDevMode(
        setEasyMode(
          rowsToSpec(enforceOpenclawAutoMergeAll(tasks), runInputFields),
          easyMode,
        ),
        devMode,
      ),
    };
  }

  function warningText(warning: FlowSaveWarning): string {
    if (warning.code === "OPENCLAW_RUNTIME_NOT_RUNNING") {
      return t("flowEditor.saveWarnings.openclawRuntimeNotRunning");
    }
    return t("flowEditor.saveWarnings.generic", {
      message: warning.message || warning.code,
    });
  }

  function notifySaveWarnings(warnings: FlowSaveWarning[] | undefined) {
    if (!warnings || warnings.length === 0) return;
    const text = warnings.map((item) => warningText(item)).join("\n\n");
    void alert(text);
  }

  async function persistFlow(payload: FlowSavePayload): Promise<PersistFlowResult> {
    if (isNew) {
      return api.createFlow(payload);
    }
    return api.updateFlow(id!, { ...payload, version: version ?? 1 });
  }

  function extractRepoIssue(e: ApiError): RepoIssue | null {
    if (e.code !== "INVALID_REPO") return null;
    const details = e.details ?? {};
    const repo = String(details.repo ?? "").trim();
    if (!repo) return null;
    const aid = String(details.agent_id ?? details.agentId ?? "").trim();
    const rawReason = String(details.reason ?? "").trim();
    const reason: RepoIssueReason =
      rawReason === "path_not_found" ||
      rawReason === "not_directory" ||
      rawReason === "not_git_repo" ||
      rawReason === "no_initial_commit"
        ? rawReason
        : "unknown";
    return {
      agentId: aid || t("flowEditor.repoIssue.unknownAgent"),
      repo,
      reason,
    };
  }

  function repoIssueReasonText(issue: RepoIssue): string {
    if (issue.reason === "path_not_found") {
      return t("flowEditor.repoIssue.reasonPathMissing");
    }
    if (issue.reason === "not_directory") {
      return t("flowEditor.repoIssue.reasonNotDirectory");
    }
    if (issue.reason === "not_git_repo") {
      return t("flowEditor.repoIssue.reasonNotGitRepo");
    }
    if (issue.reason === "no_initial_commit") {
      return t("flowEditor.repoIssue.reasonNoInitialCommit");
    }
    return t("flowEditor.repoIssue.reasonUnknown");
  }

  async function createRepoAndRetrySave() {
    if (!repoIssue || !pendingSavePayload) return;
    setFixingRepo(true);
    setSubmitting(true);
    setError(null);
    try {
      await api.ensureGitRepo({
        path: repoIssue.repo,
        createDirIfMissing: true,
        initializeIfMissing: true,
        createInitialCommitIfMissing: true,
      });
      const result = await persistFlow(pendingSavePayload);
      setRepoIssue(null);
      setPendingSavePayload(null);
      notifySaveWarnings(result.warnings);
      clearFlowEditorDraft();
      navigate("/flows");
    } catch (e) {
      if (e instanceof ApiError) {
        const nextIssue = extractRepoIssue(e);
        if (nextIssue) {
          setRepoIssue(nextIssue);
          return;
        }
        setError(`${e.code}: ${e.message}`);
      } else {
        setError(String(e));
      }
    } finally {
      setFixingRepo(false);
      setSubmitting(false);
    }
  }

  function reselectRepoPath() {
    if (!repoIssue) return;
    setRepoIssue(null);
    setPendingSavePayload(null);
    setError(
      t("flowEditor.repoIssue.reselectHint", {
        agentId: repoIssue.agentId,
        repo: repoIssue.repo,
      }),
    );
  }

  /** P8 — verify every DISTINCT non-OpenClaw agent's repo exists (creating it on
   *  confirm) and that its target branch exists in that repo. Returns localized
   *  blocker messages (empty = all good). Reuses ensureRepoAndListBranches — the
   *  same util the inline repo fields use — so the client is authoritative for
   *  repo/branch existence (previously only field presence was checked here and
   *  existence was caught, partially, server-side). */
  async function checkAgentReposAndBranches(): Promise<string[]> {
    const blockers: string[] = [];
    const seen = new Set<string>();
    for (const r of tasks) {
      if (!isNonOpenclawKind(r.ownerKind)) continue;
      const ownerId = r.ownerId.trim();
      const repo = r.ownerRepo.trim();
      const branch = r.ownerTargetBranch.trim();
      // Presence is already gated by validate(); skip incomplete rows here.
      if (!ownerId || !repo || !branch) continue;
      const key = `${ownerId}\u001f${normalizeRepoPathForCompare(repo)}\u001f${branch}`;
      if (seen.has(key)) continue;
      seen.add(key);
      const out = await ensureRepoAndListBranches({
        repo,
        preserveBranch: branch,
        agentLabel: ownerId,
        confirmCreate: confirm,
        messages: repoBranchMessages(t),
      });
      if (!out.ok) {
        blockers.push(
          out.error || t("flowEditor.validation.agentRepoInvalid", { agentId: ownerId, repo }),
        );
        continue;
      }
      if (!out.result.branches.includes(branch)) {
        blockers.push(
          t("flowEditor.validation.agentBranchMissing", { agentId: ownerId, branch }),
        );
      }
    }
    return Array.from(new Set(blockers));
  }

  async function onSubmit() {
    const detectedNow = await refreshOwnerKindsFast({ silent: true });
    const ownerKindsNow = mergeOwnerKindAvailability(
      detectedNow,
      usedOwnerKinds(tasks, leaderKind),
    );
    // Aggregate every unmet condition (Flow-level + per-task) so the
    // user sees the full punch-list at once. Dedupe so the same
    // per-row issue doesn't show twice when a task has multiple rule
    // violations sharing a message.
    const blockers: string[] = [];
    if (!name.trim()) {
      blockers.push(t("flowEditor.validation.nameRequired"));
    }
    if (!description.trim()) {
      blockers.push(t("flowEditor.validation.goalRequired"));
    }
    if (!leaderId.trim()) {
      blockers.push(t("flowEditor.validation.pickLeader"));
    }
    const leaderKindIssue = leaderKindIssueText(
      leaderKind,
      leaderIsTemporary,
      ownerKindsNow,
    );
    if (leaderKindIssue) blockers.push(leaderKindIssue);
    for (const row of tasks) {
      const issue = taskOwnerKindIssueText(row, ownerKindsNow);
      if (issue) blockers.push(issue);
    }
    for (const issue of issues) blockers.push(issue.message);
    const uniqBlockers = Array.from(new Set(blockers));

    if (uniqBlockers.length > 0) {
      setSaveBlockers(uniqBlockers);
      setError(null);
      // Scroll back to the error rail so the user actually notices it
      // — without this the rail can be off-screen below a long task
      // list when the Save button is at the page header.
      if (typeof window !== "undefined") {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      return;
    }
    setSaveBlockers(null);
    setError(null);
    setRepoIssue(null);
    setPendingSavePayload(null);
    setSubmitting(true);
    // P8 — repo + branch existence for every agent before we POST.
    const existenceBlockers = await checkAgentReposAndBranches();
    if (existenceBlockers.length > 0) {
      setSaveBlockers(existenceBlockers);
      setSubmitting(false);
      if (typeof window !== "undefined") {
        window.scrollTo({ top: 0, behavior: "smooth" });
      }
      return;
    }
    try {
      const payload = buildSavePayload();
      const result = await persistFlow(payload);
      notifySaveWarnings(result.warnings);
      // Saving always returns the user to the Flow list (per UX spec).
      clearFlowEditorDraft();
      navigate("/flows");
    } catch (e) {
      if (e instanceof ApiError) {
        const issue = extractRepoIssue(e);
        if (issue) {
          setRepoIssue(issue);
          setPendingSavePayload(buildSavePayload());
          return;
        }
        setError(`${e.code}: ${e.message}`);
      } else {
        setError(String(e));
      }
    } finally {
      setSubmitting(false);
    }
  }

  // ── decompose ----------------------------------------------------

  async function validateLeaderRepoAndBranchForDecompose(): Promise<boolean> {
    if (!isNonOpenclawKind(leaderKind)) return true;
    const repo = leaderRepo.trim();
    if (!repo) {
      setError(t("flowEditor.decompose.leaderRepoRequired"));
      return false;
    }
    commitLeaderRepoCheck(repo);
    const branchBeforeCheck = leaderTargetBranch;
    const out = await ensureRepoAndListBranches({
      repo,
      preserveBranch: branchBeforeCheck,
      agentLabel: leaderId.trim() || t("flowEditor.repoIssue.unknownAgent"),
      confirmCreate: confirm,
      messages: repoBranchMessages(t),
    });
    if (!out.ok) {
      if (out.invalidPath) {
        void alert(out.error);
      } else if (out.error) {
        setError(out.error);
      }
      return false;
    }
    setLeaderBranchOptions(out.result.branches);
    setLeaderBranchEditable(out.result.editable);
    if (out.result.path && out.result.path !== leaderRepo) {
      setLeaderRepo(out.result.path);
    }
    const branch = leaderTargetBranch.trim();
    if (!branch) {
      setError(t("flowEditor.validation.claudeTargetBranchRequired"));
      return false;
    }
    if (!out.result.branches.includes(branch)) {
      setError(t("flowEditor.taskBranchCheck.notFound"));
      return false;
    }
    return true;
  }

  async function openDecompose() {
    if (!description.trim()) {
      setError(t("flowEditor.validation.goalRequired"));
      return;
    }
    if (!leaderId.trim()) {
      setError(t("flowEditor.validation.pickLeader"));
      return;
    }
    const detectedNow = await refreshOwnerKindsFast({ silent: true });
    const ownerKindsNow = mergeOwnerKindAvailability(
      detectedNow,
      usedOwnerKinds(tasks, leaderKind),
    );
    const kindIssue = leaderKindIssueText(leaderKind, leaderIsTemporary, ownerKindsNow);
    if (kindIssue) {
      setError(kindIssue);
      return;
    }
    if (leaderKind === "openclaw") {
      if (openclawOptions.length === 0) {
        setError(t("flowEditor.decompose.leaderEmpty"));
        return;
      }
      if (!openclawOptions.some((a) => a.id === leaderId.trim())) {
        setError(t("flowEditor.validation.pickLeader"));
        return;
      }
    } else {
      if (
        !leaderIsTemporary &&
        isOwnerKind(leaderKind) &&
        isManagedPickKind(leaderKind) &&
        !pickAgentsForKind(leaderKind, hermesOptions).some(
          (a) => a.id === leaderId.trim(),
        )
      ) {
        setError(t("flowEditor.validation.pickLeader"));
        return;
      }
      const repoReady = await validateLeaderRepoAndBranchForDecompose();
      if (!repoReady) return;
    }
    setError(null);
    setDecomposeOpen(true);
  }

  async function onPickLeaderRepo() {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    snapshotLeaderRepoBeforeEdit();
    setLeaderPickingRepo(true);
    try {
      const out = await api.pickDirectory({
        title: t("flowEditor.taskFields.pickDirTitle"),
        initialPath: leaderRepo || undefined,
      });
      if (out.path) {
        setLeaderRepo(out.path);
        setLeaderBranchOptions([]);
        setLeaderBranchEditable(false);
        commitLeaderRepoCheck(out.path);
      }
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof Error
          ? e.message
          : String(e);
      void alert(t("flowEditor.pickDirFailed", { message: msg }));
    } finally {
      setLeaderPickingRepo(false);
    }
  }

  // ── render --------------------------------------------------------

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">
          {isNew ? t("flowEditor.titleNew") : t("flowEditor.titleEdit")}
        </h1>
        <div className="flex gap-2">
          <button
            className="btn-outline"
            onClick={() => {
              // Cancel discards the in-progress edits: drop the persisted draft
              // so returning to the editor shows the saved server state, not the
              // abandoned changes.
              clearFlowEditorDraft();
              navigate("/flows");
            }}
            disabled={submitting}
          >
            {t("common.cancel")}
          </button>
          <button
            className="btn-primary"
            onClick={onSubmit}
            disabled={submitting}
          >
            {submitting ? t("flowEditor.saving") : t("flowEditor.save")}
          </button>
        </div>
      </div>

      {saveBlockers && saveBlockers.length > 0 && (
        <ErrorBox>
          <div className="font-medium mb-1">
            {t("flowEditor.validation.saveBlockedTitle")}
          </div>
          <ul className="list-disc pl-5 space-y-0.5">
            {saveBlockers.map((msg, i) => (
              <li key={i}>{msg}</li>
            ))}
          </ul>
        </ErrorBox>
      )}
      {error && <ErrorBox>{error}</ErrorBox>}

      <Card>
        <CardTitle>{t("flowEditor.flowBasics")}</CardTitle>
        <div className="mb-2 flex items-center justify-between rounded-lg border border-emerald-100/90 bg-emerald-50/60 px-3 py-2">
          <div className="pr-3">
            <div className="text-sm font-medium text-ink-800">{t("flowEditor.easyMode")}</div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={easyMode}
            onClick={() => {
              const next = !easyMode;
              setEasyModeState(next);
              // Mutually exclusive with developer mode.
              if (next) {
                setDevModeState(false);
                setEasyModeNoticeOpen(true);
              }
            }}
            className={cn(
              "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors",
              easyMode ? "bg-emerald-500" : "bg-ink-300",
            )}
          >
            <span
              className={cn(
                "inline-block h-5 w-5 transform rounded-full bg-surface shadow transition-transform",
                easyMode ? "translate-x-5" : "translate-x-1",
              )}
            />
          </button>
        </div>
        <div className="mb-4 flex items-center justify-between rounded-lg border border-purple-100/90 bg-purple-50/60 px-3 py-2">
          <div className="pr-3">
            <div className="text-sm font-medium text-ink-800">{t("flowEditor.devMode")}</div>
          </div>
          <button
            type="button"
            role="switch"
            aria-checked={devMode}
            onClick={() => {
              const next = !devMode;
              setDevModeState(next);
              // Mutually exclusive with easy mode.
              if (next) {
                setEasyModeState(false);
                setDevModeNoticeOpen(true);
              }
            }}
            className={cn(
              "relative inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors",
              devMode ? "bg-purple-500" : "bg-ink-300",
            )}
          >
            <span
              className={cn(
                "inline-block h-5 w-5 transform rounded-full bg-surface shadow transition-transform",
                devMode ? "translate-x-5" : "translate-x-1",
              )}
            />
          </button>
        </div>
        <Modal
          open={easyModeNoticeOpen}
          onClose={() => setEasyModeNoticeOpen(false)}
          title={t("flowEditor.easyMode")}
        >
          <p className="whitespace-pre-line text-sm text-ink-700">
            {t("flowEditor.easyModeNotice")}
          </p>
          <div className="mt-4 flex justify-end">
            <button className="btn btn-primary" onClick={() => setEasyModeNoticeOpen(false)}>
              {t("flowEditor.easyModeAck")}
            </button>
          </div>
        </Modal>
        <Modal
          open={devModeNoticeOpen}
          onClose={() => setDevModeNoticeOpen(false)}
          title={t("flowEditor.devMode")}
        >
          <p className="whitespace-pre-line text-sm text-ink-700">
            {t("flowEditor.devModeNotice")}
          </p>
          <div className="mt-4 flex justify-end">
            <button className="btn btn-primary" onClick={() => setDevModeNoticeOpen(false)}>
              {t("flowEditor.devModeAck")}
            </button>
          </div>
        </Modal>
        <div className="grid grid-cols-1 gap-4">
          <div>
            <label className="label">{t("flowEditor.flowName")} *</label>
            <input
              className="input"
              value={name}
              placeholder={t("flowEditor.namePlaceholder")}
              onChange={(e) => setName(e.target.value)}
            />
          </div>
          <div>
            <label className="label">{t("flowEditor.overallGoal")} *</label>
            <textarea
              className="textarea h-24"
              value={description}
              placeholder={t("flowEditor.descriptionPlaceholder")}
              onChange={(e) => setDescription(e.target.value)}
            />
          </div>
          <div>
            <div className="flex items-center justify-between gap-2">
              <label className="label mb-0">{t("flowEditor.leader")} *</label>
              <button
                type="button"
                className="btn-outline whitespace-nowrap"
                onClick={openDecompose}
                title={decomposeDisabledReason || t("flowEditor.decompose.hint")}
                disabled={!!decomposeDisabledReason}
              >
                <ChatIcon className="h-4 w-4 text-brandicon" />
                {t("flowEditor.aiDecompose")}
              </button>
            </div>
            <div className="mt-2 grid gap-2 md:grid-cols-3">
              <div>
                <label className="label">{t("flowEditor.taskFields.ownerSource")}</label>
                <select
                  className="select"
                  value={leaderIsTemporary ? "new" : "existing"}
                  onChange={(e) => {
                    const nextMode = e.target.value as OwnerMode;
                    // Toggling source keeps repo/branch as-is, but requires the
                    // user to explicitly re-pick owner kind + id.
                    if (nextMode === "new") {
                      setLeaderIsTemporary(true);
                      setLeaderKind("");
                      setLeaderId("");
                    } else {
                      setLeaderIsTemporary(false);
                      setLeaderKind("");
                      setLeaderId("");
                    }
                  }}
                >
                  <option value="existing">{t("flowEditor.taskFields.ownerSourceExisting")}</option>
                  <option value="new">{t("flowEditor.taskFields.ownerSourceNew")}</option>
                </select>
              </div>
              <div>
                <label className="label">{t("flowEditor.leaderKindLabel")}</label>
                <select
                  className="select"
                  value={leaderKind || ""}
                  onFocus={() => {
                    void refreshOwnerKindsFast({ silent: true });
                  }}
                  onMouseDown={() => {
                    void refreshOwnerKindsFast({ silent: true });
                  }}
                  onChange={(e) => {
                    const nextKind = e.target.value as OwnerKindDraft;
                    const sourceMode: OwnerMode = leaderIsTemporary ? "new" : "existing";
                    const nextLeaderId = ownerIdAfterPlatformChange({
                      sourceMode,
                      previousKind: leaderKind,
                      nextKind,
                      ownerId: leaderId,
                      openclawOptions,
                      hermesOptions,
                    });
                    setLeaderKind(nextKind);
                    setLeaderId(nextLeaderId);
                  }}
                >
                  <option value="">{t("flowEditor.taskFields.ownerKindPlaceholder")}</option>
                  {(leaderIsTemporary
                    ? mergedOwnerKinds.temporaryKinds
                    : mergedOwnerKinds.persistentKinds).map((k) => (
                    <option key={k} value={k}>
                      {ownerKindLabel(k, (key) => t(key))}
                    </option>
                  ))}
                </select>
              </div>
              <div>
                <label className="label">{t("flowEditor.leaderAgentLabel")}</label>
                {!isOwnerKind(leaderKind) ? (
                  <input
                    className="input"
                    value={leaderId}
                    placeholder={t("flowEditor.taskFields.pickOwnerKindFirst")}
                    onChange={(e) => tryChangeLeader(e.target.value)}
                    disabled
                  />
                ) : !leaderIsTemporary && leaderKind === "openclaw" ? (
                  <select
                    className="select"
                    value={leaderId}
                    onChange={(e) => tryChangeLeader(e.target.value)}
                  >
                    <option value="">{t("flowEditor.leaderPlaceholder")}</option>
                    {leaderId && !openclawOptions.some((a) => a.id === leaderId) && (
                      <option value={leaderId}>{leaderId}</option>
                    )}
                    {openclawOptions.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name} ({a.id})
                      </option>
                    ))}
                  </select>
                ) : !leaderIsTemporary
                  && isOwnerKind(leaderKind)
                  && isManagedPickKind(leaderKind) ? (
                  <>
                    <select
                      className="select"
                      value={leaderId}
                      onChange={(e) => tryChangeLeader(e.target.value)}
                    >
                      <option value="">{t("flowEditor.hermesAgentPlaceholder")}</option>
                      {leaderId &&
                        !pickAgentsForKind(leaderKind, hermesOptions).some(
                          (a) => a.id === leaderId,
                        ) && <option value={leaderId}>{leaderId}</option>}
                      {pickAgentsForKind(leaderKind, hermesOptions).map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name} ({a.id})
                        </option>
                      ))}
                    </select>
                    {pickAgentsForKind(leaderKind, hermesOptions).length === 0 && (
                      <div className="text-xs text-ink-500 mt-1">
                        {t("flowEditor.hermesAgentEmpty")}
                      </div>
                    )}
                  </>
                ) : (
                  <input
                    className="input"
                    value={leaderId}
                    placeholder={t("flowEditor.taskFields.leaderNewAgentPlaceholder")}
                    onChange={(e) => setLeaderId(e.target.value)}
                  />
                )}
              </div>
            </div>
            {!isOpenclawKind(leaderKind) && (
              <div className="mt-2 grid gap-2 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
                <div>
                  <label className="label">{t("flowEditor.leaderRepoLabel")}</label>
                  {deploymentMode === "server" ? (
                    <>
                      <select
                        className="select"
                        value={leaderRepo}
                        onChange={(e) => {
                          snapshotLeaderRepoBeforeEdit();
                          const next = e.target.value;
                          setLeaderRepo(next);
                          setLeaderBranchOptions([]);
                          setLeaderBranchEditable(false);
                          commitLeaderRepoCheck(next);
                        }}
                      >
                        <option value="">
                          {t("flowEditor.taskFields.claudeRepoServerPlaceholder")}
                        </option>
                        {leaderRepo &&
                          !workspaceDirOptions.includes(leaderRepo) && (
                          <option value={leaderRepo}>{leaderRepo}</option>
                        )}
                        {workspaceDirOptions.map((p) => (
                          <option key={p} value={p}>
                            {p}
                          </option>
                        ))}
                      </select>
                      <div className="text-xs text-ink-500 mt-1">
                        {workspaceDirOptions.length === 0
                          ? t("flowEditor.taskFields.claudeRepoServerEmpty")
                          : t("flowEditor.taskFields.claudeRepoServerHint")}
                      </div>
                    </>
                  ) : (
                    <div className="flex gap-2">
                      <input
                        className="input flex-1"
                        value={leaderRepo}
                        placeholder={t("flowEditor.taskFields.claudeRepoPlaceholder")}
                        onChange={(e) => {
                          setLeaderRepo(e.target.value);
                          setLeaderBranchOptions([]);
                          setLeaderBranchEditable(false);
                        }}
                        onFocus={snapshotLeaderRepoBeforeEdit}
                        onBlur={(e) => commitLeaderRepoCheck(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                            (e.target as HTMLInputElement).blur();
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="btn-outline whitespace-nowrap"
                        onClick={() => void onPickLeaderRepo()}
                        disabled={leaderPickingRepo}
                      >
                        {t("flowEditor.taskFields.pickDirButton")}
                      </button>
                    </div>
                  )}
                </div>
                <div>
                  <label className="label">{t("flowEditor.leaderTargetBranchLabel")}</label>
                  <select
                    className="select"
                    value={leaderTargetBranch}
                    disabled={!leaderBranchEditable || leaderBranchLoading || !leaderRepo.trim()}
                    onChange={(e) => setLeaderTargetBranch(e.target.value)}
                  >
                {!leaderTargetBranch && (
                  <option value="">
                    {t("flowEditor.taskFields.pickBranch")}
                  </option>
                )}
                {leaderTargetBranch &&
                  !leaderBranchOptions.includes(leaderTargetBranch) && (
                  <option value={leaderTargetBranch}>{leaderTargetBranch}</option>
                )}
                {leaderBranchOptions.map((name) => (
                      <option key={name} value={name}>
                        {name}
                      </option>
                    ))}
                  </select>
                  {leaderBranchEditable && (
                    <div className="text-xs text-ink-500 mt-1">
                      {t("flowEditor.taskBranchCheck.editableHint")}
                    </div>
                  )}
                </div>
              </div>
            )}
            <div className="text-xs text-ink-500 mt-1">
              {t("flowEditor.leaderHint")}
            </div>
            {decomposeDisabledReason && (
              <div className="text-xs text-rose-700 mt-1">
                {decomposeDisabledReason}
              </div>
            )}
          </div>
        </div>
      </Card>

      <Card>
        <CardTitle>{t("flowEditor.runInputRequirement")}</CardTitle>
        <div className="text-xs text-ink-500">
          {t("flowEditor.runInputRequirementHint")}
        </div>
        <div className="mt-2 flex gap-2">
          <button
            type="button"
            className="btn-outline whitespace-nowrap"
            onClick={() => addRunInputField(runInputFieldDraft)}
          >
            {t("flowEditor.runInputRequirementAdd")}
          </button>
          <input
            className="input flex-1"
            value={runInputFieldDraft}
            placeholder={t("flowEditor.runInputRequirementPlaceholder")}
            onChange={(e) => {
              setRunInputFieldDraft(e.target.value);
              if (runInputFieldError) setRunInputFieldError(null);
            }}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                addRunInputField(runInputFieldDraft);
              }
            }}
          />
        </div>
        {runInputFieldError && (
          <div className="mt-1 text-xs text-rose-700">{runInputFieldError}</div>
        )}
        <div className="mt-2 flex flex-wrap gap-2">
          {runInputFields.length === 0 ? (
            <span className="text-xs text-ink-400">
              {t("flowEditor.runInputRequirementEmpty")}
            </span>
          ) : (
            runInputFields.map((field) => (
              <button
                key={field}
                type="button"
                className="inline-flex items-center gap-1 rounded-full border border-blue-300 bg-blue-50 px-2 py-1 text-xs font-medium text-blue-800 hover:bg-blue-100"
                onClick={() => removeRunInputField(field)}
                title={t("flowEditor.runInputRequirementRemove")}
              >
                <span>{field}</span>
                <span aria-hidden>×</span>
              </button>
            ))
          )}
        </div>
      </Card>

      <Card>
        <CardTitle
          right={
            <div className="flex items-center gap-2">
              <button
                className="btn-outline"
                onClick={openMerge}
                disabled={!leaderId.trim()}
                title={!leaderId.trim() ? t("flowEditor.mergePickLeaderFirst") : undefined}
              >
                {t("flowEditor.mergeFlows")}
              </button>
              <button
                className="btn-primary"
                onClick={openNewTask}
                disabled={!leaderId.trim()}
                title={!leaderId.trim() ? t("flowEditor.pickLeaderFirst") : undefined}
              >
                {t("flowEditor.addTask")}
              </button>
            </div>
          }
        >
          {t("flowEditor.tasks")}
        </CardTitle>

        <div className="text-sm text-ink-600 mb-3">
          {t("flowEditor.tasksHint")}
        </div>

        {globalIssues.length > 0 && (
          <ErrorBox>
            <div className="space-y-1">
              {globalIssues.map((msg) => (
                <div key={msg}>{msg}</div>
              ))}
            </div>
          </ErrorBox>
        )}

        {orderedTasks.length === 0 ? (
          <div className="px-4 py-8 text-center text-sm text-ink-500 border border-dashed border-ink-200 rounded-md">
            {t("flowEditor.emptyTasks")}
          </div>
        ) : (
          <div className="flex flex-col gap-4 lg:flex-row lg:items-stretch lg:gap-4">
            <div className="min-w-0 w-full divide-y divide-ink-100 border border-ink-200 rounded-md lg:flex-[3]">
              {orderedTasks.map((row) => {
                const issue = issues.find((i) => i.rowKey === row.rowKey)?.message;
                const violatesLeader =
                  leaderKey !== null &&
                  ownerKey(row) === leaderKey &&
                  !row.isLeaderSummary;
                const summaryMissingDeps =
                  row.isLeaderSummary && row.dependsOn.length === 0;
                const nonSummaryIdx = orderedTasks
                  .filter((r) => !r.isLeaderSummary)
                  .findIndex((r) => r.rowKey === row.rowKey);
                const nonSummaryTotal = orderedTasks.filter(
                  (r) => !r.isLeaderSummary,
                ).length;
                return (
                  <TaskListRow
                    key={row.rowKey}
                    row={row}
                    allTasks={tasks}
                    issue={issue}
                    violatesLeader={violatesLeader}
                    summaryMissingDeps={summaryMissingDeps}
                    canMoveUp={!row.isLeaderSummary && nonSummaryIdx > 0}
                    canMoveDown={
                      !row.isLeaderSummary && nonSummaryIdx < nonSummaryTotal - 1
                    }
                    onDetail={() =>
                      setEditing({ mode: "view", rowKey: row.rowKey })
                    }
                    onEdit={() =>
                      setEditing({ mode: "edit", rowKey: row.rowKey })
                    }
                    onRemove={async () => {
                      const ok = await confirm(
                        t("flowEditor.removeTaskConfirm", {
                          name: row.subject || row.id || row.rowKey,
                        }),
                        { danger: true, okText: t("flowEditor.delete") },
                      );
                      if (!ok) return;
                      removeRow(row.rowKey);
                    }}
                    onToggleCheckpoint={() =>
                      setTasks((prev) =>
                        prev.map((item) =>
                          item.rowKey === row.rowKey && !item.isLeaderSummary
                            ? {
                              ...item,
                              requiresHumanCheckpoint: !item.requiresHumanCheckpoint,
                            }
                            : item,
                        ),
                      )}
                    devMode={devMode}
                    onSetAutoMerge={(enabled) => {
                      if (row.ownerKind === "openclaw") return;
                      const targetOwner = ownerKey(row);
                      let syncedOthers = 0;
                      let changedAny = false;
                      const nextTasks = tasks.map((item) => {
                        if (row.isLeaderSummary) {
                          if (item.rowKey !== row.rowKey) return item;
                          if (item.autoMerge === enabled) return item;
                          changedAny = true;
                          return { ...item, autoMerge: enabled };
                        }
                        if (item.isLeaderSummary || ownerKey(item) !== targetOwner) {
                          return item;
                        }
                        if (item.autoMerge === enabled) {
                          return item;
                        }
                        changedAny = true;
                        if (item.rowKey !== row.rowKey) syncedOthers += 1;
                        return { ...item, autoMerge: enabled };
                      });
                      if (changedAny) setTasks(nextTasks);
                      if (syncedOthers > 0) showAutoMergeSyncNotice();
                    }}
                    onMove={(dir) => moveRow(row.rowKey, dir)}
                  />
                );
              })}
            </div>
            <DependencyGraph tasks={tasks} />
          </div>
        )}
      </Card>

      {autoMergeSyncNoticeOpen && (
        <div className="pointer-events-none fixed inset-0 z-40 flex items-center justify-center">
          <div className="rounded-full border border-emerald-200 bg-emerald-50 px-3 py-1 text-xs font-medium text-emerald-700 shadow-card">
            {t("flowEditor.taskFields.autoMergeSyncedNotice")}
          </div>
        </div>
      )}

      {mergeOpen && (
        <MergeFlowsModal
          currentFlowId={isNew ? null : id!}
          leaderId={leaderId.trim()}
          onCancel={() => setMergeOpen(false)}
          onMerge={applyMerge}
        />
      )}

      {repoIssue && (
        <Modal
          open={true}
          onClose={reselectRepoPath}
          title={t("flowEditor.repoIssue.title")}
          dismissible={!fixingRepo}
          width="max-w-2xl"
        >
          <div className="space-y-3">
            <div className="text-sm text-ink-700">
              {t("flowEditor.repoIssue.description", {
                agentId: repoIssue.agentId,
              })}
            </div>
            <div className="rounded-md border border-ink-200 bg-ink-50 px-3 py-2">
              <div className="text-xs text-ink-500">
                {t("flowEditor.repoIssue.repoPathLabel")}
              </div>
              <div className="text-sm font-mono break-all text-ink-900">
                {repoIssue.repo}
              </div>
            </div>
            <div className="text-sm text-rose-700">
              {repoIssueReasonText(repoIssue)}
            </div>
            <div className="flex justify-end gap-2 pt-2">
              <button
                type="button"
                className="btn-outline"
                onClick={reselectRepoPath}
                disabled={fixingRepo}
              >
                {t("flowEditor.repoIssue.reselectAction")}
              </button>
              <button
                type="button"
                className="btn-primary"
                onClick={createRepoAndRetrySave}
                disabled={fixingRepo}
              >
                {fixingRepo
                  ? t("flowEditor.repoIssue.creatingAction")
                  : t("flowEditor.repoIssue.createAction")}
              </button>
            </div>
          </div>
        </Modal>
      )}

      {editing && editingRow && (
        <TaskEditModal
          mode={editing.mode}
          initialRow={editingRow}
          tasks={tasks}
          devMode={devMode}
          openclawOptions={openclawOptions}
          hermesOptions={hermesOptions}
          persistentOwnerKinds={mergedOwnerKinds.persistentKinds}
          temporaryOwnerKinds={mergedOwnerKinds.temporaryKinds}
          openclawIds={openclawIds}
          hermesIds={hermesIds}
          validationMessages={validationMessages}
          leaderKind={leaderKind}
          leaderId={leaderId.trim()}
          leaderRepo={leaderRepo.trim()}
          leaderTargetBranch={leaderTargetBranch.trim()}
          deploymentMode={deploymentMode}
          workspaceDirOptions={workspaceDirOptions}
          onRefreshOwnerKinds={() => refreshOwnerKindsFast({ silent: true })}
          onSave={(row) => {
            if (editing.mode === "create") commitNewTask(row);
            else if (editing.mode === "edit")
              applyEditedRow(editing.rowKey, row);
            setEditing(null);
          }}
          onCancel={() => setEditing(null)}
        />
      )}

      <DecomposeModal
        open={decomposeOpen}
        goal={description}
        leaderKind={leaderKind}
        leaderId={leaderId.trim()}
        leaderRepo={leaderRepo.trim()}
        leaderTargetBranch={leaderTargetBranch.trim()}
        existingRows={tasks}
        openclawAgents={openclawOptions}
        onClose={() => setDecomposeOpen(false)}
        onApply={(proposal) => {
          const rows = proposalToRows(proposal, openclawOptions, hermesOptions);
          const idx = rows.findIndex((r) => r.isLeaderSummary);
          if (idx >= 0 && leaderId.trim()) {
            const normalizedLeaderRepo = needsRepoBranchFields(leaderKind)
              ? leaderRepo.trim()
              : "";
            const normalizedLeaderTargetBranch = needsRepoBranchFields(leaderKind)
              ? leaderTargetBranch.trim()
              : "";
            rows[idx] = {
              ...rows[idx],
              ownerKind: leaderKind,
              ownerId: leaderId.trim(),
              ownerRepo: normalizedLeaderRepo,
              ownerTargetBranch: normalizedLeaderTargetBranch,
            };
          }
          setTasks(rows);
          setDecomposeOpen(false);
        }}
      />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Compact list row
// ──────────────────────────────────────────────────────────────────────


function TaskListRow({
  row,
  allTasks,
  issue,
  violatesLeader,
  summaryMissingDeps,
  canMoveUp,
  canMoveDown,
  onDetail,
  onEdit,
  onRemove,
  onToggleCheckpoint,
  devMode,
  onSetAutoMerge,
  onMove,
}: {
  row: TaskRow;
  allTasks: TaskRow[];
  issue?: string;
  violatesLeader: boolean;
  summaryMissingDeps: boolean;
  canMoveUp: boolean;
  canMoveDown: boolean;
  onDetail: () => void;
  onEdit: () => void;
  onRemove: () => void;
  onToggleCheckpoint: () => void;
  devMode: boolean;
  onSetAutoMerge: (enabled: boolean) => void;
  onMove: (dir: -1 | 1) => void;
}) {
  const { t } = useTranslation();
  const isSummary = row.isLeaderSummary;
  const hasIssue = !!issue || violatesLeader || summaryMissingDeps;
  // Auto-generated task ids look like `task-abc12345` and mean nothing to
  // the user — render the dependent task's subject instead.
  const subjectForId = (id: string) => {
    const dep = allTasks.find((r) => r.id === id);
    return dep?.subject.trim() || dep?.id || id;
  };
  return (
    <div
      className={cn(
        "px-4 py-3",
        hasIssue && "bg-rose-50",
      )}
    >
      <div className="flex items-center gap-2">
        <div className="min-w-0 flex-1 overflow-x-auto overscroll-x-contain touch-pan-x">
          <div className="w-max pr-1 space-y-1">
            <div className="flex items-center gap-2 flex-nowrap">
              <span className="text-sm font-medium text-ink-900 whitespace-nowrap">
                {row.subject.trim() || t("flowEditor.rowUntitled")}
              </span>
              {isSummary && (
                <span className="pill-brand shrink-0 whitespace-nowrap">
                  ⭐ {t("flowEditor.summaryTaskBadge")}
                </span>
              )}
              {!isSummary && row.requiresHumanCheckpoint && (
                <span className="pill-warning shrink-0 whitespace-nowrap">
                  ⛳ {t("flowEditor.taskFields.requiresHumanCheckpointEnabledShort")}
                </span>
              )}
              {devMode && (() => {
                const ownerIsOpenclaw = row.ownerKind === "openclaw";
                // OpenClaw is forced to auto-merge regardless of stored value.
                const autoMergeOn = ownerIsOpenclaw || row.autoMerge;
                return (
                  <span className="inline-flex shrink-0 items-center gap-2">
                    <button
                      type="button"
                      role="switch"
                      aria-checked={autoMergeOn}
                      aria-label={t("flowEditor.taskFields.autoMergeEnabledShort")}
                      disabled={ownerIsOpenclaw}
                      title={
                        ownerIsOpenclaw
                          ? t("flowEditor.taskFields.autoMergeOpenclawLocked")
                          : undefined
                      }
                      className={cn(
                        "relative inline-flex h-5 w-9 items-center rounded-full transition-colors",
                        autoMergeOn ? "bg-purple-500" : "bg-ink-300",
                        ownerIsOpenclaw && "cursor-not-allowed opacity-80",
                      )}
                      onClick={
                        ownerIsOpenclaw
                          ? undefined
                          : () => onSetAutoMerge(!row.autoMerge)
                      }
                    >
                      <span
                        className={cn(
                          "inline-block h-4 w-4 transform rounded-full bg-surface shadow transition-transform",
                          autoMergeOn ? "translate-x-4" : "translate-x-0.5",
                        )}
                      />
                    </button>
                    <span
                      className={cn(
                        "text-xs font-semibold whitespace-nowrap",
                        autoMergeOn ? "text-purple-700" : "text-ink-500",
                      )}
                    >
                      {autoMergeOn
                        ? t("flowEditor.taskFields.autoMergeEnabledShort")
                        : t("flowEditor.taskFields.autoMergeDisabledShort")}
                    </span>
                  </span>
                );
              })()}
              {!isSummary && (
                <button
                  type="button"
                  className={cn(
                    "shrink-0 rounded-md border px-2.5 py-1 text-xs font-semibold transition",
                    row.requiresHumanCheckpoint
                      ? "border-amber-500 bg-amber-500 text-white shadow-[0_10px_18px_-10px_rgba(217,119,6,0.85)] hover:bg-amber-600"
                      : "border-amber-300 bg-amber-50 text-amber-700 hover:bg-amber-100",
                  )}
                  onClick={onToggleCheckpoint}
                >
                  {row.requiresHumanCheckpoint
                    ? t("flowEditor.taskFields.requiresHumanCheckpointDisableAction")
                    : t("flowEditor.taskFields.requiresHumanCheckpointEnableAction")}
                </button>
              )}
              <button
                type="button"
                className="btn-outline shrink-0"
                onClick={onDetail}
              >
                {t("flowEditor.details")}
              </button>
              <button
                type="button"
                className="btn-outline shrink-0"
                onClick={onEdit}
              >
                {t("flowEditor.edit")}
              </button>
              <button
                type="button"
                className="btn-danger shrink-0"
                onClick={onRemove}
                disabled={isSummary}
                title={isSummary ? t("flowEditor.summaryTaskLocked") : undefined}
              >
                {t("flowEditor.delete")}
              </button>
            </div>
            <div className="text-xs text-ink-500 whitespace-nowrap">
              <span>
                {t("flowEditor.rowOwner")}:{" "}
                <span className="font-mono">{row.ownerId || "—"}</span>{" "}
                <span className="pill-default align-middle">
                  {ownerKindLabel(row.ownerKind, t)}
                </span>
              </span>
              <span className="mx-2">·</span>
              <span>
                {t("flowEditor.rowDependsOn")}:{" "}
                {row.dependsOn.length === 0 ? (
                  <span className="text-ink-400">
                    {t("flowEditor.rowNoDeps")}
                  </span>
                ) : (
                  <span>{row.dependsOn.map(subjectForId).join("、")}</span>
                )}
              </span>
            </div>
            {isSummary && (
              <div className="text-xs text-ink-500 whitespace-nowrap">
                {t("flowEditor.summaryTaskLocked")}
              </div>
            )}
            {summaryMissingDeps && (
              <div className="text-xs text-rose-700 whitespace-nowrap">
                {t("flowEditor.summaryNoDepsWarning")}
              </div>
            )}
            {(issue || violatesLeader) && (
              <div className="text-xs text-rose-700 whitespace-nowrap">
                {violatesLeader ? t("flowEditor.leaderHardConstraint") : issue}
              </div>
            )}
            {!isSummary && row.requiresHumanCheckpoint && (
              <div className="text-xs text-brand-700 whitespace-nowrap">
                {t("flowEditor.taskFields.requiresHumanCheckpointEnabled")}
              </div>
            )}
          </div>
        </div>
        <div className="shrink-0 flex flex-row items-center gap-0.5 border-l border-ink-200 pl-2">
          <button
            type="button"
            className="btn-ghost px-2"
            onClick={() => onMove(-1)}
            disabled={!canMoveUp}
            title="↑"
          >
            ↑
          </button>
          <button
            type="button"
            className="btn-ghost px-2"
            onClick={() => onMove(1)}
            disabled={!canMoveDown}
            title="↓"
          >
            ↓
          </button>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Task edit modal — wraps the full task form
// ──────────────────────────────────────────────────────────────────────


/**
 * Task editor modal. Holds a local draft so users can cancel without
 * dirtying parent state. Save validates required fields client-side and
 * surfaces the first violation inline. ``view`` mode disables every input.
 */
function TaskEditModal({
  mode,
  initialRow,
  tasks,
  devMode,
  openclawOptions,
  hermesOptions,
  persistentOwnerKinds,
  temporaryOwnerKinds,
  openclawIds,
  hermesIds,
  validationMessages,
  leaderKind,
  leaderId,
  leaderRepo,
  leaderTargetBranch,
  deploymentMode,
  workspaceDirOptions,
  onRefreshOwnerKinds,
  onSave,
  onCancel,
}: {
  mode: "create" | "edit" | "view";
  initialRow: TaskRow;
  tasks: TaskRow[];
  devMode: boolean;
  openclawOptions: OpenclawAgentSummary[];
  hermesOptions: HermesAgentSummary[];
  persistentOwnerKinds: OwnerKind[];
  temporaryOwnerKinds: NonOpenclawOwnerKind[];
  openclawIds: Set<string>;
  hermesIds: Set<string>;
  validationMessages: ValidationMessages;
  leaderKind: OwnerKindDraft;
  /** Currently-selected leader id. Excluded from sub-task agent picker
   *  (leader can only own the auto-summary task). */
  leaderId: string;
  leaderRepo: string;
  leaderTargetBranch: string;
  deploymentMode: DeploymentMode;
  workspaceDirOptions: string[];
  onRefreshOwnerKinds: () => Promise<OwnerKindsAvailability>;
  onSave: (row: TaskRow) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const { confirm, alert } = useDialog();
  const [draft, setDraft] = useState<TaskRow>(initialRow);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [repoChecking, setRepoChecking] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([]);
  const [branchEditable, setBranchEditable] = useState(
    needsRepoBranchFields(initialRow.ownerKind),
  );
  const [branchLoading, setBranchLoading] = useState(false);
  /** Repo path last committed for validation (blur / pick / select), not every keystroke. */
  const [repoToCheck, setRepoToCheck] = useState(() =>
    isOpenclawKind(initialRow.ownerKind) ? "" : initialRow.ownerRepo.trim(),
  );
  /** Repo + branch before the current edit; restored on check failure. */
  const repoBeforeEditRef = useRef({ repo: "", branch: "" });
  const readOnly = mode === "view";
  const isSummary = draft.isLeaderSummary;
  const [ownerMode, setOwnerMode] = useState<OwnerMode>(() =>
    initialRow.ownerKind !== "openclaw" && initialRow.ownerIsTemporary
      ? "new"
      : "existing",
  );
  const knownAgentIds = useMemo(
    () => collectKnownAgentIds(tasks, initialRow.rowKey),
    [tasks, initialRow.rowKey],
  );

  const title =
    mode === "create"
      ? t("flowEditor.newTaskModalTitle")
      : mode === "view"
      ? t("flowEditor.detailTaskModalTitle")
      : t("flowEditor.editTaskModalTitle");

  function patch(p: Partial<TaskRow>) {
    setSaveError(null);
    if (Object.prototype.hasOwnProperty.call(p, "ownerRepo")) {
      setBranchOptions([]);
      setBranchEditable(false);
    }
    setDraft((d) => ({ ...d, ...p }));
  }

  function snapshotRepoBeforeEdit(row: Pick<TaskRow, "ownerRepo" | "ownerTargetBranch">) {
    repoBeforeEditRef.current = {
      repo: row.ownerRepo,
      branch: row.ownerTargetBranch,
    };
  }

  function revertRepoAfterEdit() {
    const snap = repoBeforeEditRef.current;
    patch({ ownerRepo: snap.repo, ownerTargetBranch: snap.branch });
    setRepoToCheck(snap.repo.trim());
  }

  function commitRepoCheck(repo: string) {
    setRepoToCheck(repo.trim());
  }

  useEffect(() => {
    if (isOpenclawKind(draft.ownerKind)) {
      setBranchOptions([]);
      setBranchEditable(false);
      return;
    }
    const repo = repoToCheck.trim();
    if (!repo) {
      setBranchOptions([]);
      setBranchEditable(false);
      setDraft((prev) =>
        !prev.ownerRepo.trim() && prev.ownerTargetBranch
          ? { ...prev, ownerTargetBranch: "" }
          : prev,
      );
      return;
    }
    let cancelled = false;
    setBranchLoading(true);
    const branchBeforeCheck = draft.ownerTargetBranch;
    void (async () => {
      const out = await ensureRepoAndListBranches({
        repo,
        preserveBranch: branchBeforeCheck,
        agentLabel: draft.ownerId.trim() || t("flowEditor.repoIssue.unknownAgent"),
        confirmCreate: confirm,
        messages: repoBranchMessages(t),
      });
      if (cancelled) return;
      if (!out.ok) {
        setBranchOptions([]);
        setBranchEditable(false);
        setBranchLoading(false);
        if (out.cancelled || out.invalidPath) {
          revertRepoAfterEdit();
          if (out.invalidPath) {
            void alert(out.error);
          }
        } else if (out.error) {
          setSaveError(out.error);
        }
        return;
      }
      setBranchOptions(out.result.branches);
      setBranchEditable(out.result.editable);
      setDraft((prev) => {
        const nextRepo = out.result.path || repo;
        const keepBranch = branchAfterRepoCheck(
          branchBeforeCheck,
          out.result.branches,
        );
        if (prev.ownerRepo === nextRepo && prev.ownerTargetBranch === keepBranch) {
          return prev;
        }
        return { ...prev, ownerRepo: nextRepo, ownerTargetBranch: keepBranch };
      });
      setBranchLoading(false);
    })();
    return () => {
      cancelled = true;
    };
    // P2: the repo/branch check keys off the repo + kind only. Editing the agent
    // NAME (draft.ownerId, used solely as a confirm-dialog label) must NOT
    // re-run this check — the three owner blocks are independent.
  }, [alert, confirm, draft.ownerKind, repoToCheck, t]);

  function hasOtherSubtaskWithSameAgentDifferentBinding(row: TaskRow): boolean {
    const ownerId = row.ownerId.trim();
    if (!ownerId) return false;
    return tasks.some(
      (task) =>
        task.rowKey !== initialRow.rowKey &&
        !task.isLeaderSummary &&
        task.ownerId.trim() === ownerId &&
        !sameOwnerBinding(task, row),
    );
  }

  async function ensureNonOpenclawRepoReady(
    row: TaskRow,
  ): Promise<TaskRow | null> {
    if (isOpenclawKind(row.ownerKind)) return row;
    const repo = row.ownerRepo.trim();
    if (!repo) return row;
    const branch = row.ownerTargetBranch.trim();
    if (!branch) {
      setSaveError(t("flowEditor.taskFieldRequired"));
      return null;
    }
    const out = await ensureRepoAndListBranches({
      repo,
      preserveBranch: branch,
      agentLabel: row.ownerId.trim() || t("flowEditor.repoIssue.unknownAgent"),
      confirmCreate: confirm,
      messages: repoBranchMessages(t),
    });
    if (!out.ok) {
      if (out.invalidPath) {
        void alert(out.error);
      } else if (out.error) {
        setSaveError(out.error);
      }
      return null;
    }
    if (!out.result.branches.includes(branch)) {
      setSaveError(t("flowEditor.taskBranchCheck.notFound"));
      return null;
    }
    return {
      ...row,
      ownerRepo: out.result.path || repo,
      ownerTargetBranch: branch,
    };
  }

  /** The full task list AS IT WILL BE after this draft is committed — mirrors
   *  commitNewTask / applyEditedRow (binding-normalize + cross-task sync), minus
   *  the task-id rename remap (the modal never edits the hidden id). Used to run
   *  the global validator against the prospective save state. */
  function buildProspectiveRows(saved: TaskRow): TaskRow[] {
    const normalized = applyOwnerBinding(saved, normalizedOwnerBinding(saved));
    const ownerId = normalized.ownerId.trim();
    if (mode === "create") {
      const base = ownerId
        ? syncOwnerBindingAcrossSubtasks(tasks, ownerId, normalized).rows
        : tasks;
      const summaryIdx = base.findIndex((r) => r.isLeaderSummary);
      return summaryIdx === -1
        ? [...base, normalized]
        : [...base.slice(0, summaryIdx), normalized, ...base.slice(summaryIdx)];
    }
    let rows = tasks.map((r) =>
      r.rowKey === initialRow.rowKey ? { ...normalized, rowKey: initialRow.rowKey } : r,
    );
    if (ownerId) {
      rows = syncOwnerBindingAcrossSubtasks(rows, ownerId, normalized, initialRow.rowKey).rows;
    }
    return rows;
  }

  async function attemptSave() {
    if (repoChecking) return;
    const prospective = buildProspectiveRows(draft);
    const detectedNow = await onRefreshOwnerKinds();
    const ownerKindsNow = mergeOwnerKindAvailability(
      detectedNow,
      usedOwnerKinds(prospective, leaderKind),
    );
    if (!isOwnerKind(draft.ownerKind)) {
      setSaveError(validationMessages.ownerKindRequired);
      return;
    }
    if (!ownerKindAvailableForSource(draft.ownerKind, draft.ownerIsTemporary, ownerKindsNow)) {
      setSaveError(
        validationMessages.ownerKindUnavailable({
          subject: draft.subject.trim() || draft.id || t("flowEditor.rowUntitled"),
          kindLabel: ownerKindLabel(draft.ownerKind, (key) => t(key)),
        }),
      );
      return;
    }
    // P6 — comprehensive check at "保存子任务". Runs cheap field/cross-task
    // validation first (the SAME validator the Flow save uses, scoped to this
    // row), then the cross-task unity confirm, then async repo/branch existence.
    const cycle = detectTaskCycle(prospective);
    if (cycle.length > 0) {
      setSaveError(validationMessages.cycleDetected(cycle.join(" -> ")));
      return;
    }
    const rowIssue = validate(prospective, validationMessages, openclawIds, hermesIds).find(
      (i) => i.rowKey === draft.rowKey,
    );
    if (rowIssue) {
      setSaveError(rowIssue.message);
      return;
    }
    // A brand-new temporary name may not collide with the leader's name (the
    // leader owns only the summary; a worker can never reuse it).
    if (ownerMode === "new") {
      const candidate = draft.ownerId.trim();
      if (candidate && knownAgentIds.has(candidate)) {
        const usedByOtherSubtask = tasks.some(
          (task) =>
            task.rowKey !== initialRow.rowKey &&
            task.ownerId.trim() === candidate &&
            !task.isLeaderSummary,
        );
        if (!usedByOtherSubtask) {
          setSaveError(
            t("flowEditor.validation.newAgentNameDuplicated", { agentId: candidate }),
          );
          return;
        }
      }
    }
    // P4 — cross-task binding unity: confirm once, then onSave syncs the binding
    // to every same-name subtask.
    if (hasOtherSubtaskWithSameAgentDifferentBinding(draft)) {
      const ok = await confirm(
        t("flowEditor.taskRepoCheck.confirmExistingAgentRepoBranchChange", {
          agentId: draft.ownerId.trim(),
        }),
      );
      if (!ok) return;
    }
    setRepoChecking(true);
    try {
      const next = await ensureNonOpenclawRepoReady(draft);
      if (!next) return;
      onSave(enforceOpenclawAutoMerge(next));
    } finally {
      setRepoChecking(false);
    }
  }

  return (
    <Modal open={true} onClose={onCancel} title={title} width="max-w-3xl">
      <TaskFormBody
        row={draft}
        readOnly={readOnly}
        isSummary={isSummary}
        devMode={devMode}
        showCheckpointField={mode !== "edit"}
        tasks={tasks}
        ownerMode={ownerMode}
        openclawOptions={openclawOptions}
        hermesOptions={hermesOptions}
        persistentOwnerKinds={persistentOwnerKinds}
        temporaryOwnerKinds={temporaryOwnerKinds}
        deploymentMode={deploymentMode}
        workspaceDirOptions={workspaceDirOptions}
        branchOptions={branchOptions}
        branchEditable={branchEditable}
        branchLoading={branchLoading}
        onRepoPathCommit={commitRepoCheck}
        onRepoPathEditStart={() => snapshotRepoBeforeEdit(draft)}
        onOwnerKindMenuOpen={() => {
          void onRefreshOwnerKinds();
        }}
        onOwnerModeChange={(nextMode) => {
          setOwnerMode(nextMode);
          // Switching source clears kind+id so user re-picks explicitly.
          if (nextMode === "new") {
            patch({ ownerKind: "", ownerId: "", ownerIsTemporary: true });
          } else {
            patch({ ownerKind: "", ownerId: "", ownerIsTemporary: false });
          }
        }}
        onChange={patch}
      />
      {saveError && (
        <div className="text-sm text-rose-700 mt-3">{saveError}</div>
      )}
      <div className="flex justify-end gap-2 mt-4">
        <button type="button" className="btn-outline" onClick={onCancel}>
          {readOnly ? t("common.close") : t("flowEditor.modalCancel")}
        </button>
        {!readOnly && (
          <button
            type="button"
            className="btn-primary"
            onClick={() => void attemptSave()}
            disabled={repoChecking}
          >
            {t("flowEditor.modalSave")}
          </button>
        )}
      </div>
    </Modal>
  );
}


function TaskFormBody({
  row,
  readOnly,
  isSummary,
  devMode,
  showCheckpointField,
  tasks,
  ownerMode,
  openclawOptions,
  hermesOptions,
  persistentOwnerKinds,
  temporaryOwnerKinds,
  deploymentMode,
  workspaceDirOptions,
  branchOptions,
  branchEditable,
  branchLoading,
  onRepoPathCommit,
  onRepoPathEditStart,
  onOwnerKindMenuOpen,
  onOwnerModeChange,
  onChange,
}: {
  row: TaskRow;
  readOnly: boolean;
  isSummary: boolean;
  devMode: boolean;
  showCheckpointField: boolean;
  tasks: TaskRow[];
  ownerMode: OwnerMode;
  openclawOptions: OpenclawAgentSummary[];
  hermesOptions: HermesAgentSummary[];
  persistentOwnerKinds: OwnerKind[];
  temporaryOwnerKinds: NonOpenclawOwnerKind[];
  deploymentMode: DeploymentMode;
  workspaceDirOptions: string[];
  branchOptions: string[];
  branchEditable: boolean;
  branchLoading: boolean;
  onRepoPathCommit: (repo: string) => void;
  onRepoPathEditStart: () => void;
  onOwnerKindMenuOpen: () => void;
  onOwnerModeChange: (mode: OwnerMode) => void;
  onChange: (patch: Partial<TaskRow>) => void;
}) {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const [pickingRepo, setPickingRepo] = useState(false);
  const ownerLocked = readOnly || isSummary;
  const ownerKindSelected = isOwnerKind(row.ownerKind);
  const ownerIsOpenclaw = isOpenclawKind(row.ownerKind);
  const ownerShowsRepoFields = !ownerIsOpenclaw;
  const ownerIsNew = ownerMode === "new";
  const ownerKindEditable = !ownerLocked;
  const branchHelperText = branchEditable
    ? t("flowEditor.taskBranchCheck.editableHint")
    : "";

  function patchOwnerRepo(repo: string) {
    onChange({ ownerRepo: repo });
  }

  function commitRepoPath(repo: string) {
    onRepoPathCommit(repo.trim());
  }

  // Dependable list = every other non-summary task. Keeping summary tasks
  // out avoids summary↔summary/self dependency cycles.
  const dependableTasks = tasks
    .filter((r) => r.rowKey !== row.rowKey && !r.isLeaderSummary && r.id.trim())
    .map((r) => r.id);

  async function onPickRepo() {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    onRepoPathEditStart();
    setPickingRepo(true);
    try {
      const out = await api.pickDirectory({
        title: t("flowEditor.taskFields.pickDirTitle"),
        initialPath: row.ownerRepo || undefined,
      });
      if (out.path) {
        patchOwnerRepo(out.path);
        commitRepoPath(out.path);
      }
    } catch (e) {
      // Backend without a GUI display (the typical headless server case)
      // returns DIRECTORY_PICKER_UNAVAILABLE with a human-readable detail.
      // Show that detail in a popup instead of leaking the raw code into
      // the form's error rail.
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof Error
          ? e.message
          : String(e);
      void alert(t("flowEditor.pickDirFailed", { message: msg }));
    } finally {
      setPickingRepo(false);
    }
  }

  return (
    <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
      {isSummary && (
        <div className="md:col-span-2 text-xs text-ink-500 bg-ink-50 rounded-md p-2">
          ⭐ {t("flowEditor.summaryTaskLocked")}
        </div>
      )}
      <div className="md:col-span-2">
        <label className="label">
          {t("flowEditor.taskFields.subject")} *
        </label>
        <input
          className="input"
          placeholder={t("flowEditor.taskFields.subjectPlaceholder")}
          value={row.subject}
          readOnly={readOnly}
          onChange={(e) => onChange({ subject: e.target.value })}
        />
      </div>
      <div className="md:col-span-2">
        <label className="label">{t("flowEditor.taskFields.description")}</label>
        {!isSummary && devMode && (
          <div className="text-xs text-amber-800 bg-amber-50 border border-amber-200 rounded-md px-2.5 py-2 mb-2">
            {t("flowEditor.taskFields.descriptionCollabHint")}
          </div>
        )}
        <textarea
          className="textarea h-24"
          placeholder={t("flowEditor.taskFields.descriptionPlaceholder")}
          value={row.description}
          readOnly={readOnly}
          onChange={(e) => onChange({ description: e.target.value })}
        />
        {isSummary && (
          <div className="text-xs text-ink-500 mt-1">
            {t("flowEditor.taskFields.summaryDescriptionHint")}
          </div>
        )}
      </div>
      <div className="md:col-span-2">
        <label className="label">
          {t("flowEditor.taskFields.outputSummary")}{" "}
          <span className="text-ink-500 text-xs">
            {isSummary
              ? t("flowEditor.taskFields.outputSummaryHintSummary")
              : t("flowEditor.taskFields.outputSummaryHint")}
          </span>
        </label>
        <textarea
          className="textarea h-20"
          placeholder={
            isSummary
              ? t("flowEditor.taskFields.outputSummaryPlaceholderSummary")
              : t("flowEditor.taskFields.outputSummaryPlaceholder")
          }
          value={row.outputSummaryRequirement}
          readOnly={readOnly}
          onChange={(e) => onChange({ outputSummaryRequirement: e.target.value })}
        />
      </div>

      {/* Owner picker */}
      <div>
        <label className="label">{t("flowEditor.taskFields.ownerSource")}</label>
        <select
          className="select"
          value={ownerMode}
          disabled={ownerLocked}
          onChange={(e) => onOwnerModeChange(e.target.value as OwnerMode)}
        >
          <option value="existing">{t("flowEditor.taskFields.ownerSourceExisting")}</option>
          <option value="new">{t("flowEditor.taskFields.ownerSourceNew")}</option>
        </select>
      </div>

      {/* Owner type (kind). Selectable in both sources: existing → a filter
          over registered agents; new → the kind of the temporary agent. */}
      <div>
        <label className="label">{t("flowEditor.taskFields.ownerKind")}</label>
        <select
          className="select"
          value={row.ownerKind || ""}
          disabled={!ownerKindEditable}
          onFocus={onOwnerKindMenuOpen}
          onMouseDown={onOwnerKindMenuOpen}
          onChange={(e) => {
            const nextKind = e.target.value as OwnerKindDraft;
            const nextOwnerId = ownerIdAfterPlatformChange({
              sourceMode: ownerMode,
              previousKind: row.ownerKind,
              nextKind,
              ownerId: row.ownerId,
              openclawOptions,
              hermesOptions,
            });
            onChange({
              ownerKind: nextKind,
              ownerId: nextOwnerId,
              autoMerge: nextKind === "openclaw" ? true : row.autoMerge,
            });
          }}
        >
          <option value="">{t("flowEditor.taskFields.ownerKindPlaceholder")}</option>
          {ownerKindSelected
            && !(
              ownerIsNew
                ? temporaryOwnerKinds.includes(row.ownerKind as NonOpenclawOwnerKind)
                : persistentOwnerKinds.includes(row.ownerKind as OwnerKind)
            ) && (
            <option value={row.ownerKind}>{ownerKindLabel(row.ownerKind, (key) => t(key))}</option>
          )}
          {(ownerIsNew ? temporaryOwnerKinds : persistentOwnerKinds).map((k) => (
            <option key={k} value={k}>
              {ownerKindLabel(k, (key) => t(key))}
            </option>
          ))}
        </select>
      </div>

      {/* Agent identity. Persistent → pick a registered agent of the chosen
          kind (OpenClaw / Hermes managed dropdown). Temporary → free-type a new
          agent name AND/OR pick a temporary agent already created in THIS flow
          (both inputs open at once). */}
      <div>
        <label className="label">
          {ownerIsNew
            ? t("flowEditor.taskFields.newAgentName")
            : t("flowEditor.taskFields.existingAgent")}
        </label>
        {!ownerKindSelected ? (
          <input
            className="input"
            value={row.ownerId}
            disabled
            placeholder={t("flowEditor.taskFields.pickOwnerKindFirst")}
            onChange={(e) => onChange({ ownerId: e.target.value })}
          />
        ) : ownerIsNew ? (
          // Temporary: a single editable combobox. Free-TYPE to create a
          // brand-new temporary agent (name only — repo/branch stay independent,
          // P2). Or open the dropdown and SELECT a temporary agent already
          // defined in another task of THIS flow: that adopts its exact id +
          // repo + target branch (the user-confirmed exception — selecting an
          // enumerated definition overwrites repo/branch).
          (() => {
            const opts = isNonOpenclawKind(row.ownerKind)
              ? flowTempAgentsForKind(
                  row.ownerKind,
                  tasks,
                  row.rowKey,
                )
              : [];
            const current = row.ownerId.trim()
              ? tempAgentValue({
                  id: row.ownerId.trim(),
                  repo: row.ownerRepo.trim(),
                  targetBranch: row.ownerTargetBranch.trim(),
                })
              : "";
            const selectValue = opts.some((o) => tempAgentValue(o) === current)
              ? current
              : "";
            return (
              <TempAgentCombobox
                value={row.ownerId}
                options={opts}
                selectedValue={selectValue}
                disabled={ownerLocked}
                placeholder={t("flowEditor.taskFields.newAgentNamePlaceholder")}
                onType={(text) => onChange({ ownerId: text })}
                onPick={(sel) => {
                  onChange({
                    ownerId: sel.id,
                    ownerRepo: sel.repo,
                    ownerTargetBranch: sel.targetBranch,
                    ownerIsTemporary: true,
                  });
                  commitRepoPath(sel.repo);
                }}
              />
            );
          })()
        ) : ownerIsOpenclaw ? (
          <>
            <select
              className="select"
              value={row.ownerId}
              disabled={ownerLocked}
              onChange={(e) => onChange({ ownerId: e.target.value })}
            >
              <option value="">{t("flowEditor.taskFields.existingAgentPlaceholder")}</option>
              {row.ownerId && !openclawOptions.some((a) => a.id === row.ownerId) && (
                <option value={row.ownerId}>{row.ownerId}</option>
              )}
              {openclawOptions.map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.id})
                </option>
              ))}
            </select>
            {openclawOptions.length === 0 && (
              <div className="text-xs text-ink-500 mt-1">
                {t("flowEditor.taskFields.existingAgentEmpty")}
              </div>
            )}
          </>
        ) : (
          // Persistent non-OpenClaw = Hermes (the only other managed platform):
          // pick from the registered Hermes profiles.
          <>
            <select
              className="select"
              value={row.ownerId}
              disabled={ownerLocked}
              // SELECTING a Hermes agent that another task of this flow already
              // uses overwrites this row's repo/branch from that binding (the
              // user-confirmed exception). Picking one with no in-flow binding
              // leaves repo/branch as-is.
              onChange={(e) => {
                const ownerId = e.target.value;
                const existing = findFlowOwnerWorkspace(
                  tasks,
                  { ownerKind: isOwnerKind(row.ownerKind) ? row.ownerKind : "hermes", ownerId },
                  row.rowKey,
                );
                onChange({
                  ownerId,
                  ...(existing
                    ? {
                        ownerRepo: existing.repo,
                        ownerTargetBranch: existing.targetBranch,
                      }
                    : {}),
                });
                if (existing?.repo) {
                  commitRepoPath(existing.repo);
                }
              }}
            >
              <option value="">{t("flowEditor.hermesAgentPlaceholder")}</option>
              {row.ownerId &&
                !pickAgentsForKind(
                  isOwnerKind(row.ownerKind) ? row.ownerKind : "hermes",
                  hermesOptions,
                ).some(
                  (a) => a.id === row.ownerId,
                ) && <option value={row.ownerId}>{row.ownerId}</option>}
              {pickAgentsForKind(
                isOwnerKind(row.ownerKind) ? row.ownerKind : "hermes",
                hermesOptions,
              ).map((a) => (
                <option key={a.id} value={a.id}>
                  {a.name} ({a.id})
                </option>
              ))}
            </select>
            {pickAgentsForKind(
              isOwnerKind(row.ownerKind) ? row.ownerKind : "hermes",
              hermesOptions,
            ).length === 0 && (
              <div className="text-xs text-ink-500 mt-1">
                {t("flowEditor.hermesAgentEmpty")}
              </div>
            )}
          </>
        )}
      </div>

      {ownerShowsRepoFields && (
        <div className="md:col-span-2">
          <div className="grid gap-2 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
            <div>
              <label className="label">
                {t("flowEditor.taskFields.claudeRepoPath")}
              </label>
              {deploymentMode === "server" ? (
                <>
                  <select
                    className="select"
                    value={row.ownerRepo}
                    disabled={ownerLocked}
                    onChange={(e) => {
                      onRepoPathEditStart();
                      patchOwnerRepo(e.target.value);
                      commitRepoPath(e.target.value);
                    }}
                  >
                    <option value="">
                      {t("flowEditor.taskFields.claudeRepoServerPlaceholder")}
                    </option>
                    {row.ownerRepo &&
                      !workspaceDirOptions.includes(row.ownerRepo) && (
                      <option value={row.ownerRepo}>{row.ownerRepo}</option>
                    )}
                    {workspaceDirOptions.map((p) => (
                      <option key={p} value={p}>
                        {p}
                      </option>
                    ))}
                  </select>
                  <div className="text-xs text-ink-500 mt-1">
                    {workspaceDirOptions.length === 0
                      ? t("flowEditor.taskFields.claudeRepoServerEmpty")
                      : t("flowEditor.taskFields.claudeRepoServerHint")}
                  </div>
                </>
              ) : (
                <div className="flex gap-2">
                  <input
                    className="input flex-1"
                    placeholder={t("flowEditor.taskFields.claudeRepoPlaceholder")}
                    value={row.ownerRepo}
                    readOnly={ownerLocked}
                    onChange={(e) => patchOwnerRepo(e.target.value)}
                    onFocus={onRepoPathEditStart}
                    onBlur={(e) => commitRepoPath(e.target.value)}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                        (e.target as HTMLInputElement).blur();
                      }
                    }}
                  />
                  <button
                    type="button"
                    className="btn-outline whitespace-nowrap"
                    onClick={onPickRepo}
                    disabled={pickingRepo || ownerLocked}
                  >
                    {t("flowEditor.taskFields.pickDirButton")}
                  </button>
                </div>
              )}
            </div>
            <div>
              <label className="label">
                {t("flowEditor.taskFields.targetBranch")}
              </label>
              <select
                className="select"
                value={row.ownerTargetBranch}
                disabled={ownerLocked || !branchEditable || branchLoading || !row.ownerRepo.trim()}
                onChange={(e) => onChange({ ownerTargetBranch: e.target.value })}
              >
                {!row.ownerTargetBranch && (
                  <option value="">
                    {t("flowEditor.taskFields.pickBranch")}
                  </option>
                )}
                {row.ownerTargetBranch &&
                  !branchOptions.includes(row.ownerTargetBranch) && (
                  <option value={row.ownerTargetBranch}>{row.ownerTargetBranch}</option>
                )}
                {branchOptions.map((name) => (
                  <option key={name} value={name}>
                    {name}
                  </option>
                ))}
              </select>
              {branchHelperText && (
                <div className="text-xs text-ink-500 mt-1">{branchHelperText}</div>
              )}
            </div>
          </div>
        </div>
      )}

      <div className="md:col-span-2">
        {!isSummary && showCheckpointField && (
          <>
            <label className="inline-flex items-center gap-2 text-sm text-ink-700 mb-2">
              <input
                type="checkbox"
                checked={row.requiresHumanCheckpoint}
                disabled={readOnly}
                onChange={(e) =>
                  onChange({ requiresHumanCheckpoint: e.target.checked })}
              />
              <span>{t("flowEditor.taskFields.requiresHumanCheckpoint")}</span>
            </label>
            <div className="text-xs text-ink-500 mb-2">
              {t("flowEditor.taskFields.requiresHumanCheckpointHint")}
            </div>
          </>
        )}
        <label
          className={cn(
            "label",
            isSummary && row.dependsOn.length === 0 && "text-rose-700",
          )}
        >
          {t("flowEditor.taskFields.dependsOn")}
        </label>
        <MultiSelect
          options={dependableTasks}
          selected={row.dependsOn}
          disabled={readOnly}
          onChange={(v) => onChange({ dependsOn: v })}
          placeholder={t("common.none")}
          renderLabel={(id) => {
            const dep = tasks.find((r) => r.id === id);
            return dep?.subject.trim() || dep?.id || id;
          }}
        />
        <div
          className={cn(
            "text-xs mt-1",
            isSummary && row.dependsOn.length === 0
              ? "text-rose-700"
              : "text-ink-500",
          )}
        >
          {isSummary && row.dependsOn.length === 0
            ? t("flowEditor.summaryNoDepsWarning")
            : t("flowEditor.taskFields.dependsOnHint")}
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Multi-select dropdown for dependsOn
// ──────────────────────────────────────────────────────────────────────


function MultiSelect({
  options,
  selected,
  onChange,
  placeholder,
  disabled,
  renderLabel,
}: {
  options: string[];
  selected: string[];
  onChange: (v: string[]) => void;
  placeholder?: string;
  disabled?: boolean;
  /** Optional label resolver. Defaults to the option value itself. */
  renderLabel?: (value: string) => string;
}) {
  const labelFor = (v: string) => (renderLabel ? renderLabel(v) : v);
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    function onClick(e: MouseEvent) {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener("mousedown", onClick);
    return () => document.removeEventListener("mousedown", onClick);
  }, []);

  function toggle(v: string) {
    onChange(
      selected.includes(v)
        ? selected.filter((x) => x !== v)
        : [...selected, v],
    );
    setOpen(false);
  }

  return (
    <div className="relative" ref={ref}>
      <button
        type="button"
        className="input text-left flex items-center justify-between"
        disabled={disabled}
        onClick={() => setOpen((x) => !x)}
      >
        <span className="truncate">
          {selected.length === 0
            ? placeholder ?? "Select…"
            : selected.map(labelFor).join(", ")}
        </span>
        <span className="text-ink-400">▾</span>
      </button>
      {open && (
        <div className="absolute z-10 mt-1 w-full max-h-48 overflow-auto rounded-md border border-ink-200 bg-surface shadow-card dark:border-ink-400 dark:bg-ink-200 dark:shadow-lg">
          {options.length === 0 ? (
            <div className="px-3 py-2 text-xs text-ink-500">
              (no other tasks yet)
            </div>
          ) : (
            options.map((o) => (
              <label
                key={o}
                className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-ink-50 dark:hover:bg-white/5 cursor-pointer"
              >
                <input
                  type="checkbox"
                  checked={selected.includes(o)}
                  onChange={() => toggle(o)}
                />
                <span>{labelFor(o)}</span>
              </label>
            ))
          )}
        </div>
      )}
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Dependency graph
// ──────────────────────────────────────────────────────────────────────

/**
 * Compact SVG visualization of the task DAG: each task is a dot positioned
 * by its longest-path depth (column) within its non-summary cohort; edges
 * follow ``dependsOn`` arrows. Summary task pins to its own rightmost
 * column. Hovering a dot reveals the subject in a floating tooltip.
 *
 * Pure-DOM SVG keeps the bundle lean (no D3 / Cytoscape). Layout is
 * deterministic and recomputed on every render.
 */
/** SVG node/edge colors for the dependency graph. SVG attributes can't read
 *  Tailwind utility classes, so we switch the literal palette by theme. The
 *  dark variant mirrors the legend chips (deep tinted fills + light strokes/
 *  text) so the graph reads on the dark canvas instead of glowing white. */
interface GraphPalette {
  edge: string;
  edgeSummary: string;
  workerFill: string;
  workerStroke: string;
  hoverFill: string;
  hoverStroke: string;
  summaryFill: string;
  summaryStroke: string;
  summaryGlyph: string;
  rootFill: string;
  rootStroke: string;
  rootGlyph: string;
  glowSummary: string;
  glowNode: string;
  checkpointFill: string;
  checkpointStroke: string;
  checkpointGlyph: string;
}

const GRAPH_PALETTE_LIGHT: GraphPalette = {
  edge: "#475569",
  edgeSummary: "#ea580c",
  workerFill: "#ffffff",
  workerStroke: "#111827",
  hoverFill: "#f0f9ff",
  hoverStroke: "#0f172a",
  summaryFill: "#fff7ed",
  summaryStroke: "#ea580c",
  summaryGlyph: "#ea580c",
  rootFill: "#ecfeff",
  rootStroke: "#0891b2",
  rootGlyph: "#0891b2",
  glowSummary: "#fdba74",
  glowNode: "#67e8f9",
  checkpointFill: "#fbbf24",
  checkpointStroke: "#92400e",
  checkpointGlyph: "#78350f",
};

const GRAPH_PALETTE_DARK: GraphPalette = {
  edge: "#94a3b8",
  edgeSummary: "#fb923c",
  workerFill: "#1f2632",
  workerStroke: "#cbd5e1",
  hoverFill: "#10212b",
  hoverStroke: "#e2e8f0",
  summaryFill: "#3a1e0b",
  summaryStroke: "#fb923c",
  summaryGlyph: "#fdba74",
  rootFill: "#08313d",
  rootStroke: "#22d3ee",
  rootGlyph: "#67e8f9",
  glowSummary: "#fb923c",
  glowNode: "#22d3ee",
  checkpointFill: "#fbbf24",
  checkpointStroke: "#78350f",
  checkpointGlyph: "#451a03",
};

function DependencyGraph({ tasks }: { tasks: TaskRow[] }) {
  const { t } = useTranslation();
  const [hover, setHover] = useState<string | null>(null);
  const C = useTheme() === "dark" ? GRAPH_PALETTE_DARK : GRAPH_PALETTE_LIGHT;

  const layout = useMemo(() => computeGraphLayout(tasks), [tasks]);
  if (layout.nodes.length === 0) return null;

  const { nodes, edges, width, height } = layout;
  const hovered = nodes.find((n) => n.id === hover);
  const nodeDensityScale = Math.max(
    0.78,
    Math.min(1.24, 1.24 - Math.max(0, nodes.length - 4) * 0.028),
  );
  const nodeRadius = (node: Pick<GraphNode, "isSummary" | "degree">, isHover = false) => {
    const rawBase = node.isSummary ? 14.2 : 9.6 + Math.min(4.4, node.degree * 0.64);
    const scaled = node.isSummary
      ? Math.max(11.2, rawBase * (nodeDensityScale + 0.08))
      : Math.max(7.0, rawBase * nodeDensityScale);
    return isHover ? scaled + 1.8 : scaled;
  };
  const hoveredLeft = hovered
    ? `${Math.max(2, Math.min(98, (hovered.x / Math.max(1, width)) * 100))}%`
    : "0%";
  const hoveredTop = hovered
    ? `${Math.max(2, Math.min(98, (hovered.y / Math.max(1, height)) * 100))}%`
    : "0%";

  return (
    <div className="w-full min-w-0 lg:flex-[2]">
      <div className="text-xs text-ink-500 mb-2">
        {t("flowEditor.graphTitle")}
      </div>
      <div className="min-w-0 w-full min-h-[320px] overflow-hidden rounded-md border border-ink-200 bg-gradient-to-br from-ink-50/40 to-surface">
        <div className="relative w-full">
          <svg
            className="w-full h-auto block"
            viewBox={`0 0 ${width} ${height}`}
            preserveAspectRatio="xMidYMid meet"
          >
          <defs>
            {/* Arrow markers — slim *chevron* (two-stroke open arrow), not
                a filled triangle. ``markerUnits=userSpaceOnUse`` keeps the
                stroke a constant 1.4 px regardless of the parent path's
                stroke-width. */}
            <marker
              id="csflow-arrow"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="9"
              markerHeight="9"
              markerUnits="userSpaceOnUse"
              orient="auto-start-reverse"
            >
              <path
                d="M 1 2 L 9 5 L 1 8"
                fill="none"
                stroke={C.edge}
                strokeWidth="1.4"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </marker>
            <marker
              id="csflow-arrow-summary"
              viewBox="0 0 10 10"
              refX="9"
              refY="5"
              markerWidth="10"
              markerHeight="10"
              markerUnits="userSpaceOnUse"
              orient="auto-start-reverse"
            >
              <path
                d="M 1 2 L 9 5 L 1 8"
                fill="none"
                stroke={C.edgeSummary}
                strokeWidth="1.6"
                strokeLinecap="round"
                strokeLinejoin="round"
              />
            </marker>
            {/* Soft brand-tinted glow used on hover + summary node. */}
            <filter id="csflow-glow" x="-50%" y="-50%" width="200%" height="200%">
              <feGaussianBlur stdDeviation="3" result="blur" />
              <feMerge>
                <feMergeNode in="blur" />
                <feMergeNode in="SourceGraphic" />
              </feMerge>
            </filter>
          </defs>
          {edges.map((e, i) => {
            const summaryEdge = e.toSummary;
            const stroke = summaryEdge ? C.edgeSummary : C.edge;
            const marker = summaryEdge
              ? "url(#csflow-arrow-summary)"
              : "url(#csflow-arrow)";
            // Pull the path endpoint back to the rim of the destination
            // node so the arrowhead doesn't disappear under the circle.
            const target = nodes.find((n) => n.id === e.to);
            const targetR = target ? nodeRadius(target) : 10;
            const dx = e.x2 - e.x1;
            const dy = e.y2 - e.y1;
            const dist = Math.sqrt(dx * dx + dy * dy) || 1;
            const offset = targetR + 3; // tiny gap so arrow doesn't kiss the circle
            const x2 = e.x2 - (dx / dist) * offset;
            const y2 = e.y2 - (dy / dist) * offset;
            const dim = hover && hover !== e.from && hover !== e.to;
            return (
              <path
                key={i}
                d={cubicPath(e.x1, e.y1, x2, y2)}
                stroke={stroke}
                strokeWidth={summaryEdge ? 2 : 1.8}
                fill="none"
                markerEnd={marker}
                opacity={dim ? 0.25 : 0.95}
              />
            );
          })}
          {nodes.map((n) => {
            const summary = n.isSummary;
            const root = n.isRoot;
            const checkpoint = !summary && n.requiresHumanCheckpoint;
            const isHover = hover === n.id;
            // Node radius scales gently with degree so hub tasks stand out
            // in busier networks without dwarfing the leaves.
            const r = nodeRadius(n, isHover);
            // Palette:
            //   - Summary: warm orange (action / terminal)
            //   - Root:    cyan-teal   (entry points — "start here")
            //   - Worker:  neutral grey (intermediate steps)
            const fill = summary
              ? C.summaryFill
              : root
              ? C.rootFill
              : isHover
              ? C.hoverFill
              : C.workerFill;
            const stroke = summary
              ? C.summaryStroke
              : root
              ? C.rootStroke
              : isHover
              ? C.hoverStroke
              : C.workerStroke;
            return (
              <g
                key={n.id}
                transform={`translate(${n.x}, ${n.y})`}
                onMouseEnter={() => setHover(n.id)}
                onMouseLeave={() =>
                  setHover((cur) => (cur === n.id ? null : cur))
                }
                style={{ cursor: "default" }}
              >
                {(isHover || summary) && (
                  <circle
                    r={r + 6}
                    fill={summary ? C.glowSummary : C.glowNode}
                    opacity={summary ? 0.25 : 0.35}
                    filter="url(#csflow-glow)"
                  />
                )}
                <circle
                  r={r}
                  fill={fill}
                  stroke={stroke}
                  strokeWidth={summary ? 2.4 : root ? 2 : 1.8}
                />
                {summary && (
                  <text
                    y={4}
                    textAnchor="middle"
                    fontSize={11}
                    fontWeight="700"
                    fill={C.summaryGlyph}
                  >
                    ★
                  </text>
                )}
                {root && !summary && (
                  <text
                    y={4}
                    textAnchor="middle"
                    fontSize={10}
                    fontWeight="700"
                    fill={C.rootGlyph}
                  >
                    ▶
                  </text>
                )}
                {checkpoint && (
                  <g>
                    <circle
                      cx={r * 0.74}
                      cy={-r * 0.74}
                      r={3.6}
                      fill={C.checkpointFill}
                      stroke={C.checkpointStroke}
                      strokeWidth={1}
                    />
                    <text
                      x={r * 0.74}
                      y={-r * 0.74 + 1.5}
                      textAnchor="middle"
                      fontSize={5.5}
                      fontWeight="700"
                      fill={C.checkpointGlyph}
                    >
                      !
                    </text>
                  </g>
                )}
              </g>
            );
          })}
          </svg>
          {hovered && (
            <div
              className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md bg-ink-900 px-2 py-1 text-xs text-ink-50 shadow-md whitespace-nowrap"
              style={{ left: hoveredLeft, top: hoveredTop }}
            >
              {hovered.subject || t("flowEditor.rowUntitled")}
            </div>
          )}
        </div>
      </div>
      <div className="mt-2 flex items-center gap-4 text-[11px] text-ink-500">
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-cyan-600 bg-cyan-50" />
          {t("flowEditor.graphLegendRoot")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-ink-900 bg-surface" />
          {t("flowEditor.graphLegendTask")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-flex h-3 w-3 items-center justify-center rounded-full border border-amber-700 bg-amber-300 text-[9px] font-bold text-amber-900">
            !
          </span>
          {t("flowEditor.graphLegendCheckpoint")}
        </span>
        <span className="inline-flex items-center gap-1.5">
          <span className="inline-block w-3 h-3 rounded-full border-2 border-orange-500 bg-orange-50" />
          {t("flowEditor.graphLegendSummary")}
        </span>
      </div>
    </div>
  );
}


interface GraphNode {
  id: string;
  subject: string;
  isSummary: boolean;
  requiresHumanCheckpoint: boolean;
  /** True iff this task has no incoming dependencies (root of the DAG).
   *  Used to tint the node so the user can tell where execution begins. */
  isRoot: boolean;
  x: number;
  y: number;
  /** Connection count (in + out). Drives node size. */
  degree: number;
}
interface GraphEdge {
  from: string;
  to: string;
  x1: number;
  y1: number;
  x2: number;
  y2: number;
  toSummary: boolean;
}

/**
 * Network-style layout: a lightweight Fruchterman–Reingold force simulation
 * with a soft horizontal bias toward each node's topological depth. The
 * result keeps the "upstream-left, downstream-right" reading order of a
 * traditional DAG drawing while letting nodes spread organically along the
 * vertical axis — so even chain-shaped DAGs read as a network rather than
 * a rigid column ladder.
 *
 * Determinism: a small string-hash PRNG seeds the initial scatter so the
 * layout is stable across renders (no jitter while the user is editing).
 *
 * Complexity: O(iters · n²). With ITER=180 and n≤30 (typical Flow size)
 * this stays well under 5 ms in practice; we don't memoise inside the
 * function — ``DependencyGraph`` already wraps it in ``useMemo``.
 */
function computeGraphLayout(tasks: TaskRow[]): {
  nodes: GraphNode[];
  edges: GraphEdge[];
  width: number;
  height: number;
} {
  const usableTasks = tasks.filter((r) => r.id.trim());
  if (usableTasks.length === 0) {
    return { nodes: [], edges: [], width: 0, height: 0 };
  }
  const byId = new Map<string, TaskRow>();
  for (const r of usableTasks) byId.set(r.id, r);

  // ── Depth (longest path from a root) ────────────────────────────────
  const depth = new Map<string, number>();
  const visiting = new Set<string>();
  function depthOf(id: string): number {
    if (depth.has(id)) return depth.get(id)!;
    if (visiting.has(id)) return 0; // cycle guard
    visiting.add(id);
    const r = byId.get(id);
    const deps = (r?.dependsOn ?? []).filter((d) => byId.has(d));
    const d = deps.length === 0 ? 0 : 1 + Math.max(...deps.map(depthOf));
    visiting.delete(id);
    depth.set(id, d);
    return d;
  }
  for (const r of usableTasks) depthOf(r.id);

  let maxWorkerDepth = 0;
  for (const r of usableTasks) {
    if (!r.isLeaderSummary) {
      maxWorkerDepth = Math.max(maxWorkerDepth, depth.get(r.id)!);
    }
  }
  // Summary anchors one step further than every worker.
  const totalDepth = maxWorkerDepth + 1;

  // ── Canvas sizing (align with RunDetail dependency board strategy) ---
  const n = usableTasks.length;
  const BOARD_CANVAS_MIN_WIDTH = 320;
  const BOARD_CANVAS_MIN_HEIGHT = 340;
  const BOARD_PAD_X = 56;
  const BOARD_PAD_Y = 42;
  const width = Math.max(
    BOARD_CANVAS_MIN_WIDTH,
    260 + n * 84 + Math.max(0, totalDepth - 1) * 22,
  );
  const height = Math.max(
    BOARD_CANVAS_MIN_HEIGHT,
    210 + Math.ceil(n / 2) * 72,
  );
  const innerWidth = width - BOARD_PAD_X * 2;
  const innerHeight = height - BOARD_PAD_Y * 2;

  // ── Edges + degree --------------------------------------------------
  const edgeList: [string, string][] = [];
  const degree = new Map<string, number>();
  for (const r of usableTasks) degree.set(r.id, 0);
  for (const r of usableTasks) {
    for (const dep of r.dependsOn) {
      if (!byId.has(dep)) continue;
      edgeList.push([dep, r.id]);
      degree.set(dep, (degree.get(dep) ?? 0) + 1);
      degree.set(r.id, (degree.get(r.id) ?? 0) + 1);
    }
  }

  // ── Deterministic init via string-hash PRNG -------------------------
  function rng(seed: string) {
    let h = 2166136261;
    for (let i = 0; i < seed.length; i += 1) {
      h ^= seed.charCodeAt(i);
      h = Math.imul(h, 16777619);
    }
    return () => {
      h = Math.imul(h ^ (h >>> 15), 2246822507);
      h = Math.imul(h ^ (h >>> 13), 3266489909);
      h ^= h >>> 16;
      return ((h >>> 0) % 10000) / 10000;
    };
  }

  interface FNode {
    id: string;
    x: number;
    y: number;
    fx: number;
    fy: number;
    targetX: number;
  }
  const nodes: FNode[] = usableTasks.map((r) => {
    const d = r.isLeaderSummary ? totalDepth : depth.get(r.id)!;
    const rand = rng(r.id);
    const targetX = totalDepth > 0
      ? BOARD_PAD_X + (d / totalDepth) * innerWidth
      : BOARD_PAD_X + innerWidth / 2;
    return {
      id: r.id,
      x: targetX + (rand() - 0.5) * 26,
      y: BOARD_PAD_Y + rand() * innerHeight,
      fx: 0,
      fy: 0,
      targetX,
    };
  });
  const nodeById = new Map(nodes.map((n2) => [n2.id, n2] as const));

  // ── Force simulation -------------------------------------------------
  const ITERS = 140;
  const k = Math.sqrt((width * height) / Math.max(1, n)) * 0.6;
  for (let iter = 0; iter < ITERS; iter += 1) {
    for (const a of nodes) {
      a.fx = 0;
      a.fy = 0;
    }
    // Repulsion (all pairs)
    for (let i = 0; i < nodes.length; i += 1) {
      for (let j = i + 1; j < nodes.length; j += 1) {
        const a = nodes[i];
        const b = nodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.5) {
          // Same-position guard: nudge apart deterministically.
          dx = (i - j) * 0.7;
          dy = (j - i) * 0.7;
          d2 = dx * dx + dy * dy;
        }
        const d = Math.sqrt(d2);
        const force = (k * k) / d;
        a.fx += (dx / d) * force;
        a.fy += (dy / d) * force;
        b.fx -= (dx / d) * force;
        b.fy -= (dy / d) * force;
      }
    }
    // Attraction along edges
    for (const [fromId, toId] of edgeList) {
      const a = nodeById.get(fromId)!;
      const b = nodeById.get(toId)!;
      const dx = a.x - b.x;
      const dy = a.y - b.y;
      const d = Math.sqrt(dx * dx + dy * dy) || 1;
      const force = (d * d) / k;
      const ax = (dx / d) * force;
      const ay = (dy / d) * force;
      a.fx -= ax;
      a.fy -= ay;
      b.fx += ax;
      b.fy += ay;
    }
    // Horizontal bias toward target column (same strategy as run board).
    for (const a of nodes) {
      a.fx += (a.targetX - a.x) * 0.22;
    }
    const temperature = (1 - iter / ITERS) * 13 + 1;
    for (const a of nodes) {
      const fmag = Math.sqrt(a.fx * a.fx + a.fy * a.fy) || 1;
      const step = Math.min(fmag, temperature);
      a.x += (a.fx / fmag) * step;
      a.y += (a.fy / fmag) * step;
      a.x = Math.max(BOARD_PAD_X - 6, Math.min(width - BOARD_PAD_X + 6, a.x));
      a.y = Math.max(BOARD_PAD_Y - 6, Math.min(height - BOARD_PAD_Y + 6, a.y));
    }
  }

  // Tight-crop to content, identical spirit to RunDetail's board.
  const minX = Math.min(...nodes.map((nn) => nn.x));
  const maxX = Math.max(...nodes.map((nn) => nn.x));
  const minY = Math.min(...nodes.map((nn) => nn.y));
  const maxY = Math.max(...nodes.map((nn) => nn.y));
  const offsetX = BOARD_PAD_X - minX;
  const offsetY = BOARD_PAD_Y - minY;
  const tightWidth = Math.max(
    BOARD_CANVAS_MIN_WIDTH,
    (maxX - minX) + BOARD_PAD_X * 2,
  );
  const tightHeight = Math.max(
    BOARD_CANVAS_MIN_HEIGHT,
    (maxY - minY) + BOARD_PAD_Y * 2,
  );

  // ── Materialise public node + edge lists -----------------------------
  // A "root" task has no upstream dependencies. Highlighting them makes
  // it obvious to the user where the Flow's execution actually starts.
  const outNodes: GraphNode[] = usableTasks.map((r) => {
    const fn = nodeById.get(r.id)!;
    const deps = r.dependsOn.filter((d) => byId.has(d));
    return {
      id: r.id,
      subject: r.subject.trim(),
      isSummary: r.isLeaderSummary,
      requiresHumanCheckpoint: !r.isLeaderSummary && !!r.requiresHumanCheckpoint,
      isRoot: !r.isLeaderSummary && deps.length === 0,
      x: fn.x + offsetX,
      y: fn.y + offsetY,
      degree: degree.get(r.id) ?? 0,
    };
  });
  const positions = new Map(outNodes.map((n2) => [n2.id, n2] as const));
  const outEdges: GraphEdge[] = [];
  for (const r of usableTasks) {
    for (const dep of r.dependsOn) {
      const from = positions.get(dep);
      const to = positions.get(r.id);
      if (!from || !to) continue;
      outEdges.push({
        from: dep,
        to: r.id,
        x1: from.x,
        y1: from.y,
        x2: to.x,
        y2: to.y,
        toSummary: !!byId.get(r.id)?.isLeaderSummary,
      });
    }
  }

  return {
    nodes: outNodes,
    edges: outEdges,
    width: tightWidth,
    height: tightHeight,
  };
}

/**
 * Straight edge from (x1,y1) to (x2,y2). Kept as a function (rather than
 * inlined) so swapping back to curves later is a one-spot change and the
 * arrow-marker math in ``DependencyGraph`` doesn't have to know about it.
 */
function cubicPath(x1: number, y1: number, x2: number, y2: number): string {
  return `M ${x1} ${y1} L ${x2} ${y2}`;
}


// ── Validation ───────────────────────────────────────────────────────


function validate(
  rows: TaskRow[],
  messages: ValidationMessages,
  openclawIds: Set<string>,
  hermesIds: Set<string>,
): { rowKey?: string; message: string }[] {
  const issues: { rowKey?: string; message: string }[] = [];
  const ids = new Set<string>();
  const ownerRepoBranch = new Map<string, { repo: string; branch: string }>();
  const ownerConflictReported = new Set<string>();
  // A FlowAgent id is global within a flow regardless of platform: rowsToSpec
  // keys agents by ownerId alone, so two rows sharing an id but with different
  // kinds would silently collapse into one agent. Surface that as an error
  // instead (mirrors the backend DUPLICATE_AGENT_ID cross-platform check).
  const ownerKindById = new Map<string, string>();
  const crossKindReported = new Set<string>();
  const summary = rows.filter((r) => r.isLeaderSummary);
  if (summary.length === 0) {
    issues.push({ message: messages.pickOneSummary });
  }
  if (summary.length > 1) {
    issues.push({ message: messages.onlyOneSummary });
  }
  // The summary must depend on at least one upstream task (it reviews and
  // reports on them). Global (not per-row) so it blocks save without
  // duplicating the row-level summaryNoDepsWarning hint. Backend enforces this
  // too (SUMMARY_NO_DEPENDENCY).
  if (summary.length === 1 && summary[0].dependsOn.length === 0) {
    issues.push({ message: messages.summaryNeedsDependency });
  }
  const leaderKey = summary[0] ? ownerKey(summary[0]) : null;
  // Only flag stale OpenClaw refs once we've actually loaded the list —
  // otherwise the first render would mark every reference broken.
  const checkOpenclawExistence = openclawIds.size > 0;
  const checkHermesExistence = hermesIds.size > 0;

  for (const r of rows) {
    if (!r.id.trim()) {
      issues.push({ rowKey: r.rowKey, message: messages.taskIdRequired });
    } else if (!ID_PATTERN.test(r.id)) {
      issues.push({ rowKey: r.rowKey, message: messages.taskIdPattern });
    } else if (ids.has(r.id)) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.duplicateTaskId(r.id),
      });
    } else {
      ids.add(r.id);
    }
    const subjectLabel = r.subject.trim() || r.id;
    if (!r.subject.trim()) {
      issues.push({ rowKey: r.rowKey, message: messages.subjectRequired });
    }
    // Description must contain an instruction body — without one the
    // worker / leader dispatch prompt has nothing to act on.
    if (!r.description.trim()) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.descriptionRequired(subjectLabel),
      });
    }
    if (!isOwnerKind(r.ownerKind)) {
      issues.push({ rowKey: r.rowKey, message: messages.ownerKindRequired });
      continue;
    }
    if (r.ownerIsTemporary && !isNonOpenclawKind(r.ownerKind)) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.ownerKindUnavailable({
          subject: subjectLabel,
          kindLabel: r.ownerKind,
        }),
      });
      continue;
    }
    if (!r.ownerIsTemporary && !isPersistentOwnerKind(r.ownerKind)) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.ownerKindUnavailable({
          subject: subjectLabel,
          kindLabel: r.ownerKind,
        }),
      });
      continue;
    }
    if (!r.ownerId.trim()) {
      issues.push({
        rowKey: r.rowKey,
        message:
          r.ownerKind === "openclaw"
            ? messages.pickOpenclawAgent
            : messages.ownerAgentNameRequired,
      });
    } else if (!ID_PATTERN.test(r.ownerId.trim())) {
      issues.push({ rowKey: r.rowKey, message: messages.ownerAgentIdPattern });
    } else if (
      r.ownerKind === "openclaw" &&
      checkOpenclawExistence &&
      !openclawIds.has(r.ownerId.trim())
    ) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.openclawAgentMissing(subjectLabel, r.ownerId.trim()),
      });
    } else if (
      // Persistent Hermes owner must reference a real Hermes profile. Temporary
      // agents are ad-hoc (no managed store) and skip this check.
      r.ownerKind === "hermes" &&
      !r.ownerIsTemporary &&
      checkHermesExistence &&
      !hermesIds.has(r.ownerId.trim())
    ) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.hermesAgentMissing(subjectLabel, r.ownerId.trim()),
      });
    }
    if (r.ownerId.trim() && ID_PATTERN.test(r.ownerId.trim())) {
      const oid = r.ownerId.trim();
      const prevKind = ownerKindById.get(oid);
      if (prevKind === undefined) {
        ownerKindById.set(oid, r.ownerKind);
      } else if (prevKind !== r.ownerKind && !crossKindReported.has(oid)) {
        crossKindReported.add(oid);
        issues.push({
          rowKey: r.rowKey,
          message: messages.duplicateAgentIdCrossKind(oid),
        });
      }
    }
    if (isNonOpenclawKind(r.ownerKind) && !r.ownerRepo.trim()) {
      issues.push({ rowKey: r.rowKey, message: messages.claudeRepoRequired });
    }
    if (isNonOpenclawKind(r.ownerKind) && !r.ownerTargetBranch.trim()) {
      issues.push({ rowKey: r.rowKey, message: messages.claudeTargetBranchRequired });
    }
    if (isNonOpenclawKind(r.ownerKind) && r.ownerId.trim()) {
      const ownerKey = `${r.ownerKind}:${r.ownerId.trim()}`;
      const repo = r.ownerRepo.trim();
      const branch = r.ownerTargetBranch.trim();
      const prev = ownerRepoBranch.get(ownerKey);
      if (!prev) {
        ownerRepoBranch.set(ownerKey, { repo, branch });
      } else if (
        (!sameRepoPathForCompare(prev.repo, repo) || prev.branch !== branch)
        && !ownerConflictReported.has(ownerKey)
      ) {
        ownerConflictReported.add(ownerKey);
        issues.push({
          rowKey: r.rowKey,
          message: messages.ownerRepoBranchMismatch(r.ownerId.trim()),
        });
      }
    }
    if (leaderKey && !r.isLeaderSummary && ownerKey(r) === leaderKey) {
      issues.push({
        rowKey: r.rowKey,
        message: messages.leaderCannotOwnNonSummary,
      });
    }
  }
  const cycle = detectTaskCycle(rows);
  if (cycle.length > 0) {
    issues.push({ message: messages.cycleDetected(cycle.join(" -> ")) });
  }
  return issues;
}


function detectTaskCycle(rows: TaskRow[]): string[] {
  const graph = new Map<string, string[]>();
  const allIds = new Set(
    rows
      .map((r) => r.id.trim())
      .filter((id) => ID_PATTERN.test(id)),
  );
  for (const r of rows) {
    const id = r.id.trim();
    if (!id || !allIds.has(id) || graph.has(id)) continue;
    graph.set(id, r.dependsOn.filter((d) => allIds.has(d)));
  }

  const visiting = new Set<string>();
  const visited = new Set<string>();
  const stack: string[] = [];

  function visit(node: string): string[] | null {
    visiting.add(node);
    stack.push(node);
    for (const dep of graph.get(node) ?? []) {
      if (visiting.has(dep)) {
        const start = stack.indexOf(dep);
        const cycle = stack.slice(start);
        cycle.push(dep);
        return cycle;
      }
      if (!visited.has(dep) && graph.has(dep)) {
        const hit = visit(dep);
        if (hit) return hit;
      }
    }
    stack.pop();
    visiting.delete(node);
    visited.add(node);
    return null;
  }

  for (const node of graph.keys()) {
    if (visited.has(node)) continue;
    const cycle = visit(node);
    if (cycle) return cycle;
  }
  return [];
}

// ── rowsToSpec / specToRows ──────────────────────────────────────────


function rowsToSpec(rows: TaskRow[], runInputFields: string[] = []): FlowSpec {
  const byKey = new Map<string, FlowAgent>();
  for (const r of rows) {
    const k = r.ownerId.trim();
    if (!k) continue;
    if (!isOwnerKind(r.ownerKind)) continue;
    if (byKey.has(k)) continue;
    const isLeader = r.isLeaderSummary;
    const agent: FlowAgent =
      r.ownerKind === "openclaw"
        ? {
            id: r.ownerId.trim(),
            kind: "openclaw" as AgentKind,
            isLeader,
            repo: null,
            targetBranch: null,
            // OpenClaw is always a persistent agent.
            isTemporary: false,
          }
        : {
            id: r.ownerId.trim(),
            kind: r.ownerKind as AgentKind,
            isLeader,
            repo: r.ownerRepo.trim(),
            targetBranch: r.ownerTargetBranch.trim(),
            isTemporary: !!r.ownerIsTemporary,
          };
    byKey.set(k, agent);
  }
  const summary = rows.find((r) => r.isLeaderSummary);
  if (summary) {
    const leaderKey = summary.ownerId.trim();
    for (const [k, a] of byKey) {
      a.isLeader = k === leaderKey;
    }
  }

  const tasks: FlowTask[] = rows.map((r) => ({
    id: r.id.trim(),
    ownerAgentId: r.ownerId.trim(),
    subject: r.subject.trim(),
    description: r.description,
    outputSummaryRequirement: r.outputSummaryRequirement.trim() || null,
    requiresHumanCheckpoint: !r.isLeaderSummary && !!r.requiresHumanCheckpoint,
    // Developer-mode per-task auto-merge (default true). OpenClaw is forced on.
    devAutoMerge: r.ownerKind === "openclaw" ? true : r.autoMerge !== false,
    dependsOn: r.dependsOn,
    isLeaderSummary: r.isLeaderSummary,
    timeoutSeconds: r.timeoutSeconds || DEFAULT_TIMEOUT_SECONDS,
  }));

  return setRunInputFields(
    { agents: Array.from(byKey.values()), tasks },
    runInputFields,
  );
}


// ──────────────────────────────────────────────────────────────────────
// AI Decompose modal — auto-starts on open (no input form).
// ──────────────────────────────────────────────────────────────────────


interface DecomposeProposal {
  agents: Record<string, unknown>[];
  tasks: Record<string, unknown>[];
}

const DECOMPOSE_MODAL_TIMEOUT_SECONDS = 1800;


function DecomposeModal({
  open,
  goal,
  leaderKind,
  leaderId,
  leaderRepo,
  leaderTargetBranch,
  existingRows,
  openclawAgents,
  onClose,
  onApply,
}: {
  open: boolean;
  goal: string;
  leaderKind: OwnerKindDraft;
  leaderId: string;
  leaderRepo: string;
  leaderTargetBranch: string;
  existingRows: TaskRow[];
  openclawAgents: OpenclawAgentSummary[];
  onClose: () => void;
  onApply: (proposal: DecomposeProposal) => void;
}) {
  const { t, i18n } = useTranslation();
  const [requestId, setRequestId] = useSessionBackedState<string | null>(
    "flow-editor:decompose-request-id",
    null,
    { isClosed: (value) => value === null },
  );
  const [status, setStatus] = useState<DecomposeStatus | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [retrySeq, setRetrySeq] = useState(0);
  const [localTimeout, setLocalTimeout] = useState(false);
  const [cancelling, setCancelling] = useState(false);
  const [applying, setApplying] = useState(false);
  const [confirmCloseOpen, setConfirmCloseOpen] = useState(false);
  const [dispatchStarted, setDispatchStarted] = useState(false);
  /** Wall-clock seconds since the modal opened. Drives the "elapsed
   *  N s" counter so the user can see the wait is alive. Stops as soon
   *  as the request hits a terminal state. */
  const [elapsedSec, setElapsedSec] = useState(0);
  const startRunKeyRef = useRef<string | null>(null);
  const statusTerminal =
    status !== null
    && (
      status.status === "succeeded"
      || status.status === "failed"
      || status.status === "timed_out"
    );
  const timedOutLocally = localTimeout && !statusTerminal;

  // Auto-start a decompose request as soon as the modal opens.
  useEffect(() => {
    if (!open) {
      startRunKeyRef.current = null;
      setRequestId(null);
      setStatus(null);
      setError(null);
      setLocalTimeout(false);
      setCancelling(false);
      setConfirmCloseOpen(false);
      setDispatchStarted(false);
      setElapsedSec(0);
      return;
    }
    const normalizedGoal = goal.trim();
    const normalizedLeaderId = leaderId.trim();
    const normalizedLeaderRepo = leaderRepo.trim();
    const normalizedLeaderTargetBranch = leaderTargetBranch.trim();
    // Refresh restore path: if a request id is already persisted and this
    // isn't an explicit retry, re-attach polling instead of starting again.
    if (requestId && retrySeq === 0) return;
    // Flow data may hydrate asynchronously after refresh; avoid firing a
    // doomed request before required fields are ready.
    if (!normalizedGoal || !normalizedLeaderId) {
      startRunKeyRef.current = null;
      return;
    }
    if (isNonOpenclawKind(leaderKind) && !normalizedLeaderRepo) {
      startRunKeyRef.current = null;
      return;
    }
    // React StrictMode replays effects in development. Guard by logical
    // run-key so a single open/retry cycle only starts one backend request.
    const runKey = [
      `retry:${retrySeq}`,
      `goal:${normalizedGoal}`,
      `leader:${leaderKind}:${normalizedLeaderId}`,
      `repo:${normalizedLeaderRepo}`,
      `target:${normalizedLeaderTargetBranch}`,
    ].join("|");
    if (startRunKeyRef.current === runKey) return;
    startRunKeyRef.current = runKey;
    setRequestId(null);
    setStatus(null);
    setError(null);
    setLocalTimeout(false);
    setCancelling(false);
    setDispatchStarted(true);
    setElapsedSec(0);
    const resultLanguage = i18n.resolvedLanguage?.startsWith("zh") ? "zh" : "en";
    let cancelled = false;
    (async () => {
      try {
        const r = await api.startDecompose({
          goal: normalizedGoal,
          leaderAgentId: normalizedLeaderId,
          leaderKind: isOwnerKind(leaderKind) ? leaderKind : undefined,
          leaderRepo: isNonOpenclawKind(leaderKind) ? normalizedLeaderRepo : null,
          leaderTargetBranch: isNonOpenclawKind(leaderKind)
            ? normalizedLeaderTargetBranch
            : null,
          existingAgents: rowsToHintAgents(existingRows),
          existingTasks: rowsToHintTasks(existingRows),
          resultLanguage,
        });
        if (cancelled) return;
        setRequestId(r.requestId);
      } catch (e) {
        if (cancelled) return;
        setDispatchStarted(false);
        setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [
    open,
    goal,
    leaderKind,
    leaderId,
    leaderRepo,
    leaderTargetBranch,
    requestId,
    retrySeq,
    setRequestId,
  ]);

  // 1-Hz elapsed counter — visible feedback that the wait is alive.
  // Tied to ``open`` + terminal status so it doesn't keep ticking once
  // the leader's response has landed (or failed / timed out).
  useEffect(() => {
    if (!open) return;
    if (statusTerminal || localTimeout) return;
    const id = setInterval(() => setElapsedSec((s) => s + 1), 1000);
    return () => clearInterval(id);
  }, [localTimeout, open, statusTerminal]);

  // Client-side guardrail: if the request still has no terminal result
  // after 30 minutes, stop waiting and surface a retry action.
  useEffect(() => {
    if (!open || !requestId) return;
    if (localTimeout || statusTerminal) return;
    if (elapsedSec < DECOMPOSE_MODAL_TIMEOUT_SECONDS) return;
    setLocalTimeout(true);
  }, [elapsedSec, localTimeout, open, requestId, statusTerminal]);

  // Poll status while a request is in flight.
  useEffect(() => {
    if (!requestId || localTimeout) return;
    let cancelled = false;
    async function tick() {
      try {
        const s = await api.decomposeStatus(requestId!);
        if (cancelled) return;
        setStatus(s);
        if (
          s.status === "succeeded" ||
          s.status === "failed" ||
          s.status === "timed_out"
        ) {
          return;
        }
      } catch (e) {
        if (cancelled) return;
        setError(e instanceof ApiError ? e.message : String(e));
        return;
      }
      setTimeout(tick, 1000);
    }
    tick();
    return () => {
      cancelled = true;
    };
  }, [localTimeout, requestId]);

  function applyProposal() {
    if (!requestId || !status?.resultAgents || !status?.resultTasks) return;
    setApplying(true);
    setError(null);
    void api
      .applyDecompose(requestId)
      .then((r) => {
        onApply({ agents: r.agents, tasks: r.tasks });
      })
      .catch((e) => {
        setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
      })
      .finally(() => {
        setApplying(false);
      });
  }

  function retryDecompose() {
    setRequestId(null);
    setStatus(null);
    setError(null);
    setLocalTimeout(false);
    setCancelling(false);
    setConfirmCloseOpen(false);
    setDispatchStarted(false);
    setElapsedSec(0);
    startRunKeyRef.current = null;
    setRetrySeq((v) => v + 1);
  }

  async function cancelDecompose() {
    if (!requestId) return;
    if (cancelling) return;
    setCancelling(true);
    try {
      await api.cancelDecompose(requestId);
      onClose();
      return;
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setError(t("flowEditor.decompose.cancelFailed", { message: msg }));
    }
    setCancelling(false);
  }

  const badgeStatus = timedOutLocally ? "timed_out" : status?.status ?? null;
  const cancelActionVisible = (dispatchStarted || Boolean(requestId)) && !statusTerminal;
  const cancelActionEnabled = cancelActionVisible && Boolean(requestId) && !cancelling;
  const shouldConfirmClose = cancelActionVisible;

  function requestModalClose() {
    if (!shouldConfirmClose) {
      onClose();
      return;
    }
    setConfirmCloseOpen(true);
  }

  async function confirmModalClose() {
    setConfirmCloseOpen(false);
    if (!cancelActionEnabled) {
      onClose();
      return;
    }
    await cancelDecompose();
  }

  return (
    <Modal
      open={open}
      onClose={requestModalClose}
      title={t("flowEditor.decompose.title")}
      width="max-w-2xl"
    >
      <div className="space-y-3">
        <p className="text-sm text-ink-600">{t("flowEditor.decompose.hint")}</p>
        {leaderKind === "openclaw" && openclawAgents.length === 0 && (
          <ErrorBox>{t("flowEditor.decompose.leaderEmpty")}</ErrorBox>
        )}
        {error && <ErrorBox>{error}</ErrorBox>}

        {!status && !error && !timedOutLocally && (
          <div className="space-y-2">
            <Loading
              label={t("flowEditor.decompose.polling", { seconds: elapsedSec })}
            />
            <p className="text-xs text-ink-500">
              {t("flowEditor.decompose.waitingHint")}
            </p>
          </div>
        )}

        {badgeStatus && (
          <div className="flex items-center gap-2 text-sm">
            {requestId && (
              <code className="text-xs font-mono text-ink-500">{requestId}</code>
            )}
            <StatusPill status={badgeStatus} />
          </div>
        )}

        {status?.status === "succeeded" && status.resultTasks && (
          <>
            <div className="text-sm text-ink-700">
              ✅ {t("flowEditor.decompose.result")}:{" "}
              {t("flowEditor.decompose.resultCounts", {
                taskCount: status.resultTasks.length,
                agentCount: status.resultAgents?.length ?? 0,
              })}
            </div>
            <pre className="text-xs bg-ink-900 text-ink-100 rounded-md p-3 overflow-auto max-h-72">
{JSON.stringify({ agents: status.resultAgents, tasks: status.resultTasks }, null, 2)}
            </pre>
            <div className="flex justify-end gap-2">
              <button
                className="btn-outline"
                onClick={requestModalClose}
              >
                {t("common.close")}
              </button>
              <button
                className="btn-primary"
                onClick={applyProposal}
                disabled={applying}
              >
                ⬇ {applying ? t("flowEditor.decompose.applying") : t("flowEditor.decompose.apply")}
              </button>
            </div>
          </>
        )}

        {(timedOutLocally
          || (status && (status.status === "failed" || status.status === "timed_out"))) && (
            <>
              <ErrorBox>
                {timedOutLocally
                  ? t("flowEditor.decompose.timeoutFailed")
                  : t("flowEditor.decompose.failed", {
                    message:
                      status?.errorMessage?.trim() ||
                      status?.errorCode ||
                      t("common.failed"),
                  })}
              </ErrorBox>
              <div className="flex justify-end gap-2">
                <button
                  className="btn-outline"
                  onClick={requestModalClose}
                >
                  {t("common.close")}
                </button>
                {(timedOutLocally || status?.status === "timed_out") && (
                  <button className="btn-primary" onClick={retryDecompose}>
                    {t("flowEditor.decompose.retry")}
                  </button>
                )}
              </div>
            </>
          )}

        {status &&
          !timedOutLocally &&
          !["succeeded", "failed", "timed_out"].includes(status.status) && (
            <div className="space-y-2">
              <Loading
                label={t("flowEditor.decompose.polling", {
                  seconds: elapsedSec,
                })}
              />
              <p className="text-xs text-ink-500">
                {t("flowEditor.decompose.waitingHint")}
              </p>
            </div>
          )}

        {confirmCloseOpen && (
          <div className="rounded-md border border-amber-200 bg-amber-50 px-4 py-3">
            <p className="text-sm text-amber-900">
              {t("flowEditor.decompose.confirmCancelClose")}
            </p>
            <div className="mt-3 flex justify-end gap-2">
              <button
                type="button"
                className="btn-outline"
                onClick={() => setConfirmCloseOpen(false)}
              >
                {t("common.no")}
              </button>
              <button
                type="button"
                className="btn-danger"
                onClick={() => {
                  void confirmModalClose();
                }}
              >
                {t("flowEditor.decompose.confirmCancel")}
              </button>
            </div>
          </div>
        )}

        {cancelActionVisible && (
          <div className="flex justify-end pt-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => setConfirmCloseOpen(true)}
              disabled={!cancelActionEnabled}
            >
              {cancelling ? t("flowEditor.decompose.cancelling") : t("flowEditor.decompose.cancelDecompose")}
            </button>
          </div>
        )}
      </div>
    </Modal>
  );
}


/** Convert task rows → minimal hint dicts the decomposer skill can read. */
function rowsToHintTasks(rows: TaskRow[]): Record<string, unknown>[] {
  return rows
    .filter((r) => r.id || r.subject)
    .map((r) => ({
      id: r.id,
      subject: r.subject,
      ownerAgentId: r.ownerId,
      dependsOn: r.dependsOn,
      isLeaderSummary: r.isLeaderSummary,
    }));
}


/** Squash duplicate (kind, id) tuples into hint agents. */
function rowsToHintAgents(rows: TaskRow[]): Record<string, unknown>[] {
  const seen = new Set<string>();
  const out: Record<string, unknown>[] = [];
  for (const r of rows) {
    if (!r.ownerId.trim()) continue;
    const key = ownerKey(r);
    if (seen.has(key)) continue;
    seen.add(key);
    out.push({
      id: r.ownerId,
      kind: r.ownerKind,
      repo: isNonOpenclawKind(r.ownerKind) ? r.ownerRepo : null,
      targetBranch: isNonOpenclawKind(r.ownerKind)
        ? r.ownerTargetBranch.trim()
        : null,
      isLeader: r.isLeaderSummary,
    });
  }
  return out;
}


/** Hydrate a decomposer proposal into editor TaskRow[]. */
function proposalToRows(
  proposal: DecomposeProposal,
  openclawOptions: OpenclawAgentSummary[],
  hermesOptions: HermesAgentSummary[],
): TaskRow[] {
  const byId = new Map<
    string,
    { kind: OwnerKind; repo: string; targetBranch: string; isTemporary: boolean }
  >();
  for (const a of proposal.agents) {
    const aid = String(a.id ?? "").trim();
    if (!aid) continue;
    const kind: OwnerKind = toOwnerKind(a.kind);
    const repo = String(a.repo ?? "");
    const rawBranch = a.targetBranch ?? a.target_branch;
    const targetBranch =
      rawBranch === null || rawBranch === undefined
        ? ""
        : String(rawBranch).trim();
    // Temporariness is derived from the registry, NOT from the decomposer's
    // self-reported flag (which it often gets wrong). A Hermes agent is
    // persistent ONLY when it matches a registered managed Hermes profile; an
    // unregistered Hermes id (e.g. one the decomposer invented) is temporary —
    // exactly like Claude/Codex/Cursor — so it never trips the managed-existence
    // check on save. OpenClaw is always persistent. This mirrors the backend
    // _resolve_leader_target classification.
    const isTemporary =
      kind === "openclaw"
        ? false
        : kind === "hermes"
        ? !hermesOptions.some((h) => h.id === aid)
        : true;
    byId.set(aid, { kind, repo, targetBranch, isTemporary });
  }
  for (const a of openclawOptions) {
    if (!byId.has(a.id)) {
      byId.set(a.id, { kind: "openclaw", repo: "", targetBranch: "", isTemporary: false });
    }
  }

  return proposal.tasks.map((tk) => {
    const ownerId = String(tk.ownerAgentId ?? tk.owner_agent_id ?? "").trim();
    const meta = byId.get(ownerId) ?? {
      kind: "claude" as OwnerKind,
      repo: "",
      targetBranch: "",
      isTemporary: true,
    };
    const dependsOn = Array.isArray(tk.dependsOn ?? tk.depends_on)
      ? ((tk.dependsOn ?? tk.depends_on) as unknown[]).map(String)
      : [];
    return {
      rowKey: newRowKey(),
      id: String(tk.id ?? "").trim(),
      subject: String(tk.subject ?? "").trim(),
      description: String(tk.description ?? ""),
      outputSummaryRequirement: String(
        tk.outputSummaryRequirement ?? tk.output_summary_requirement ?? "",
      ),
      requiresHumanCheckpoint: !!(
        tk.requiresHumanCheckpoint ?? tk.requires_human_checkpoint
      ),
      autoMerge: meta.kind === "openclaw"
        ? true
        : (tk.devAutoMerge ?? tk.dev_auto_merge) !== false,
      ownerKind: meta.kind,
      ownerId,
      ownerRepo: isNonOpenclawKind(meta.kind) ? meta.repo : "",
      ownerTargetBranch: isNonOpenclawKind(meta.kind)
        ? meta.targetBranch
        : "",
      ownerIsTemporary: meta.kind !== "openclaw" && meta.isTemporary,
      dependsOn,
      isLeaderSummary: !!(tk.isLeaderSummary ?? tk.is_leader_summary),
      timeoutSeconds: Number(
        tk.timeoutSeconds ?? tk.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS,
      ),
    };
  });
}


function specToRows(spec: FlowSpec): TaskRow[] {
  const byId = new Map<string, FlowAgent>();
  for (const a of spec.agents) byId.set(a.id, a);
  return spec.tasks.map((tk) => {
    const a = byId.get(tk.ownerAgentId);
    const ownerKind: OwnerKind = toOwnerKind(a?.kind);
    return {
      rowKey: newRowKey(),
      id: tk.id,
      subject: tk.subject,
      description: tk.description ?? "",
      outputSummaryRequirement: tk.outputSummaryRequirement ?? "",
      requiresHumanCheckpoint: !!tk.requiresHumanCheckpoint,
      autoMerge: ownerKind === "openclaw" ? true : tk.devAutoMerge !== false,
      ownerKind,
      ownerId: tk.ownerAgentId,
      ownerRepo: isNonOpenclawKind(ownerKind) ? a?.repo ?? "" : "",
      ownerTargetBranch: isNonOpenclawKind(ownerKind)
        ? (a?.targetBranch ?? "")
        : "",
      // OpenClaw is never temporary; otherwise honor the persisted flag. A
      // MISSING flag (legacy pre-0.1.13 spec) defaults to temporary — matching
      // proposalToRows — so an unregistered legacy agent renders as a text input
      // instead of a blank managed dropdown. The 0.1.13b7 upgrade migration
      // backfills explicit values, so this only matters before that runs.
      ownerIsTemporary: ownerKind !== "openclaw" && (a?.isTemporary ?? true),
      dependsOn: tk.dependsOn ?? [],
      isLeaderSummary: !!tk.isLeaderSummary,
      timeoutSeconds: tk.timeoutSeconds ?? DEFAULT_TIMEOUT_SECONDS,
    };
  });
}


// ──────────────────────────────────────────────────────────────────────
// Merge-existing-flows modal
// ──────────────────────────────────────────────────────────────────────


interface ConflictHit {
  flowName: string;
  taskSubject: string;
  taskId: string;
  agentId: string;
}

/**
 * Multi-select picker over the user's existing Flows. On confirm:
 *   1. Fetch each selected Flow's full detail.
 *   2. Refuse if any non-summary task in the source set is owned by an
 *      OpenClaw agent whose id equals the current leader (the source
 *      flow would violate the "leader-only-owns-summary" invariant once
 *      imported under this leader).
 *   3. Hand the validated details to the parent for the actual merge.
 */
function MergeFlowsModal({
  currentFlowId,
  leaderId,
  onCancel,
  onMerge,
}: {
  /** Flow currently being edited — excluded from the candidate list to
   *  prevent "merge a flow into itself". ``null`` while creating a new
   *  flow (in that case every existing flow is a candidate). */
  currentFlowId: string | null;
  leaderId: string;
  onCancel: () => void;
  onMerge: (details: FlowDetail[]) => void;
}) {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const [items, setItems] = useState<FlowSummary[] | null>(null);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [loadError, setLoadError] = useState<string | null>(null);
  const [merging, setMerging] = useState(false);
  const [mergeError, setMergeError] = useState<string | null>(null);

  useEffect(() => {
    api
      .listFlows()
      .then((r) => setItems(r.items))
      .catch((e) => {
        setLoadError(e instanceof ApiError ? e.message : String(e));
      });
  }, []);

  function toggle(id: string) {
    setSelected((cur) => {
      const next = new Set(cur);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  async function onMergeClick() {
    if (selected.size === 0) {
      setMergeError(t("flowEditor.mergeModal.noSelection"));
      return;
    }
    setMergeError(null);
    setMerging(true);
    try {
      // Fetch all selected flow details in parallel; preserve user
      // selection order via the items list (newest-first per backend).
      const ids = Array.from(selected);
      const settled = await Promise.allSettled(ids.map((id) => api.getFlow(id)));
      const details: FlowDetail[] = [];
      const failed: { id: string; name: string }[] = [];
      for (let i = 0; i < ids.length; i += 1) {
        const result = settled[i];
        if (result.status === "fulfilled") {
          details.push(result.value);
        } else {
          const meta = (items ?? []).find((f) => f.id === ids[i]);
          failed.push({ id: ids[i], name: meta?.name ?? ids[i] });
        }
      }
      if (failed.length > 0) {
        setMergeError(
          t("flowEditor.mergeModal.partialFailure", {
            count: failed.length,
            names: failed.map((f) => f.name).join(", "),
          }),
        );
        return;
      }

      // Conflict check: aggregate ALL conflicts across selected flows so
      // the user can fix everything at once instead of playing whack-a-
      // mole one Flow at a time.
      const hits: ConflictHit[] = [];
      for (const flow of details) {
        const tasks = flow.spec?.tasks ?? [];
        const agents = flow.spec?.agents ?? [];
        const openclawAgentIds = new Set(
          agents
            .filter((a) => a.kind === "openclaw")
            .map((a) => a.id),
        );
        for (const tk of tasks) {
          if (tk.isLeaderSummary) continue;
          if (!openclawAgentIds.has(tk.ownerAgentId)) continue;
          if (tk.ownerAgentId === leaderId) {
            hits.push({
              flowName: flow.name,
              taskSubject: tk.subject || tk.id,
              taskId: tk.id,
              agentId: tk.ownerAgentId,
            });
          }
        }
      }
      if (hits.length > 0) {
        const lines = [t("flowEditor.mergeModal.conflictHeader")];
        for (const h of hits) {
          lines.push(
            "• " +
              t("flowEditor.mergeModal.conflictDetail", {
                flow: h.flowName,
                task: h.taskSubject,
                agent: h.agentId,
              }),
          );
        }
        void alert(lines.join("\n"));
        return;
      }

      onMerge(details);
    } finally {
      setMerging(false);
    }
  }

  const candidates = (items ?? []).filter((f) => f.id !== currentFlowId);

  return (
    <Modal
      open={true}
      onClose={() => {
        if (merging) return;
        onCancel();
      }}
      title={t("flowEditor.mergeModal.title")}
      width="max-w-3xl"
    >
      <p className="text-sm text-ink-600 mb-3">
        {t("flowEditor.mergeModal.hint")}
      </p>

      {loadError && <ErrorBox>{loadError}</ErrorBox>}
      {!items && !loadError && <Loading />}

      {items && candidates.length === 0 && (
        <div className="text-sm text-ink-500 border border-dashed border-ink-200 rounded-md px-4 py-6 text-center">
          {t("flowEditor.mergeModal.empty")}
        </div>
      )}

      {items && candidates.length > 0 && (
        <div className="border border-ink-200 rounded-md max-h-[420px] overflow-auto">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500 sticky top-0">
              <tr>
                <th className="w-10 px-3 py-2"></th>
                <th className="text-left px-3 py-2 font-medium">
                  {t("flowEditor.mergeModal.columnName")}
                </th>
                <th className="text-left px-3 py-2 font-medium">
                  {t("flowEditor.mergeModal.columnLeader")}
                </th>
                <th className="text-left px-3 py-2 font-medium">
                  {t("flowEditor.mergeModal.columnUpdated")}
                </th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((f) => {
                const checked = selected.has(f.id);
                return (
                  <tr
                    key={f.id}
                    className="border-t border-ink-100 hover:bg-ink-50 cursor-pointer"
                    onClick={() => toggle(f.id)}
                  >
                    <td className="px-3 py-2 align-middle">
                      <input
                        type="checkbox"
                        checked={checked}
                        onChange={() => toggle(f.id)}
                        onClick={(e) => e.stopPropagation()}
                      />
                    </td>
                    <td className="px-3 py-2 align-middle">
                      <div className="font-medium text-ink-900">{f.name}</div>
                      {f.description && (
                        <div className="text-xs text-ink-500 mt-0.5 line-clamp-1">
                          {f.description}
                        </div>
                      )}
                    </td>
                    <td className="px-3 py-2 align-middle">
                      {f.leaderAgentId ? (
                        <span className="font-mono text-xs text-ink-700">
                          {f.leaderAgentId}
                        </span>
                      ) : (
                        <span className="text-xs text-ink-400">—</span>
                      )}
                    </td>
                    <td className="px-3 py-2 align-middle text-xs text-ink-500">
                      {new Date(f.updatedAt).toLocaleString()}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}

      {mergeError && (
        <div className="mt-3">
          <ErrorBox>{mergeError}</ErrorBox>
        </div>
      )}

      <div className="flex justify-end gap-2 mt-4">
        <button
          type="button"
          className="btn-outline"
          onClick={onCancel}
          disabled={merging}
        >
          {t("flowEditor.mergeModal.cancel")}
        </button>
        <button
          type="button"
          className="btn-primary"
          onClick={onMergeClick}
          disabled={merging || !items || selected.size === 0}
        >
          {merging
            ? t("flowEditor.mergeModal.merging")
            : t("flowEditor.mergeModal.merge")}
        </button>
      </div>
    </Modal>
  );
}
