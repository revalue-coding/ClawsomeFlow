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
  ManagedAgentSummary,
  OpenclawAgentSummary,
  api,
} from "@/lib/api";
import { Card, CardTitle, ErrorBox, Loading, Modal, StatusPill } from "@/components/ui";
import { ChatIcon } from "@/components/icons";
import { cn } from "@/lib/cn";
import {
  DEFAULT_TARGET_BRANCH,
  getRunInputFields,
  setRunInputFields,
} from "@/lib/flowRuntime";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";

type NonOpenclawOwnerKind = "claude" | "codex" | "cursor" | "hermes";
type OwnerKind = "openclaw" | NonOpenclawOwnerKind;
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
  ownerKind: OwnerKind;
  ownerId: string;
  ownerRepo: string;
  ownerTargetBranch: string;
  dependsOn: string[];
  isLeaderSummary: boolean;
  timeoutSeconds: number;
}

interface ValidationMessages {
  pickOneSummary: string;
  onlyOneSummary: string;
  taskIdRequired: string;
  taskIdPattern: string;
  duplicateTaskId: (taskId: string) => string;
  subjectRequired: string;
  pickOpenclawAgent: string;
  ownerAgentNameRequired: string;
  ownerAgentIdPattern: string;
  claudeRepoRequired: string;
  claudeTargetBranchRequired: string;
  leaderCannotOwnNonSummary: string;
  summaryNeedsDependency: string;
  ownerRepoBranchMismatch: (agentId: string) => string;
  cycleDetected: (cyclePath: string) => string;
  /** Per-task: description (instruction body) must be non-empty so the
   *  worker dispatch prompt actually contains an instruction. */
  descriptionRequired: (subject: string) => string;
  /** Per-task: the OpenClaw agent picked as owner no longer exists in
   *  the user's agent list (was deleted in another tab / pulled flow
   *  from a different user, etc). Compile would fail with
   *  ``OPENCLAW_AGENT_NOT_FOUND`` — catch it client-side first. */
  openclawAgentMissing: (subject: string, agentId: string) => string;
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

function blankRow(): TaskRow {
  return {
    rowKey: newRowKey(),
    id: newTaskId(),
    subject: "",
    description: "",
    outputSummaryRequirement: "",
    requiresHumanCheckpoint: false,
    ownerKind: "claude",
    ownerId: "",
    ownerRepo: "",
    ownerTargetBranch: DEFAULT_TARGET_BRANCH,
    dependsOn: [],
    isLeaderSummary: false,
    timeoutSeconds: DEFAULT_TIMEOUT_SECONDS,
  };
}

/** Detect whether the SPA is served to a non-loopback host. The native
 *  directory picker only works when backend and browser run on the same
 *  machine, so we surface a friendlier hint for remote access. */
function isRemoteBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
}

function isOpenclawKind(kind: OwnerKind): kind is "openclaw" {
  return kind === "openclaw";
}

function isNonOpenclawKind(kind: OwnerKind): kind is NonOpenclawOwnerKind {
  return kind !== "openclaw";
}

// Kinds whose agent id must be picked from a managed-agent dropdown (not free
// text). Unlike OpenClaw, these KEEP their repo/branch (working dir is per-task).
const MANAGED_PICK_KINDS = new Set<OwnerKind>(["hermes", "claude", "codex"]);

function isManagedPickKind(kind: OwnerKind): boolean {
  return MANAGED_PICK_KINDS.has(kind);
}

/** Combined managed-agent picklist for a kind (Hermes + Claude/Codex). */
function pickAgentsForKind(
  kind: OwnerKind,
  hermes: HermesAgentSummary[],
  managed: ManagedAgentSummary[],
): { id: string; name: string }[] {
  if (kind === "hermes") return hermes.map((a) => ({ id: a.id, name: a.name }));
  if (kind === "claude" || kind === "codex") {
    return managed.filter((a) => a.kind === kind).map((a) => ({ id: a.id, name: a.name }));
  }
  return [];
}

function ownerKey(
  row: Pick<TaskRow, "ownerKind" | "ownerId" | "ownerRepo" | "ownerTargetBranch">,
) {
  if (isOpenclawKind(row.ownerKind)) return `openclaw:${row.ownerId.trim()}`;
  return `${row.ownerKind}:${row.ownerRepo.trim()}:${row.ownerTargetBranch.trim()}:${row.ownerId.trim()}`;
}

function ownerKindLabel(
  kind: OwnerKind,
  t: (key: string) => string,
): string {
  if (kind === "openclaw") return t("flowEditor.taskFields.ownerKindOpenclaw");
  if (kind === "codex") return t("flowEditor.taskFields.ownerKindCodex");
  if (kind === "cursor") return t("flowEditor.taskFields.ownerKindCursor");
  if (kind === "hermes") return t("flowEditor.taskFields.ownerKindHermes");
  return t("flowEditor.taskFields.ownerKindClaude");
}

function buildExistingOwnerOptions(
  rows: TaskRow[],
  openclawOptions: OpenclawAgentSummary[],
  opts: {
    leaderId: string;
    leaderKind: OwnerKind;
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
  const normalizedLeaderRepo = leaderRepo.trim();
  const normalizedLeaderTargetBranch =
    leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH;
  const leaderKey = normalizedLeaderId
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
    const repo = isNonOpenclawKind(kind) ? r.ownerRepo.trim() : "";
    const targetBranch = isNonOpenclawKind(kind)
      ? (r.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
      : "";
    const key = isOpenclawKind(kind)
      ? `openclaw:${id}`
      : `${kind}:${id}:${repo}:${targetBranch}`;
    if (!isSummary && leaderKey && key === leaderKey) continue;
    add({
      key,
      id,
      kind,
      repo,
      targetBranch,
      label: isOpenclawKind(kind)
        ? `${ownerKindLabel(kind, t)} · ${id}`
        : `${ownerKindLabel(kind, t)} · ${id} (${repo || "—"} @ ${targetBranch || DEFAULT_TARGET_BRANCH})`,
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
  const isNew = !id || id === "new";
  const navigate = useNavigate();

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [leaderId, setLeaderId] = useState("");
  const [leaderKind, setLeaderKind] = useState<OwnerKind>("openclaw");
  const [leaderRepo, setLeaderRepo] = useState("");
  const [leaderTargetBranch, setLeaderTargetBranch] = useState(DEFAULT_TARGET_BRANCH);
  const [leaderBranchOptions, setLeaderBranchOptions] = useState<string[]>([
    DEFAULT_TARGET_BRANCH,
  ]);
  const [leaderBranchEditable, setLeaderBranchEditable] = useState(false);
  const [leaderBranchLoading, setLeaderBranchLoading] = useState(false);
  const [leaderPickingRepo, setLeaderPickingRepo] = useState(false);
  const [runInputFields, setRunInputFieldsState] = useState<string[]>([]);
  const [runInputFieldDraft, setRunInputFieldDraft] = useState("");
  const [runInputFieldError, setRunInputFieldError] = useState<string | null>(null);
  const [version, setVersion] = useState<number | null>(null);
  const [tasks, setTasks] = useState<TaskRow[]>([]);
  const [openclawOptions, setOpenclawOptions] = useState<OpenclawAgentSummary[]>([]);
  const [hermesOptions, setHermesOptions] = useState<HermesAgentSummary[]>([]);
  const [managedOptions, setManagedOptions] = useState<ManagedAgentSummary[]>([]);
  const [deploymentMode, setDeploymentMode] = useState<DeploymentMode>("local");
  const [workspaceDirOptions, setWorkspaceDirOptions] = useState<string[]>([]);
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
  const remoteBrowser = isRemoteBrowser();

  // Load option lists.
  useEffect(() => {
    api
      .listOpenclawAgents()
      .then((r) => setOpenclawOptions(r.items))
      .catch(() => {});
    api
      .listHermesAgents()
      .then((r) => setHermesOptions(r.items))
      .catch(() => {});
    api
      .listManagedAgents()
      .then((r) => setManagedOptions(r.items))
      .catch(() => {});
    api
      .listWorkspaceDirectories()
      .then((r) => {
        setDeploymentMode(r.deploymentMode);
        setWorkspaceDirOptions(r.items);
      })
      .catch(() => {});
  }, []);

  // Load existing flow when editing.
  useEffect(() => {
    if (isNew) return;
    api
      .getFlow(id!)
      .then((flow) => {
        setName(flow.name);
        setDescription(flow.description);
        setRunInputFieldsState(getRunInputFields(flow.spec));
        setVersion(flow.version);
        const rows = specToRows(flow.spec);
        setTasks(rows);
        const summary = rows.find((r) => r.isLeaderSummary);
        if (summary) {
          setLeaderId(summary.ownerId);
          setLeaderKind(summary.ownerKind);
          setLeaderRepo(summary.ownerRepo);
          setLeaderTargetBranch(
            summary.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH,
          );
        }
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
      });
  }, [id, isNew]);

  // Keep the summary task in sync with leader fields.
  // Important: when leader fields are in an intermediate state (for example
  // switching kind and leaderId is temporarily empty), preserve the existing
  // summary row instead of deleting/recreating it.
  useEffect(() => {
    const normalizedLeaderId = leaderId.trim();
    const normalizedLeaderRepo = isNonOpenclawKind(leaderKind)
      ? leaderRepo.trim()
      : "";
    const normalizedLeaderTargetBranch = isNonOpenclawKind(leaderKind)
      ? (leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
      : "";
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
          requiresHumanCheckpoint: false,
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
        // New summary rows start with empty dependencies and require explicit
        // user selection of upstream tasks.
        dependsOn: [],
        isLeaderSummary: true,
        requiresHumanCheckpoint: false,
      };
      return [...rows, summary];
    });
  }, [leaderId, leaderKind, leaderRepo, leaderTargetBranch, t]);

  useEffect(() => {
    if (leaderKind === "openclaw") {
      setLeaderBranchOptions([]);
      setLeaderBranchEditable(false);
      return;
    }
    const repo = leaderRepo.trim();
    if (!repo) {
      setLeaderBranchOptions([DEFAULT_TARGET_BRANCH]);
      setLeaderBranchEditable(false);
      setLeaderTargetBranch((prev) =>
        prev.trim() === DEFAULT_TARGET_BRANCH ? prev : DEFAULT_TARGET_BRANCH,
      );
      return;
    }
    let cancelled = false;
    setLeaderBranchLoading(true);
    void api
      .listRepoBranches({ path: repo })
      .then((meta) => {
        if (cancelled) return;
        const branches = meta.branches.length > 0
          ? meta.branches
          : [DEFAULT_TARGET_BRANCH];
        const fallback =
          meta.currentBranch?.trim() || branches[0] || DEFAULT_TARGET_BRANCH;
        setLeaderBranchOptions(branches);
        setLeaderBranchEditable(meta.editable);
        setLeaderTargetBranch((prev) => {
          const current = prev.trim();
          const next = current && branches.includes(current)
            ? current
            : fallback;
          return current === next ? prev : next;
        });
      })
      .catch((e) => {
        void e;
        if (cancelled) return;
        setLeaderBranchOptions([DEFAULT_TARGET_BRANCH]);
        setLeaderBranchEditable(false);
        setLeaderTargetBranch((prev) =>
          prev.trim() === DEFAULT_TARGET_BRANCH ? prev : DEFAULT_TARGET_BRANCH,
        );
      })
      .finally(() => {
        if (!cancelled) setLeaderBranchLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [leaderKind, leaderRepo]);

  // ── derived state -------------------------------------------------

  const summaryTask = tasks.find((r) => r.isLeaderSummary);
  const leaderKey = summaryTask ? ownerKey(summaryTask) : null;

  const validationMessages = useMemo<ValidationMessages>(
    () => ({
      pickOneSummary: t("flowEditor.validation.pickOneSummary"),
      onlyOneSummary: t("flowEditor.validation.onlyOneSummary"),
      taskIdRequired: t("flowEditor.validation.taskIdRequired"),
      taskIdPattern: t("flowEditor.validation.taskIdPattern"),
      duplicateTaskId: (taskId: string) =>
        t("flowEditor.validation.duplicateTaskId", { taskId }),
      subjectRequired: t("flowEditor.validation.subjectRequired"),
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
      cycleDetected: (cyclePath: string) =>
        t("flowEditor.validation.cycleDetected", { cyclePath }),
      descriptionRequired: (subject: string) =>
        t("flowEditor.validation.descriptionRequired", { subject }),
      openclawAgentMissing: (subject: string, agentId: string) =>
        t("flowEditor.validation.openclawAgentMissing", { subject, agentId }),
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
  const issues = useMemo(
    () => validate(tasks, validationMessages, openclawIds),
    [tasks, validationMessages, openclawIds],
  );
  // Auto-dismiss the save-blockers rail as soon as the user starts
  // fixing things — otherwise a stale list lingers until the next
  // Save click and feels broken.
  useEffect(() => {
    setSaveBlockers(null);
  }, [name, description, leaderId, leaderKind, leaderRepo, leaderTargetBranch, tasks]);
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
    if (!leaderId.trim()) {
      return t("flowEditor.validation.pickLeader");
    }
    if (leaderKind === "openclaw") {
      if (openclawOptions.length === 0) {
        return t("flowEditor.decompose.leaderEmpty");
      }
      if (!openclawOptions.some((a) => a.id === leaderId.trim())) {
        return t("flowEditor.validation.pickLeader");
      }
      return null;
    }
    if (isManagedPickKind(leaderKind)) {
      // Managed agent required (no ad-hoc creation), plus a working dir.
      const opts = pickAgentsForKind(leaderKind, hermesOptions, managedOptions);
      if (!opts.some((a) => a.id === leaderId.trim())) {
        return t("flowEditor.validation.pickLeader");
      }
    }
    if (!leaderRepo.trim()) {
      return t("flowEditor.decompose.leaderRepoRequired");
    }
    return null;
  }, [leaderId, leaderKind, leaderRepo, openclawOptions, hermesOptions, managedOptions, t]);

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
      window.alert(t("flowEditor.pickLeaderFirst"));
      return;
    }
    setEditing({ mode: "create", draft: blankRow() });
  }

  function openMerge() {
    if (!leaderId.trim()) {
      window.alert(t("flowEditor.mergePickLeaderFirst"));
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
      window.alert(
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
        hermesOptions.find((a) => a.id === normalized) ??
        managedOptions.find((a) => a.id === normalized);
      window.alert(
        t("flowEditor.leaderInUseByTask", {
          name: agent ? `${agent.name} (${agent.id})` : normalized,
        }),
      );
      return; // keep leaderId untouched — select snaps back via controlled value
    }
    if (isManagedPickKind(leaderKind)) {
      // Managed kinds (Hermes/Claude/Codex) bind identity via the managed id,
      // but the working directory (repo/branch) is chosen separately — preserve it.
      setLeaderId(normalized);
      return;
    }
    setLeaderKind("openclaw");
    setLeaderRepo("");
    setLeaderTargetBranch(DEFAULT_TARGET_BRANCH);
    setLeaderId(normalized);
  }

  /** Commit a draft task from the create-mode modal, inserting it BEFORE
   *  the summary task so the summary stays pinned at the end. */
  function commitNewTask(row: TaskRow) {
    setTasks((rows) => {
      const summaryIdx = rows.findIndex((r) => r.isLeaderSummary);
      if (summaryIdx === -1) return [...rows, row];
      return [...rows.slice(0, summaryIdx), row, ...rows.slice(summaryIdx)];
    });
  }

  /** Apply an entire row replacement from edit-mode modal save. */
  function applyEditedRow(rowKey: string, replacement: TaskRow) {
    setTasks((rows) => {
      const prev = rows.find((r) => r.rowKey === rowKey);
      const prevId = prev?.id.trim() || "";
      const nextId = replacement.id.trim();
      const renamed = prevId.length > 0 && nextId.length > 0 && prevId !== nextId;
      const changedExistingNonOpenclaw = Boolean(
        prev &&
        isNonOpenclawKind(prev.ownerKind) &&
        prev.ownerId.trim() &&
        prev.ownerId.trim() === replacement.ownerId.trim() &&
        prev.ownerKind === replacement.ownerKind &&
        (
          prev.ownerRepo.trim() !== replacement.ownerRepo.trim()
          || (prev.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
            !== (replacement.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
        ),
      );
      return rows.map((r) => {
        let nextRow = r;
        if (r.rowKey === rowKey) {
          return { ...replacement, rowKey };
        }
        if (
          changedExistingNonOpenclaw
          && r.ownerKind === replacement.ownerKind
          && r.ownerId.trim() === replacement.ownerId.trim()
        ) {
          nextRow = {
            ...nextRow,
            ownerRepo: replacement.ownerRepo,
            ownerTargetBranch: replacement.ownerTargetBranch,
          };
        }
        if (!renamed) return nextRow;
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
      spec: rowsToSpec(tasks, runInputFields),
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
    if (typeof window !== "undefined") {
      window.alert(text);
      return;
    }
    setError(text);
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

  async function onSubmit() {
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
    try {
      const payload = buildSavePayload();
      const result = await persistFlow(payload);
      notifySaveWarnings(result.warnings);
      // Saving always returns the user to the Flow list (per UX spec).
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

  async function ensureLeaderRepoReadyForDecompose(): Promise<boolean> {
    if (!isNonOpenclawKind(leaderKind)) return true;
    const repo = leaderRepo.trim();
    if (!repo) {
      setError(t("flowEditor.decompose.leaderRepoRequired"));
      return false;
    }
    const leaderLabel = leaderId.trim() || t("flowEditor.repoIssue.unknownAgent");
    const errText = (e: unknown) =>
      e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);

    let checked;
    try {
      checked = await api.ensureGitRepo({
        path: repo,
        createDirIfMissing: false,
        initializeIfMissing: false,
      });
    } catch (e) {
      setError(t("flowEditor.taskRepoCheck.checkFailed", { message: errText(e) }));
      return false;
    }

    const resolvedPath = checked.path || repo;
    if (checked.pathExists && checked.isGitRepo && checked.hasInitialCommit) {
      if (resolvedPath !== leaderRepo) {
        setLeaderRepo(resolvedPath);
      }
      return true;
    }

    const reason = !checked.pathExists
      ? t("flowEditor.repoIssue.reasonPathMissing")
      : !checked.isGitRepo
      ? t("flowEditor.repoIssue.reasonNotGitRepo")
      : !checked.hasInitialCommit
      ? t("flowEditor.repoIssue.reasonNoInitialCommit")
      : t("flowEditor.repoIssue.reasonUnknown");
    const shouldCreate = window.confirm(
      t("flowEditor.taskRepoCheck.confirmCreate", {
        agentId: leaderLabel,
        repo: resolvedPath,
        reason,
      }),
    );
    if (!shouldCreate) {
      setError(t("flowEditor.taskRepoCheck.reselectHint"));
      return false;
    }

    try {
      const ensured = await api.ensureGitRepo({
        path: resolvedPath,
        createDirIfMissing: true,
        initializeIfMissing: true,
        createInitialCommitIfMissing: true,
      });
      if (!ensured.isGitRepo || !ensured.hasInitialCommit) {
        setError(t("flowEditor.taskRepoCheck.stillInvalid"));
        return false;
      }
      const normalizedPath = ensured.path || resolvedPath;
      let nextLeaderTargetBranch = leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH;
      if (ensured.initializedRepo || ensured.createdInitialCommit) {
        const fromEnsure = ensured.currentBranch?.trim();
        if (fromEnsure) {
          nextLeaderTargetBranch = fromEnsure;
        } else {
          try {
            const meta = await api.listRepoBranches({ path: normalizedPath });
            nextLeaderTargetBranch =
              meta.currentBranch?.trim()
              || meta.branches[0]
              || DEFAULT_TARGET_BRANCH;
          } catch {
            nextLeaderTargetBranch = DEFAULT_TARGET_BRANCH;
          }
        }
      }
      if (normalizedPath !== leaderRepo) {
        setLeaderRepo(normalizedPath);
      }
      if (nextLeaderTargetBranch !== leaderTargetBranch) {
        setLeaderTargetBranch(nextLeaderTargetBranch);
      }
      return true;
    } catch (e) {
      setError(t("flowEditor.taskRepoCheck.createFailed", { message: errText(e) }));
      return false;
    }
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
        isManagedPickKind(leaderKind) &&
        !pickAgentsForKind(leaderKind, hermesOptions, managedOptions).some(
          (a) => a.id === leaderId.trim(),
        )
      ) {
        setError(t("flowEditor.validation.pickLeader"));
        return;
      }
      const repoReady = await ensureLeaderRepoReadyForDecompose();
      if (!repoReady) return;
    }
    setError(null);
    setDecomposeOpen(true);
  }

  async function onPickLeaderRepo() {
    if (remoteBrowser) {
      window.alert(t("flowEditor.pickDirRemoteHint"));
      return;
    }
    setLeaderPickingRepo(true);
    try {
      const out = await api.pickDirectory({
        title: t("flowEditor.taskFields.pickDirTitle"),
        initialPath: leaderRepo || undefined,
      });
      if (out.path) setLeaderRepo(out.path);
    } catch (e) {
      const msg =
        e instanceof ApiError
          ? e.message
          : e instanceof Error
          ? e.message
          : String(e);
      window.alert(t("flowEditor.pickDirFailed", { message: msg }));
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
            onClick={() => navigate("/flows")}
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
                <ChatIcon className="h-4 w-4 text-brand-500" />
                {t("flowEditor.aiDecompose")}
              </button>
            </div>
            <div className="mt-2 grid gap-2 md:grid-cols-2">
              <div>
                <label className="label">{t("flowEditor.leaderKindLabel")}</label>
                <select
                  className="select"
                  value={leaderKind}
                  onChange={(e) => {
                    const nextKind = e.target.value as OwnerKind;
                    const toggledOpenclaw =
                      (leaderKind === "openclaw") !== (nextKind === "openclaw");
                    if (toggledOpenclaw) {
                      setLeaderId("");
                    }
                    setLeaderKind(nextKind);
                    if (nextKind === "openclaw") {
                      setLeaderRepo("");
                      setLeaderTargetBranch(DEFAULT_TARGET_BRANCH);
                    }
                  }}
                >
                  <option value="openclaw">
                    {t("flowEditor.taskFields.ownerKindOpenclaw")}
                  </option>
                  <option value="claude">
                    {t("flowEditor.taskFields.ownerKindClaude")}
                  </option>
                  <option value="codex">
                    {t("flowEditor.taskFields.ownerKindCodex")}
                  </option>
                  <option value="cursor">
                    {t("flowEditor.taskFields.ownerKindCursor")}
                  </option>
                  <option value="hermes">
                    {t("flowEditor.taskFields.ownerKindHermes")}
                  </option>
                </select>
              </div>
              <div>
                <label className="label">{t("flowEditor.leaderAgentLabel")}</label>
                {leaderKind === "openclaw" ? (
                  <select
                    className="select"
                    value={leaderId}
                    onChange={(e) => tryChangeLeader(e.target.value)}
                  >
                    <option value="">{t("flowEditor.leaderPlaceholder")}</option>
                    {openclawOptions.map((a) => (
                      <option key={a.id} value={a.id}>
                        {a.name} ({a.id})
                      </option>
                    ))}
                  </select>
                ) : isManagedPickKind(leaderKind) ? (
                  <>
                    <select
                      className="select"
                      value={leaderId}
                      onChange={(e) => tryChangeLeader(e.target.value)}
                    >
                      <option value="">{t("flowEditor.hermesAgentPlaceholder")}</option>
                      {pickAgentsForKind(leaderKind, hermesOptions, managedOptions).map((a) => (
                        <option key={a.id} value={a.id}>
                          {a.name} ({a.id})
                        </option>
                      ))}
                    </select>
                    {pickAgentsForKind(leaderKind, hermesOptions, managedOptions).length === 0 && (
                      <div className="text-xs text-ink-500 mt-1">
                        {t("flowEditor.hermesAgentEmpty")}
                      </div>
                    )}
                  </>
                ) : (
                  <input
                    className="input"
                    value={leaderId}
                    placeholder={t("flowEditor.taskFields.newAgentNamePlaceholder")}
                    onChange={(e) => setLeaderId(e.target.value)}
                  />
                )}
              </div>
            </div>
            {leaderKind !== "openclaw" && (
              <div className="mt-2 grid gap-2 md:grid-cols-[minmax(0,1.15fr)_minmax(0,1fr)]">
                <div>
                  <label className="label">{t("flowEditor.leaderRepoLabel")}</label>
                  {deploymentMode === "server" ? (
                    <>
                      <select
                        className="select"
                        value={leaderRepo}
                        onChange={(e) => setLeaderRepo(e.target.value)}
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
                        onChange={(e) => setLeaderRepo(e.target.value)}
                        onKeyDown={(e) => {
                          if (e.key === "Enter") {
                            e.preventDefault();
                          }
                        }}
                      />
                      <button
                        type="button"
                        className="btn-outline whitespace-nowrap"
                        onClick={() => void onPickLeaderRepo()}
                        disabled={leaderPickingRepo}
                      >
                        {leaderPickingRepo
                          ? t("flowEditor.taskFields.pickingDir")
                          : t("flowEditor.taskFields.pickDirButton")}
                      </button>
                    </div>
                  )}
                </div>
                <div>
                  <label className="label">{t("flowEditor.leaderTargetBranchLabel")}</label>
                  <select
                    className="select"
                    value={leaderTargetBranch || DEFAULT_TARGET_BRANCH}
                    disabled={!leaderBranchEditable || leaderBranchLoading}
                    onChange={(e) => setLeaderTargetBranch(e.target.value)}
                  >
                    {(leaderBranchOptions.length > 0
                      ? leaderBranchOptions
                      : [DEFAULT_TARGET_BRANCH]).map((name) => (
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
                    onRemove={() => removeRow(row.rowKey)}
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
                    onMove={(dir) => moveRow(row.rowKey, dir)}
                  />
                );
              })}
            </div>
            <DependencyGraph tasks={tasks} />
          </div>
        )}
      </Card>

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
          openclawOptions={openclawOptions}
          hermesOptions={hermesOptions}
          managedOptions={managedOptions}
          leaderKind={leaderKind}
          leaderId={leaderId.trim()}
          leaderRepo={leaderRepo.trim()}
          leaderTargetBranch={leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH}
          deploymentMode={deploymentMode}
          workspaceDirOptions={workspaceDirOptions}
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
        leaderTargetBranch={leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH}
        existingRows={tasks}
        openclawAgents={openclawOptions}
        onClose={() => setDecomposeOpen(false)}
        onApply={(proposal) => {
          const rows = proposalToRows(proposal, openclawOptions);
          const idx = rows.findIndex((r) => r.isLeaderSummary);
          if (idx >= 0 && leaderId.trim()) {
            const normalizedLeaderRepo = isNonOpenclawKind(leaderKind)
              ? leaderRepo.trim()
              : "";
            const normalizedLeaderTargetBranch = isNonOpenclawKind(leaderKind)
              ? (leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
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
                <span className="font-mono">{row.ownerId || "—"}</span>
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
  openclawOptions,
  hermesOptions,
  managedOptions,
  leaderKind,
  leaderId,
  leaderRepo,
  leaderTargetBranch,
  deploymentMode,
  workspaceDirOptions,
  onSave,
  onCancel,
}: {
  mode: "create" | "edit" | "view";
  initialRow: TaskRow;
  tasks: TaskRow[];
  openclawOptions: OpenclawAgentSummary[];
  hermesOptions: HermesAgentSummary[];
  managedOptions: ManagedAgentSummary[];
  leaderKind: OwnerKind;
  /** Currently-selected leader id. Excluded from sub-task agent picker
   *  (leader can only own the auto-summary task). */
  leaderId: string;
  leaderRepo: string;
  leaderTargetBranch: string;
  deploymentMode: DeploymentMode;
  workspaceDirOptions: string[];
  onSave: (row: TaskRow) => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<TaskRow>(initialRow);
  const [saveError, setSaveError] = useState<string | null>(null);
  const [repoChecking, setRepoChecking] = useState(false);
  const [branchOptions, setBranchOptions] = useState<string[]>([
    initialRow.ownerTargetBranch?.trim() || DEFAULT_TARGET_BRANCH,
  ]);
  const [branchEditable, setBranchEditable] = useState(
    isNonOpenclawKind(initialRow.ownerKind),
  );
  const [branchLoading, setBranchLoading] = useState(false);
  const readOnly = mode === "view";
  const isSummary = draft.isLeaderSummary;
  const existingOwnerOptions = useMemo(
    () =>
      buildExistingOwnerOptions(
        tasks,
        openclawOptions,
        {
          leaderKind,
          leaderId,
          leaderRepo,
          leaderTargetBranch,
          isSummary,
          t: (k: string) => t(k),
        },
      ),
    [
      tasks,
      openclawOptions,
      leaderKind,
      leaderId,
      leaderRepo,
      leaderTargetBranch,
      isSummary,
      t,
    ],
  );
  const [ownerMode, setOwnerMode] = useState<OwnerMode>(() => {
    if (initialRow.ownerKind === "openclaw") return "existing";
    const hit = existingOwnerOptions.find((opt) =>
      opt.id === initialRow.ownerId.trim()
      && opt.kind === initialRow.ownerKind
      && (
        opt.kind === "openclaw"
        || (
          opt.repo === initialRow.ownerRepo.trim()
          && (opt.targetBranch || DEFAULT_TARGET_BRANCH) === (initialRow.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
        )
      )
    );
    return hit ? "existing" : "new";
  });
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
    setDraft((d) => ({ ...d, ...p }));
  }

  useEffect(() => {
    if (isOpenclawKind(draft.ownerKind)) {
      setBranchOptions([]);
      setBranchEditable(false);
      return;
    }
    const repo = draft.ownerRepo.trim();
    if (!repo) {
      setBranchOptions([DEFAULT_TARGET_BRANCH]);
      setBranchEditable(false);
      setDraft((prev) =>
        prev.ownerTargetBranch.trim() === DEFAULT_TARGET_BRANCH
          ? prev
          : { ...prev, ownerTargetBranch: DEFAULT_TARGET_BRANCH },
      );
      return;
    }
    let cancelled = false;
    const errText = (e: unknown) =>
      e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
    setBranchLoading(true);
    void api
      .listRepoBranches({ path: repo })
      .then((meta) => {
        if (cancelled) return;
        const branches = meta.branches.length > 0 ? meta.branches : [DEFAULT_TARGET_BRANCH];
        const fallback = meta.currentBranch?.trim() || branches[0] || DEFAULT_TARGET_BRANCH;
        setBranchOptions(branches);
        setBranchEditable(meta.editable);
        setDraft((prev) => {
          const current = prev.ownerTargetBranch.trim();
          const next = current && branches.includes(current) ? current : fallback;
          return current === next
            ? prev
            : { ...prev, ownerTargetBranch: next };
        });
      })
      .catch((e) => {
        if (cancelled) return;
        setBranchOptions([DEFAULT_TARGET_BRANCH]);
        setBranchEditable(false);
        setDraft((prev) =>
          prev.ownerTargetBranch.trim() === DEFAULT_TARGET_BRANCH
            ? prev
            : { ...prev, ownerTargetBranch: DEFAULT_TARGET_BRANCH },
        );
        setSaveError(t("flowEditor.taskBranchCheck.fetchFailed", { message: errText(e) }));
      })
      .finally(() => {
        if (!cancelled) setBranchLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [draft.ownerKind, draft.ownerRepo, t]);

  async function ensureNonOpenclawRepoReady(
    row: TaskRow,
  ): Promise<TaskRow | null> {
    if (isOpenclawKind(row.ownerKind)) return row;
    const repo = row.ownerRepo.trim();
    if (!repo) return row;
    const targetBranch = row.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH;

    const agentLabel = row.ownerId.trim() || t("flowEditor.repoIssue.unknownAgent");
    const errText = (e: unknown) =>
      e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);

    let checked;
    try {
      checked = await api.ensureGitRepo({
        path: repo,
        createDirIfMissing: false,
        initializeIfMissing: false,
      });
    } catch (e) {
      setSaveError(t("flowEditor.taskRepoCheck.checkFailed", { message: errText(e) }));
      return null;
    }

    const resolvedPath = checked.path || repo;
    if (checked.pathExists && checked.isGitRepo && checked.hasInitialCommit) {
      if (
        resolvedPath === row.ownerRepo
        && targetBranch === row.ownerTargetBranch
      ) {
        return row;
      }
      return {
        ...row,
        ownerRepo: resolvedPath,
        ownerTargetBranch: targetBranch,
      };
    }
    const reason = !checked.pathExists
      ? t("flowEditor.repoIssue.reasonPathMissing")
      : !checked.isGitRepo
      ? t("flowEditor.repoIssue.reasonNotGitRepo")
      : !checked.hasInitialCommit
      ? t("flowEditor.repoIssue.reasonNoInitialCommit")
      : t("flowEditor.repoIssue.reasonUnknown");
    const shouldCreate = window.confirm(
      t("flowEditor.taskRepoCheck.confirmCreate", {
        agentId: agentLabel,
        repo: resolvedPath,
        reason,
      }),
    );
    if (!shouldCreate) {
      setSaveError(t("flowEditor.taskRepoCheck.reselectHint"));
      return null;
    }
    try {
      const ensured = await api.ensureGitRepo({
        path: resolvedPath,
        createDirIfMissing: true,
        initializeIfMissing: true,
        createInitialCommitIfMissing: true,
      });
      if (!ensured.isGitRepo || !ensured.hasInitialCommit) {
        setSaveError(t("flowEditor.taskRepoCheck.stillInvalid"));
        return null;
      }
      const normalizedPath = ensured.path || resolvedPath;
      let ownerTargetBranch = targetBranch;
      if (ensured.initializedRepo || ensured.createdInitialCommit) {
        const fromEnsure = ensured.currentBranch?.trim();
        if (fromEnsure) {
          ownerTargetBranch = fromEnsure;
        } else {
          try {
            const meta = await api.listRepoBranches({ path: normalizedPath });
            ownerTargetBranch =
              meta.currentBranch?.trim()
              || meta.branches[0]
              || DEFAULT_TARGET_BRANCH;
          } catch {
            ownerTargetBranch = DEFAULT_TARGET_BRANCH;
          }
        }
      }
      if (
        normalizedPath === row.ownerRepo
        && ownerTargetBranch === row.ownerTargetBranch
      ) {
        return row;
      }
      return {
        ...row,
        ownerRepo: normalizedPath,
        ownerTargetBranch,
      };
    } catch (e) {
      setSaveError(t("flowEditor.taskRepoCheck.createFailed", { message: errText(e) }));
      return null;
    }
  }

  async function attemptSave() {
    if (repoChecking) return;
    if (!draft.subject.trim()) {
      setSaveError(t("flowEditor.taskFieldRequired"));
      return;
    }
    if (!draft.ownerId.trim()) {
      setSaveError(t("flowEditor.taskFieldRequired"));
      return;
    }
    if (!ID_PATTERN.test(draft.ownerId.trim())) {
      setSaveError(t("flowEditor.validation.ownerAgentIdPattern"));
      return;
    }
    if (isNonOpenclawKind(draft.ownerKind) && !draft.ownerRepo.trim()) {
      setSaveError(t("flowEditor.taskFieldRequired"));
      return;
    }
    if (isNonOpenclawKind(draft.ownerKind) && !draft.ownerTargetBranch.trim()) {
      setSaveError(t("flowEditor.taskFieldRequired"));
      return;
    }
    if (ownerMode === "new") {
      const candidate = draft.ownerId.trim();
      if (knownAgentIds.has(candidate)) {
        setSaveError(
          t("flowEditor.validation.newAgentNameDuplicated", { agentId: candidate }),
        );
        return;
      }
    }
    if (
      ownerMode === "existing"
      && isNonOpenclawKind(draft.ownerKind)
      && (
        draft.ownerRepo.trim() !== initialRow.ownerRepo.trim()
        || (draft.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
          !== (initialRow.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
      )
    ) {
      const ok = window.confirm(
        t("flowEditor.taskRepoCheck.confirmExistingAgentRepoBranchChange"),
      );
      if (!ok) {
        setSaveError(t("flowEditor.taskRepoCheck.modifyCancelled"));
        return;
      }
    }
    setRepoChecking(true);
    try {
      const next = await ensureNonOpenclawRepoReady(
        draft,
      );
      if (!next) return;
      onSave(next);
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
        showCheckpointField={mode !== "edit"}
        tasks={tasks}
        ownerMode={ownerMode}
        existingOwnerOptions={existingOwnerOptions}
        hermesOptions={hermesOptions}
        managedOptions={managedOptions}
        deploymentMode={deploymentMode}
        workspaceDirOptions={workspaceDirOptions}
        branchOptions={branchOptions}
        branchEditable={branchEditable}
        branchLoading={branchLoading}
        onOwnerModeChange={(nextMode) => {
          setOwnerMode(nextMode);
          if (nextMode === "new") {
            patch({
              ownerKind: draft.ownerKind === "openclaw" ? "claude" : draft.ownerKind,
              ownerId: "",
              ownerRepo: "",
              ownerTargetBranch: DEFAULT_TARGET_BRANCH,
            });
            return;
          }
          const first = existingOwnerOptions[0];
          if (!first) {
            patch({
              ownerKind: "claude",
              ownerId: "",
              ownerRepo: "",
              ownerTargetBranch: DEFAULT_TARGET_BRANCH,
            });
            return;
          }
          patch({
            ownerKind: first.kind,
            ownerId: first.id,
            ownerRepo: first.repo,
            ownerTargetBranch: first.targetBranch,
          });
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
  showCheckpointField,
  tasks,
  ownerMode,
  existingOwnerOptions,
  hermesOptions,
  managedOptions,
  deploymentMode,
  workspaceDirOptions,
  branchOptions,
  branchEditable,
  branchLoading,
  onOwnerModeChange,
  onChange,
}: {
  row: TaskRow;
  readOnly: boolean;
  isSummary: boolean;
  showCheckpointField: boolean;
  tasks: TaskRow[];
  ownerMode: OwnerMode;
  existingOwnerOptions: ExistingOwnerOption[];
  hermesOptions: HermesAgentSummary[];
  managedOptions: ManagedAgentSummary[];
  deploymentMode: DeploymentMode;
  workspaceDirOptions: string[];
  branchOptions: string[];
  branchEditable: boolean;
  branchLoading: boolean;
  onOwnerModeChange: (mode: OwnerMode) => void;
  onChange: (patch: Partial<TaskRow>) => void;
}) {
  const { t } = useTranslation();
  const [pickingRepo, setPickingRepo] = useState(false);
  const ownerLocked = readOnly || isSummary;
  const selectedExistingKey = useMemo(() => {
    const hit = existingOwnerOptions.find((opt) =>
      opt.id === row.ownerId.trim()
      && opt.kind === row.ownerKind
      && (
        opt.kind === "openclaw"
        || (
          opt.repo === row.ownerRepo.trim()
          && (opt.targetBranch || DEFAULT_TARGET_BRANCH) === (row.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH)
        )
      )
    );
    return hit?.key ?? "";
  }, [
    existingOwnerOptions,
    row.ownerId,
    row.ownerKind,
    row.ownerRepo,
    row.ownerTargetBranch,
  ]);
  const ownerIsOpenclaw = isOpenclawKind(row.ownerKind);
  const ownerIsNew = ownerMode === "new";
  const ownerKindEditable = ownerIsNew && !ownerLocked;
  const branchHelperText = branchEditable
    ? t("flowEditor.taskBranchCheck.editableHint")
    : "";

  const remoteBrowser = isRemoteBrowser();

  // Dependable list = every other non-summary task. Keeping summary tasks
  // out avoids summary↔summary/self dependency cycles.
  const dependableTasks = tasks
    .filter((r) => r.rowKey !== row.rowKey && !r.isLeaderSummary && r.id.trim())
    .map((r) => r.id);

  async function onPickRepo() {
    // Remote access has no usable native dialog — surface the reason in a
    // popup before we even hit the backend. Users still have the input
    // field to paste an absolute path into.
    if (remoteBrowser) {
      window.alert(t("flowEditor.pickDirRemoteHint"));
      return;
    }
    setPickingRepo(true);
    try {
      const out = await api.pickDirectory({
        title: t("flowEditor.taskFields.pickDirTitle"),
        initialPath: row.ownerRepo || undefined,
      });
      if (out.path) onChange({ ownerRepo: out.path });
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
      window.alert(t("flowEditor.pickDirFailed", { message: msg }));
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

      {ownerMode === "existing" ? (
        <>
          <div>
            <label className="label">
              {t("flowEditor.taskFields.existingAgent")}
            </label>
            <select
              className="select"
              value={selectedExistingKey}
              disabled={ownerLocked || existingOwnerOptions.length === 0}
              onChange={(e) => {
                const picked = existingOwnerOptions.find((opt) => opt.key === e.target.value);
                if (!picked) return;
                onChange({
                  ownerKind: picked.kind,
                  ownerId: picked.id,
                  ownerRepo: picked.repo,
                  ownerTargetBranch: picked.targetBranch,
                });
              }}
            >
              <option value="">
                {t("flowEditor.taskFields.existingAgentPlaceholder")}
              </option>
              {existingOwnerOptions.map((opt) => (
                <option key={opt.key} value={opt.key}>
                  {opt.label}
                </option>
              ))}
            </select>
            {existingOwnerOptions.length === 0 && (
              <div className="text-xs text-ink-500 mt-1">
                {t("flowEditor.taskFields.existingAgentEmpty")}
              </div>
            )}
          </div>
          {!ownerIsOpenclaw && (
            <div>
              <label className="label">{t("flowEditor.taskFields.ownerKind")}</label>
              <select className="select" value={row.ownerKind} disabled={true}>
                <option value="claude">
                  {t("flowEditor.taskFields.ownerKindClaude")}
                </option>
                <option value="codex">
                  {t("flowEditor.taskFields.ownerKindCodex")}
                </option>
                <option value="cursor">
                  {t("flowEditor.taskFields.ownerKindCursor")}
                </option>
                <option value="hermes">
                  {t("flowEditor.taskFields.ownerKindHermes")}
                </option>
              </select>
            </div>
          )}
        </>
      ) : (
        <>
          <div>
            <label className="label">
              {isManagedPickKind(row.ownerKind)
                ? t("flowEditor.hermesAgentLabel")
                : t("flowEditor.taskFields.newAgentName")}
            </label>
            {isManagedPickKind(row.ownerKind) ? (
              <>
                <select
                  className="select"
                  value={row.ownerId}
                  disabled={ownerLocked}
                  onChange={(e) => onChange({ ownerId: e.target.value })}
                >
                  <option value="">{t("flowEditor.hermesAgentPlaceholder")}</option>
                  {pickAgentsForKind(row.ownerKind, hermesOptions, managedOptions).map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name} ({a.id})
                    </option>
                  ))}
                </select>
                {pickAgentsForKind(row.ownerKind, hermesOptions, managedOptions).length === 0 && (
                  <div className="text-xs text-ink-500 mt-1">
                    {t("flowEditor.hermesAgentEmpty")}
                  </div>
                )}
              </>
            ) : (
              <input
                className="input"
                placeholder={t("flowEditor.taskFields.newAgentNamePlaceholder")}
                value={row.ownerId}
                readOnly={ownerLocked}
                onChange={(e) => onChange({ ownerId: e.target.value })}
              />
            )}
          </div>
          <div>
            <label className="label">{t("flowEditor.taskFields.ownerKind")}</label>
            <select
              className="select"
              value={row.ownerKind}
              disabled={!ownerKindEditable}
              onChange={(e) => {
                const nextKind = e.target.value as OwnerKind;
                // A free-typed name and a managed-agent id aren't interchangeable;
                // clear the id when toggling in/out of a managed picker.
                const togglesManaged =
                  isManagedPickKind(row.ownerKind) !== isManagedPickKind(nextKind) ||
                  (isManagedPickKind(nextKind) && row.ownerKind !== nextKind);
                onChange({
                  ownerKind: nextKind,
                  ...(togglesManaged ? { ownerId: "" } : {}),
                  ownerRepo: "",
                  ownerTargetBranch: DEFAULT_TARGET_BRANCH,
                });
              }}
            >
              <option value="claude">
                {t("flowEditor.taskFields.ownerKindClaude")}
              </option>
              <option value="codex">
                {t("flowEditor.taskFields.ownerKindCodex")}
              </option>
              <option value="cursor">
                {t("flowEditor.taskFields.ownerKindCursor")}
              </option>
              <option value="hermes">
                {t("flowEditor.taskFields.ownerKindHermes")}
              </option>
            </select>
          </div>
        </>
      )}

      {!ownerIsOpenclaw && (
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
                    onChange={(e) => onChange({ ownerRepo: e.target.value })}
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
                    onChange={(e) => onChange({ ownerRepo: e.target.value })}
                    onKeyDown={(e) => {
                      if (e.key === "Enter") {
                        e.preventDefault();
                      }
                    }}
                  />
                  <button
                    type="button"
                    className="btn-outline whitespace-nowrap"
                    onClick={onPickRepo}
                    disabled={pickingRepo || ownerLocked}
                  >
                    {pickingRepo
                      ? t("flowEditor.taskFields.pickingDir")
                      : t("flowEditor.taskFields.pickDirButton")}
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
                value={row.ownerTargetBranch || DEFAULT_TARGET_BRANCH}
                disabled={ownerLocked || !branchEditable || branchLoading}
                onChange={(e) => onChange({ ownerTargetBranch: e.target.value })}
              >
                {(branchOptions.length > 0 ? branchOptions : [DEFAULT_TARGET_BRANCH]).map((name) => (
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
        <div className="absolute z-10 mt-1 w-full max-h-48 overflow-auto rounded-md border border-ink-200 bg-white shadow-card">
          {options.length === 0 ? (
            <div className="px-3 py-2 text-xs text-ink-500">
              (no other tasks yet)
            </div>
          ) : (
            options.map((o) => (
              <label
                key={o}
                className="flex items-center gap-2 px-3 py-1.5 text-sm hover:bg-ink-50 cursor-pointer"
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
function DependencyGraph({ tasks }: { tasks: TaskRow[] }) {
  const { t } = useTranslation();
  const [hover, setHover] = useState<string | null>(null);

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
      <div className="min-w-0 w-full min-h-[320px] overflow-hidden rounded-md border border-ink-200 bg-gradient-to-br from-ink-50/40 to-white">
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
                stroke="#475569"
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
                stroke="#ea580c"
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
            const stroke = summaryEdge ? "#ea580c" : "#475569";
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
              ? "#fff7ed"
              : root
              ? "#ecfeff"
              : isHover
              ? "#f0f9ff"
              : "#ffffff";
            const stroke = summary
              ? "#ea580c"
              : root
              ? "#0891b2"
              : isHover
              ? "#0f172a"
              : "#111827";
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
                    fill={summary ? "#fdba74" : "#67e8f9"}
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
                    fill="#ea580c"
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
                    fill="#0891b2"
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
                      fill="#fbbf24"
                      stroke="#92400e"
                      strokeWidth={1}
                    />
                    <text
                      x={r * 0.74}
                      y={-r * 0.74 + 1.5}
                      textAnchor="middle"
                      fontSize={5.5}
                      fontWeight="700"
                      fill="#78350f"
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
              className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md bg-ink-900 px-2 py-1 text-xs text-white shadow-md whitespace-nowrap"
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
          <span className="inline-block w-3 h-3 rounded-full border-2 border-ink-900 bg-white" />
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
): { rowKey?: string; message: string }[] {
  const issues: { rowKey?: string; message: string }[] = [];
  const ids = new Set<string>();
  const ownerRepoBranch = new Map<string, { repo: string; branch: string }>();
  const ownerConflictReported = new Set<string>();
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
      const branch = r.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH;
      const prev = ownerRepoBranch.get(ownerKey);
      if (!prev) {
        ownerRepoBranch.set(ownerKey, { repo, branch });
      } else if (
        (prev.repo !== repo || prev.branch !== branch)
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
          }
        : {
            id: r.ownerId.trim(),
            kind: r.ownerKind as AgentKind,
            isLeader,
            repo: r.ownerRepo.trim(),
            targetBranch: r.ownerTargetBranch.trim() || DEFAULT_TARGET_BRANCH,
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
  leaderKind: OwnerKind;
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
    const normalizedLeaderTargetBranch =
      leaderTargetBranch.trim() || DEFAULT_TARGET_BRANCH;
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
          leaderKind,
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
    if (!status?.resultAgents || !status?.resultTasks) return;
    onApply({
      agents: status.resultAgents,
      tasks: status.resultTasks,
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
              <button className="btn-primary" onClick={applyProposal}>
                ⬇ {t("flowEditor.decompose.apply")}
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
        ? (r.ownerTargetBranch || DEFAULT_TARGET_BRANCH)
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
): TaskRow[] {
  const byId = new Map<string, { kind: OwnerKind; repo: string; targetBranch: string }>();
  for (const a of proposal.agents) {
    const aid = String(a.id ?? "").trim();
    if (!aid) continue;
    const kind: OwnerKind = a.kind === "openclaw"
      ? "openclaw"
      : a.kind === "codex"
      ? "codex"
      : a.kind === "cursor"
      ? "cursor"
      : a.kind === "hermes"
      ? "hermes"
      : "claude";
    const repo = String(a.repo ?? "");
    const targetBranch = String(
      a.targetBranch ?? a.target_branch ?? DEFAULT_TARGET_BRANCH,
    ).trim() || DEFAULT_TARGET_BRANCH;
    byId.set(aid, { kind, repo, targetBranch });
  }
  for (const a of openclawOptions) {
    if (!byId.has(a.id)) {
      byId.set(a.id, { kind: "openclaw", repo: "", targetBranch: "" });
    }
  }

  return proposal.tasks.map((tk) => {
    const ownerId = String(tk.ownerAgentId ?? tk.owner_agent_id ?? "").trim();
    const meta = byId.get(ownerId) ?? {
      kind: "claude" as OwnerKind,
      repo: "",
      targetBranch: DEFAULT_TARGET_BRANCH,
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
      ownerKind: meta.kind,
      ownerId,
      ownerRepo: isNonOpenclawKind(meta.kind) ? meta.repo : "",
      ownerTargetBranch: isNonOpenclawKind(meta.kind)
        ? (meta.targetBranch || DEFAULT_TARGET_BRANCH)
        : "",
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
    const ownerKind: OwnerKind =
      a?.kind === "openclaw"
        ? "openclaw"
        : a?.kind === "codex"
        ? "codex"
        : a?.kind === "cursor"
        ? "cursor"
        : a?.kind === "hermes"
        ? "hermes"
        : "claude";
    return {
      rowKey: newRowKey(),
      id: tk.id,
      subject: tk.subject,
      description: tk.description ?? "",
      outputSummaryRequirement: tk.outputSummaryRequirement ?? "",
      requiresHumanCheckpoint: !!tk.requiresHumanCheckpoint,
      ownerKind,
      ownerId: tk.ownerAgentId,
      ownerRepo: isNonOpenclawKind(ownerKind) ? a?.repo ?? "" : "",
      ownerTargetBranch: isNonOpenclawKind(ownerKind)
        ? (a?.targetBranch ?? DEFAULT_TARGET_BRANCH)
        : "",
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
        window.alert(lines.join("\n"));
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
