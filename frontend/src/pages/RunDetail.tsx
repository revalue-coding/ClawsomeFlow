/**
 * Run Detail — live view of one Run.
 *
 * Sections (top-to-bottom):
 *   1. Header: status pill + flow link + abort button
 *   2. Pending merges UI (only when status=awaiting_user_review)
 *   3. Dark task board (left: running/completed tasks; right: dependency graph)
 *   4. Live event stream (WebSocket /ws/{run_id} + REST backfill)
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import { useParams } from "react-router-dom";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  PendingMerge,
  RunDetail as RunDetailT,
  RunTaskTerminal,
  api,
} from "@/lib/api";
import {
  Card,
  CardTitle,
  ErrorBox,
  Loading,
  Modal,
  StatusPill,
} from "@/components/ui";
import { useDialog } from "@/components/dialog";
import { DEFAULT_TARGET_BRANCH } from "@/lib/flowRuntime";
import { useSessionBackedState } from "@/lib/sessionState";
import { RunWsEvent, eventViewToWs, openRunStream } from "@/lib/ws";

const TERMINAL = new Set([
  "completed",
  "completed_with_conflicts",
  "complaint_failed",
  "failed",
  "aborted",
]);
const LEADER_REPLY_VISIBLE = new Set([
  "awaiting_user_review",
  "awaiting_user_complaint",
  "complaint_processing",
  ...TERMINAL,
]);

const TASK_CANVAS_MIN_WIDTH = 280;
const TASK_CANVAS_MIN_HEIGHT = 300;
const TASK_PAD_X = 44;
const TASK_PAD_Y = 32;
const TASK_NODE_RADIUS = 8;
const EVENT_PAGE_SIZE = 10;

function normalizeRunInputs(inputs: Record<string, unknown> | null | undefined): Array<[string, string]> {
  if (!inputs) return [];
  const rows: Array<[string, string]> = [];
  for (const key of Object.keys(inputs).sort()) {
    const name = key.trim();
    if (!name) continue;
    const raw = inputs[key];
    let value = "";
    if (typeof raw === "string" || typeof raw === "number" || typeof raw === "boolean") {
      value = String(raw);
    } else if (raw == null) {
      value = "";
    } else {
      try {
        value = JSON.stringify(raw);
      } catch {
        value = String(raw);
      }
    }
    if (!value.trim()) continue;
    rows.push([name, value]);
  }
  return rows;
}

type TaskRuntimeState = "pending" | "dispatched" | "completed";
type CheckpointBoardState = "none" | "pending" | "rerun_requested" | "approved";

type TaskBoardNode = {
  id: string;
  subject: string;
  ownerAgentId: string;
  dependsOn: string[];
  isLeaderSummary: boolean;
  hasCheckpoint: boolean;
  checkpointState: CheckpointBoardState;
  state: TaskRuntimeState;
  /** Minutes from successful dispatch to completion; set only when completed. */
  durationMinutes: number | null;
  order: number;
  level: number;
  x: number;
  y: number;
};

type TaskBoardEdge = {
  from: string;
  to: string;
  highlight: boolean;
  animate: boolean;
};

type TaskBoardModel = {
  visibleNodes: TaskBoardNode[];
  listNodes: TaskBoardNode[];
  edges: TaskBoardEdge[];
  nodeById: Map<string, TaskBoardNode>;
  width: number;
  height: number;
};

type MergeFailureKind = "conflict" | "environment_error" | "unknown";

type MergeFailureItem = {
  eventId: number;
  eventType: string;
  agentId: string;
  sourceBranch: string;
  targetBranch: string;
  repoRoot: string | null;
  failureKind: MergeFailureKind;
  reason: string;
};

type CheckpointDecision = "pending" | "approved" | "rerun_requested";

type CheckpointItem = {
  taskId: string;
  subject: string;
  ownerAgentId: string;
  summary: string | null;
  decision: CheckpointDecision;
  rerunCount: number;
  lastFeedback: string | null;
  hasUnreadUpdate: boolean;
};

type ActiveCheckpoint = {
  downstreamTaskId: string;
  downstreamSubject: string;
  downstreamOwnerAgentId: string;
  allApproved: boolean;
  items: CheckpointItem[];
};

type BoardTab = "list" | "terminal";

const EMPTY_TASK_BOARD: TaskBoardModel = {
  visibleNodes: [],
  listNodes: [],
  edges: [],
  nodeById: new Map(),
  width: 720,
  height: 320,
};

export function RunDetail() {
  const { id } = useParams();
  const { t } = useTranslation();
  const { confirm, alert } = useDialog();
  const [run, setRun] = useState<RunDetailT | null>(null);
  const [flowName, setFlowName] = useState<string>("");
  const [error, setError] = useState<string | null>(null);
  const [events, setEvents] = useState<RunWsEvent[]>([]);
  const [wsStatus, setWsStatus] = useState<
    "connecting" | "open" | "closed" | "error"
  >("connecting");
  const [aborting, setAborting] = useState(false);
  const [complaintText, setComplaintText] = useState("");
  const [complaintSubmitting, setComplaintSubmitting] = useState(false);
  const [complaintActionCommitted, setComplaintActionCommitted] = useState(false);
  const [complaintNotice, setComplaintNotice] = useState<string | null>(null);
  const [checkpointSnapshot, setCheckpointSnapshot] = useState<Record<string, unknown> | null>(null);
  const [checkpointActingTaskIds, setCheckpointActingTaskIds] = useState<string[]>([]);
  const [rerunModalTaskId, setRerunModalTaskId] = useSessionBackedState<string | null>(
    `run-detail:${id ?? "unknown"}:rerun-modal-task-id`,
    null,
    { isClosed: (value) => value === null },
  );
  const [rerunFeedback, setRerunFeedback] = useSessionBackedState(
    `run-detail:${id ?? "unknown"}:rerun-feedback`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [rerunSubmitting, setRerunSubmitting] = useState(false);
  const [boardTab, setBoardTab] = useState<BoardTab>("list");
  const [terminalItems, setTerminalItems] = useState<RunTaskTerminal[]>([]);
  const [terminalLoading, setTerminalLoading] = useState(false);
  const [terminalError, setTerminalError] = useState<string | null>(null);
  const [selectedTerminalTaskId, setSelectedTerminalTaskId] = useState<string | null>(null);

  // Load run + initial event backfill.
  useEffect(() => {
    if (!id) return;
    let cancelled = false;
    (async () => {
      try {
        const r = await api.getRun(id);
        if (cancelled) return;
        setRun(r);
        if (r.status === "awaiting_user_checkpoint") {
          try {
            const cp = await api.getRunCheckpoint(id);
            if (!cancelled) setCheckpointSnapshot(cp ?? null);
          } catch {
            if (!cancelled) setCheckpointSnapshot(null);
          }
        } else {
          setCheckpointSnapshot(null);
        }
        try {
          const f = await api.getFlow(r.flowId);
          if (!cancelled) setFlowName(f.name || r.flowId);
        } catch {
          if (!cancelled) setFlowName(r.flowId);
        }
        const ev = await api.listRunEvents(id, 0, 200);
        if (!cancelled) setEvents(ev.items.map(eventViewToWs));
      } catch (e) {
        if (!cancelled)
          setError(e instanceof ApiError ? e.message : String(e));
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [id]);

  // Poll detail every 5s so status pill / pending_merges stay current.
  useEffect(() => {
    if (!id) return;
    const tid = setInterval(async () => {
      try {
        const r = await api.getRun(id);
        setRun(r);
        if (r.status === "awaiting_user_checkpoint") {
          try {
            const cp = await api.getRunCheckpoint(id);
            setCheckpointSnapshot(cp ?? null);
          } catch {
            setCheckpointSnapshot(null);
          }
        } else {
          setCheckpointSnapshot(null);
        }
      } catch {
        /* ignore transient */
      }
    }, 5000);
    return () => clearInterval(tid);
  }, [id]);

  // WebSocket live event stream.
  useEffect(() => {
    if (!id) return;
    const handle = openRunStream(id, {
      onEvent: (e) => setEvents((prev) => mergeById(prev, e)),
      onStatus: setWsStatus,
    });
    return () => handle.close();
  }, [id]);

  useEffect(() => {
    setComplaintText("");
    setComplaintActionCommitted(false);
    setComplaintNotice(null);
    setFlowName("");
    setCheckpointSnapshot(null);
    setCheckpointActingTaskIds([]);
    setRerunSubmitting(false);
    setBoardTab("list");
    setTerminalItems([]);
    setTerminalLoading(false);
    setTerminalError(null);
    setSelectedTerminalTaskId(null);
  }, [id]);

  function beginCheckpointAction(taskId: string) {
    setCheckpointActingTaskIds((prev) => (prev.includes(taskId) ? prev : [...prev, taskId]));
  }

  function endCheckpointAction(taskId: string) {
    setCheckpointActingTaskIds((prev) => prev.filter((id) => id !== taskId));
  }

  async function refreshRunAndCheckpoint(runId: string) {
    const r = await api.getRun(runId);
    setRun(r);
    if (r.status === "awaiting_user_checkpoint") {
      try {
        const cp = await api.getRunCheckpoint(runId);
        setCheckpointSnapshot(cp ?? null);
      } catch {
        setCheckpointSnapshot(null);
      }
    } else {
      setCheckpointSnapshot(null);
    }
    return r;
  }

  async function refreshCheckpointAfterApprove(runId: string) {
    let latest = await refreshRunAndCheckpoint(runId);
    if (latest.status !== "running") return;
    // After one checkpoint is approved, the next checkpoint (if any) is
    // opened by the scheduler in the following tick. Do short retries so the
    // UI can advance to the next item quickly.
    for (let i = 0; i < 4; i += 1) {
      await new Promise((resolve) => setTimeout(resolve, 350));
      latest = await refreshRunAndCheckpoint(runId);
      if (latest.status !== "running") break;
    }
  }

  async function onAbort() {
    if (
      !run
      || complaintSubmitting
      || complaintActionCommitted
      || run.status === "complaint_processing"
    ) {
      return;
    }
    if (!run || !(await confirm(t("runDetail.abortConfirm")))) return;
    setAborting(true);
    try {
      const r = await api.abortRun(run.id);
      setRun({ ...run, status: r.status });
      setCheckpointSnapshot(null);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setAborting(false);
    }
  }

  async function onMerge(agentId: string) {
    if (!run) return;
    try {
      const out = await api.mergePending(run.id, agentId);
      if (!out.success) {
        void alert(`${t("common.failed")}:\n${out.message.slice(0, 400)}`);
      }
      const r = await api.getRun(run.id);
      setRun(r);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }

  async function onDismiss(agentId: string) {
    if (!run) return;
    if (!(await confirm(`${t("runDetail.dismiss")} (${agentId})?`))) return;
    try {
      await api.dismissPending(run.id, agentId);
      const r = await api.getRun(run.id);
      setRun(r);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    }
  }

  async function onSubmitComplaint() {
    if (!run) return;
    const text = complaintText.trim();
    if (!text) {
      void alert(t("runDetail.complaint.emptyError"));
      return;
    }
    setComplaintSubmitting(true);
    try {
      await api.submitRunComplaint(run.id, text);
      setComplaintActionCommitted(true);
      const r = await api.getRun(run.id);
      setRun(r);
      setComplaintText("");
      setComplaintNotice(t("runDetail.complaint.submittedNotice"));
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setComplaintSubmitting(false);
    }
  }

  async function onSkipComplaint() {
    if (!run) return;
    if (complaintSubmitting) return;
    setComplaintSubmitting(true);
    try {
      await api.skipRunComplaint(run.id);
      setComplaintActionCommitted(true);
      const r = await api.getRun(run.id);
      setRun(r);
      setComplaintText("");
      setComplaintNotice(null);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setComplaintSubmitting(false);
    }
  }

  async function onApproveCheckpointItem(taskId: string) {
    if (!run) return;
    beginCheckpointAction(taskId);
    try {
      await api.approveCheckpointItem(run.id, taskId);
      await refreshCheckpointAfterApprove(run.id);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      endCheckpointAction(taskId);
    }
  }

  async function onMarkCheckpointItemRead(taskId: string) {
    if (!run) return;
    beginCheckpointAction(taskId);
    try {
      await api.markCheckpointItemRead(run.id, taskId);
      await refreshRunAndCheckpoint(run.id);
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      endCheckpointAction(taskId);
    }
  }

  async function onSubmitCheckpointRerun() {
    if (!run || !rerunModalTaskId) return;
    const text = rerunFeedback.trim();
    if (!text) {
      void alert(t("runDetail.checkpoint.rerunFeedbackRequired"));
      return;
    }
    const taskId = rerunModalTaskId;
    beginCheckpointAction(taskId);
    setRerunSubmitting(true);
    try {
      await api.rerunCheckpointItem(run.id, taskId, text);
      await refreshRunAndCheckpoint(run.id);
      setRerunModalTaskId(null);
      setRerunFeedback("");
    } catch (e) {
      void alert(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setRerunSubmitting(false);
      endCheckpointAction(taskId);
    }
  }

  const loadRunTerminals = useCallback(async () => {
    if (!id) return;
    if (terminalItems.length === 0) setTerminalLoading(true);
    setTerminalError(null);
    try {
      const out = await api.listRunTerminals(id, 120);
      setTerminalItems(out.items);
    } catch (e) {
      setTerminalError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setTerminalLoading(false);
    }
  }, [id, terminalItems.length]);

  useEffect(() => {
    if (!id || boardTab !== "terminal") return;
    void loadRunTerminals();
    const tid = setInterval(() => {
      void loadRunTerminals();
    }, 5000);
    return () => clearInterval(tid);
  }, [id, boardTab, loadRunTerminals]);

  useEffect(() => {
    if (terminalItems.length === 0) {
      setSelectedTerminalTaskId(null);
      return;
    }
    if (
      !selectedTerminalTaskId
      || !terminalItems.some((item) => item.taskId === selectedTerminalTaskId)
    ) {
      setSelectedTerminalTaskId(terminalItems[0].taskId);
    }
  }, [terminalItems, selectedTerminalTaskId]);

  const activeCheckpoint = useMemo(() => {
    const fromSnapshot = checkpointSnapshot ? parseCheckpointPayload(checkpointSnapshot) : null;
    return fromSnapshot ?? extractActiveCheckpoint(events);
  }, [checkpointSnapshot, events]);
  const board = useMemo(
    () => (run ? buildTaskBoard(run, events, activeCheckpoint) : EMPTY_TASK_BOARD),
    [run, events, activeCheckpoint],
  );
  const mergeFailures = useMemo(() => extractMergeFailures(events), [events]);
  const rerunTargetItem = useMemo(
    () =>
      rerunModalTaskId
        ? (activeCheckpoint?.items.find((item) => item.taskId === rerunModalTaskId) ?? null)
        : null,
    [activeCheckpoint, rerunModalTaskId],
  );
  const runInputs = useMemo(() => normalizeRunInputs(run?.inputs), [run?.inputs]);
  const checkpointRerunInProgress = Boolean(
    activeCheckpoint?.items.some((item) => item.decision === "rerun_requested"),
  );
  const checkpointActionsLocked = checkpointRerunInProgress || rerunSubmitting;
  if (error) return <ErrorBox>{error}</ErrorBox>;
  if (!run) return <Loading />;
  const leaderReply = extractLeaderReply(run, events);
  const showLeaderReply = LEADER_REPLY_VISIBLE.has(run.status);
  const showComplaintPanel =
    run.status === "awaiting_user_complaint" ||
    (run.status === "complaint_processing" && Boolean(complaintNotice));
  const abortLockedByComplaint =
    complaintSubmitting
    || complaintActionCommitted
    || run.status === "complaint_processing";
  const boardHint = t("runDetail.boardHint").trim();

  return (
    <div className="space-y-5">
      <div>
        <SilentLink to="/runs" className="btn-outline">
          {t("common.back")}
        </SilentLink>
      </div>
      {/* Header */}
      <div className="flex items-center justify-between">
        <div className="space-y-1">
          <h1 className="text-xl font-semibold text-ink-900">
            {t("runDetail.title")} · {flowName || run.flowId}
          </h1>
          <div className="flex items-center gap-3 text-sm text-ink-500">
            <StatusPill status={run.status} />
            <span>
              {t("runDetail.flowLabel")}{" "}
              <SilentLink
                to={`/flows/${run.flowId}`}
                className="text-brand-600 hover:underline"
              >
                {flowName || run.flowId}
              </SilentLink>
            </span>
            <span>
              {t("runList.columnStarted")}: {new Date(run.startedAt).toLocaleString()}
            </span>
            {run.finishedAt && (
              <span>· {t("runList.columnFinished")}: {new Date(run.finishedAt).toLocaleString()}</span>
            )}
          </div>
        </div>
        <div className="flex items-center gap-2">
          <span
            className={
              wsStatus === "open"
                ? "pill-success"
                : wsStatus === "connecting"
                ? "pill-info"
                : "pill-warning"
            }
            title={t("runDetail.eventsTitle")}
          >
            ws · {wsStatus}
          </span>
          {!TERMINAL.has(run.status) && (
            <button
              className="btn-danger"
              onClick={onAbort}
              disabled={aborting || abortLockedByComplaint}
            >
              {aborting ? t("runDetail.aborting") : t("runDetail.abort")}
            </button>
          )}
        </div>
      </div>

      {/* Pending merges */}
      {run.pendingMerges && run.pendingMerges.length > 0 && (
        <Card className="border-amber-200">
          <CardTitle hint={t("runDetail.pendingMergeHint")}>
            {t("runDetail.pendingMerges")} ({run.pendingMerges.length})
          </CardTitle>
          <div className="space-y-3">
            {run.pendingMerges.map((p) => (
              <PendingMergeCard
                key={p.agentId}
                pending={p}
                onMerge={() => onMerge(p.agentId)}
                onDismiss={() => onDismiss(p.agentId)}
              />
            ))}
          </div>
        </Card>
      )}

      {run.status === "awaiting_user_checkpoint" && (
        <Card className="border-brand-200">
          <CardTitle hint={t("runDetail.checkpoint.hint")}>
            {t("runDetail.checkpoint.title")}
          </CardTitle>
          {activeCheckpoint ? (
            <div className="space-y-3">
              <div className="rounded-md border border-brand-100 bg-brand-50/40 px-3 py-2 text-xs text-ink-700">
                <div>
                  {t("runDetail.checkpoint.targetTask")}:{" "}
                  <span className="font-mono">{activeCheckpoint.downstreamTaskId}</span>{" "}
                  · {activeCheckpoint.downstreamSubject || "—"}
                </div>
                <div className="mt-1">
                  {t("runDetail.checkpoint.targetOwner")}:{" "}
                  <span className="font-mono">{activeCheckpoint.downstreamOwnerAgentId}</span>
                </div>
              </div>
              {activeCheckpoint.items.map((item) => {
                const stateLabel = item.decision === "approved"
                  ? t("runDetail.checkpoint.stateApproved")
                  : item.decision === "rerun_requested"
                  ? t("runDetail.checkpoint.stateRerunRequested")
                  : t("runDetail.checkpoint.statePending");
                const waitingForRerunOutput = item.decision === "rerun_requested";
                const busy = checkpointActingTaskIds.includes(item.taskId);
                return (
                  <div
                    key={`checkpoint-item-${item.taskId}`}
                    className={
                      item.hasUnreadUpdate
                        ? "rounded-md border border-amber-300 bg-amber-50/50 px-4 py-3 shadow-[0_0_0_1px_rgba(245,158,11,0.2)]"
                        : "rounded-md border border-ink-200 bg-surface px-4 py-3"
                    }
                  >
                    <div className="flex items-center justify-between gap-3">
                      <div className="text-sm font-medium text-ink-900">
                        {t("runDetail.checkpoint.itemTask")}:{" "}
                        <span className="font-mono">{item.taskId}</span>{" "}
                        · {item.subject || "—"}
                      </div>
                      <div className="flex items-center gap-2">
                        {item.hasUnreadUpdate && (
                          <span className="pill-warning">
                            {t("runDetail.checkpoint.outputUpdated")}
                          </span>
                        )}
                        <span className="pill-default">{stateLabel}</span>
                      </div>
                    </div>
                    <div className="mt-1 text-xs text-ink-500">
                      {t("runDetail.checkpoint.itemOwner")}:{" "}
                      <span className="font-mono">{item.ownerAgentId}</span>
                    </div>
                    <div className="mt-2 text-xs text-ink-600">
                      {t("runDetail.checkpoint.itemSummary")}
                    </div>
                    <div className="mt-1 rounded-md border border-ink-100 bg-ink-50/60 px-3 py-2 text-sm text-ink-700 whitespace-pre-wrap break-words">
                      {waitingForRerunOutput
                        ? t("runDetail.checkpoint.refreshing")
                        : (item.summary?.trim() || t("runDetail.checkpoint.summaryMissing"))}
                    </div>
                    {item.hasUnreadUpdate && (
                      <div className="mt-2 flex items-center justify-between gap-3 rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-900">
                        <span>{t("runDetail.checkpoint.outputUpdatedHint")}</span>
                        <button
                          type="button"
                          className="btn-outline"
                          disabled={busy}
                          onClick={() => void onMarkCheckpointItemRead(item.taskId)}
                        >
                          {t("runDetail.checkpoint.markRead")}
                        </button>
                      </div>
                    )}
                    <div className="mt-3 flex items-center justify-end gap-2">
                      <button
                        type="button"
                        className="btn-outline"
                        disabled={item.decision === "approved" || busy || checkpointActionsLocked}
                        onClick={() => void onApproveCheckpointItem(item.taskId)}
                      >
                        {item.decision === "approved"
                          ? t("runDetail.checkpoint.approved")
                          : t("runDetail.checkpoint.approve")}
                      </button>
                      <button
                        type="button"
                        className="btn-primary"
                        disabled={busy || checkpointActionsLocked}
                        onClick={() => {
                          setRerunModalTaskId(item.taskId);
                          setRerunFeedback(item.lastFeedback ?? "");
                        }}
                      >
                        {t("runDetail.checkpoint.rerun")}
                      </button>
                    </div>
                  </div>
                );
              })}
              <div className="flex justify-end">
                <button
                  type="button"
                  className="btn-danger"
                  onClick={onAbort}
                  disabled={aborting}
                >
                  {aborting
                    ? t("runDetail.aborting")
                    : t("runDetail.checkpoint.abortFlow")}
                </button>
              </div>
            </div>
          ) : (
            <div className="text-sm text-ink-600">
              {t("runDetail.checkpoint.refreshing")}
            </div>
          )}
        </Card>
      )}

      {mergeFailures.length > 0 && (
        <Card className="border-rose-200">
          <CardTitle hint={t("runDetail.mergeFailureHint")}>
            {t("runDetail.mergeFailureTitle")} ({mergeFailures.length})
          </CardTitle>
          <div className="space-y-3">
            {mergeFailures.map((item) => {
              const reasonLine = item.reason
                .split(/\r?\n/)
                .map((s) => s.trim())
                .find((s) => s.length > 0) ?? "";
              const failureKindLabel = item.failureKind === "conflict"
                ? t("runDetail.mergeFailureKindConflict")
                : item.failureKind === "environment_error"
                ? t("runDetail.mergeFailureKindEnvironment")
                : t("runDetail.mergeFailureKindUnknown");
              return (
                <div
                  key={`merge-failure-${item.eventId}-${item.agentId}`}
                  className="rounded-md border border-rose-200 bg-rose-50/40 p-4"
                >
                  <div className="text-sm font-medium text-ink-900">
                    {item.agentId}
                    <span className="ml-2 text-xs text-ink-500">({failureKindLabel})</span>
                  </div>
                  <div className="mt-2 text-xs text-ink-600 font-mono break-all">
                    {t("runDetail.mergeFailureSourceBranch")}: {item.sourceBranch}
                  </div>
                  <div className="mt-1 text-xs text-ink-600 font-mono break-all">
                    {t("runDetail.mergeFailureTargetBranch")}: {item.targetBranch}
                  </div>
                  {item.repoRoot && (
                    <div className="mt-1 text-xs text-ink-600 font-mono break-all">
                      {t("runDetail.mergeFailureRepoRoot")}: {item.repoRoot}
                    </div>
                  )}
                  <div className="mt-1 text-xs text-ink-600 font-mono break-all">
                    {t("runDetail.mergeFailureCommand")}:{" "}
                    {`git checkout ${item.targetBranch} && git merge --no-ff ${item.sourceBranch}`}
                  </div>
                  {reasonLine && (
                    <div className="mt-2 text-xs text-ink-700 break-words">
                      {t("runDetail.mergeFailureReason")}: {reasonLine}
                    </div>
                  )}
                </div>
              );
            })}
          </div>
        </Card>
      )}

      {showComplaintPanel && (
        <Card className="border-brand-200">
          <CardTitle>
            {t("runDetail.complaint.title")}
          </CardTitle>
          {run.status === "awaiting_user_complaint" ? (
            <div className="space-y-3">
              <textarea
                className="input min-h-[140px]"
                value={complaintText}
                onChange={(e) => setComplaintText(e.target.value)}
                placeholder={t("runDetail.complaint.placeholder")}
                disabled={complaintSubmitting}
              />
              <div className="flex items-center justify-end gap-2">
                <button
                  type="button"
                  className="btn-outline"
                  onClick={onSkipComplaint}
                  disabled={complaintSubmitting}
                >
                  {t("runDetail.complaint.skip")}
                </button>
                <button
                  className="btn-primary"
                  onClick={onSubmitComplaint}
                  disabled={complaintSubmitting}
                >
                  {complaintSubmitting
                    ? t("runDetail.complaint.submitting")
                    : t("runDetail.complaint.action")}
                </button>
              </div>
            </div>
          ) : (
            <div className="space-y-2 text-sm text-ink-600">
              <div>{t("runDetail.complaint.processing")}</div>
              {complaintNotice ? (
                <div className="rounded-md border border-brand-200 bg-brand-50/40 px-3 py-2 text-brand-800">
                  {complaintNotice}
                </div>
              ) : null}
            </div>
          )}
        </Card>
      )}

      <Card className="border-ink-200">
        <CardTitle>
          {t("runDetail.flowInputsTitle")}
        </CardTitle>
        {runInputs.length === 0 ? (
          <div className="text-sm text-ink-500">{t("runDetail.flowInputsEmpty")}</div>
        ) : (
          <div className="space-y-2">
            {runInputs.map(([name, value]) => (
              <div
                key={name}
                className="grid grid-cols-[10rem_minmax(0,1fr)] items-start gap-3 rounded-md border border-ink-100 bg-ink-50/50 px-3 py-2"
              >
                <div className="truncate text-xs font-medium text-ink-700" title={name}>
                  {name}
                </div>
                <div className="overflow-x-auto whitespace-nowrap text-xs text-ink-600">
                  {value}
                </div>
              </div>
            ))}
          </div>
        )}
      </Card>

      {/* Leader handoff */}
      {showLeaderReply && (
        <Card className="relative overflow-hidden border-brand-400 bg-gradient-to-br from-ink-900/[0.04] to-surface shadow-[0_0_0_1px_rgb(var(--brand-400)),0_0_32px_-10px_rgb(var(--brand-500))] dark:border-brand-500/70 dark:from-white/[0.06] dark:to-surface dark:shadow-[0_0_0_1px_rgb(var(--brand-500)),0_0_36px_-8px_rgb(var(--brand-500))]">
          {/* Brand spotlight: this is the run's headline deliverable, so it gets
              a glow + ring + left accent bar to stand out from the other cards
              (the surface itself stays neutral — no heavy red fill). */}
          <span className="pointer-events-none absolute inset-y-0 left-0 w-1 bg-brand-500" />
          <CardTitle>
            {t("runDetail.leaderReplyTitle")}
          </CardTitle>
          <div className="text-sm text-ink-700 whitespace-pre-wrap">
            {leaderReply ?? t("runDetail.leaderReplyMissing")}
          </div>
          <div className="mt-3 border-t border-ink-200 pt-2 text-xs text-ink-500">
            {t("runDetail.leaderReplyFootnote")}
          </div>
        </Card>
      )}

      {/* Task dependency board */}
      <Card className="p-0 overflow-hidden">
        <div className="px-5 py-3 border-b border-ink-100">
          <h3 className="text-base font-semibold text-ink-900">
            {t("runDetail.boardTitle")}
          </h3>
          {boardTab === "list" && boardHint ? (
            <div className="text-xs text-ink-500">{boardHint}</div>
          ) : null}
          <div className="mt-2 inline-flex rounded-md border border-ink-200 bg-ink-50 p-0.5 text-xs">
            <button
              type="button"
              className={
                boardTab === "list"
                  ? "rounded-sm bg-surface px-3 py-1 font-medium text-ink-900 shadow-sm"
                  : "rounded-sm px-3 py-1 text-ink-600 hover:text-ink-900"
              }
              onClick={() => setBoardTab("list")}
            >
              {t("runDetail.boardTabList")}
            </button>
            <button
              type="button"
              className={
                boardTab === "terminal"
                  ? "rounded-sm bg-surface px-3 py-1 font-medium text-ink-900 shadow-sm"
                  : "rounded-sm px-3 py-1 text-ink-600 hover:text-ink-900"
              }
              onClick={() => setBoardTab("terminal")}
            >
              {t("runDetail.boardTabTerminal")}
            </button>
          </div>
        </div>
        {boardTab === "list" ? (
          <TaskDependencyBoard board={board} />
        ) : (
          <RunTerminalBoard
            items={terminalItems}
            loading={terminalLoading}
            error={terminalError}
            selectedTaskId={selectedTerminalTaskId}
            onSelectTaskId={setSelectedTerminalTaskId}
          />
        )}
      </Card>

      {/* Live event stream */}
      <Card>
        <CardTitle>
          {t("runDetail.eventsTitle")} ({events.length})
        </CardTitle>
        <EventTable events={events} />
      </Card>

      <Modal
        open={!!rerunModalTaskId}
        onClose={() => {
          if (rerunSubmitting) return;
          if (rerunModalTaskId) endCheckpointAction(rerunModalTaskId);
          setRerunModalTaskId(null);
          setRerunFeedback("");
        }}
        title={t("runDetail.checkpoint.rerunModalTitle")}
        width="max-w-2xl"
      >
        <div className="space-y-3">
          {rerunTargetItem && (
            <div className="text-xs text-ink-500">
              {t("runDetail.checkpoint.itemTask")}:{" "}
              <span className="font-mono">{rerunTargetItem.taskId}</span>{" "}
              · {rerunTargetItem.subject || "—"}
            </div>
          )}
          <label className="label">
            {t("runDetail.checkpoint.rerunFeedbackLabel")}
          </label>
          <textarea
            className="textarea h-36"
            value={rerunFeedback}
            onChange={(e) => setRerunFeedback(e.target.value)}
            placeholder={t("runDetail.checkpoint.rerunFeedbackPlaceholder")}
            disabled={rerunSubmitting}
          />
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (rerunModalTaskId) endCheckpointAction(rerunModalTaskId);
                setRerunModalTaskId(null);
                setRerunFeedback("");
              }}
              disabled={rerunSubmitting}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSubmitCheckpointRerun()}
              disabled={rerunSubmitting}
            >
              {rerunSubmitting
                ? t("runDetail.checkpoint.rerunSubmitting")
                : t("runDetail.checkpoint.rerunSubmit")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

function RunTerminalBoard({
  items,
  loading,
  error,
  selectedTaskId,
  onSelectTaskId,
}: {
  items: RunTaskTerminal[];
  loading: boolean;
  error: string | null;
  selectedTaskId: string | null;
  onSelectTaskId: (taskId: string) => void;
}) {
  const { t } = useTranslation();
  if (loading && items.length === 0) {
    return (
      <div className="bg-[#090f1f] text-ink-100 px-5 py-6 text-sm">
        {t("runDetail.terminal.loading")}
      </div>
    );
  }
  if (items.length === 0) {
    return (
      <div className="bg-[#090f1f] text-ink-100 px-5 py-6 text-sm">
        {error || t("runDetail.terminal.empty")}
      </div>
    );
  }
  const selected = items.find((item) => item.taskId === selectedTaskId) ?? items[0];
  return (
    <div className="bg-[#090f1f] text-ink-100 p-4">
      <div className="grid min-w-0 gap-4 md:grid-cols-[20rem_minmax(0,1fr)]">
        <div className="min-w-0 rounded-md border border-[#2a3558] bg-[#0d152b] p-3 min-h-[360px]">
          <div className="text-xs text-[#90a4d8] mb-2">
            {t("runDetail.terminal.taskListTitle")}
          </div>
          <div className="space-y-2">
            {items.map((item) => (
              <button
                key={`terminal-task-${item.taskId}`}
                type="button"
                className={
                  selected.taskId === item.taskId
                    ? "w-full text-left rounded-md border border-[#4f79de] bg-[#15264f] px-3 py-2"
                    : "w-full text-left rounded-md border border-[#2a3558] bg-[#111c39] px-3 py-2"
                }
                onClick={() => onSelectTaskId(item.taskId)}
              >
                <div className="truncate text-sm text-[#e8eeff]">
                  {item.subject || "—"}
                </div>
                <div className="mt-1 text-[11px] text-[#9ab0df] font-mono">
                  {item.taskId} · {item.ownerAgentId}
                </div>
              </button>
            ))}
          </div>
        </div>
        <div className="min-w-0 rounded-md border border-[#2a3558] bg-[#0d152b] overflow-hidden">
          <div className="flex items-center justify-between border-b border-[#2a3558] px-3 py-2">
            <div className="text-xs text-[#90a4d8]">{t("runDetail.terminal.paneTitle")}</div>
            {error ? <div className="text-[11px] text-amber-300">{error}</div> : null}
          </div>
          <div className="border-b border-[#2a3558] px-3 py-2 text-[11px] text-[#9ab0df]">
            <div>
              <span className="font-mono">{selected.taskId}</span> · {selected.subject || "—"}
            </div>
            <div className="mt-1">
              {t("runDetail.terminal.ownerLabel")}:{" "}
              <span className="font-mono">{selected.ownerAgentId}</span>
            </div>
            <div className="mt-1">
              tmux: <span className="font-mono">{selected.tmuxTarget}</span>
            </div>
          </div>
          <pre className="h-[420px] overflow-auto whitespace-pre-wrap break-words px-3 py-3 text-xs text-[#dce8ff]">
            {selected.available
              ? (selected.paneText || t("runDetail.terminal.unavailable"))
              : t("runDetail.terminal.unavailable")}
          </pre>
        </div>
      </div>
    </div>
  );
}

function TaskDependencyBoard({
  board,
}: {
  board: TaskBoardModel;
}) {
  const { t } = useTranslation();
  const [hoverNodeId, setHoverNodeId] = useState<string | null>(null);
  if (board.visibleNodes.length === 0) {
    return (
      <div className="bg-[#090f1f] text-ink-100 px-5 py-6 text-sm">
        {t("runDetail.boardEmpty")}
      </div>
    );
  }
  const hovered = board.visibleNodes.find((n) => n.id === hoverNodeId) ?? null;
  const nodeRadius = (n: TaskBoardNode): number =>
    n.isLeaderSummary ? TASK_NODE_RADIUS + 2 : n.state === "dispatched" ? TASK_NODE_RADIUS + 1 : TASK_NODE_RADIUS;
  const checkpointBadge = (n: TaskBoardNode): { label: string; className: string } | null => {
    if (!n.hasCheckpoint || n.checkpointState === "none") return null;
    if (n.checkpointState === "approved") {
      return {
        label: t("runDetail.boardCheckpointApproved"),
        className: "inline-flex rounded-full bg-emerald-700/30 px-2 py-0.5 text-[10px] text-emerald-100",
      };
    }
    if (n.checkpointState === "rerun_requested") {
      return {
        label: t("runDetail.boardCheckpointRerun"),
        className: "inline-flex rounded-full bg-amber-600/30 px-2 py-0.5 text-[10px] text-amber-100",
      };
    }
    return {
      label: t("runDetail.boardCheckpointPending"),
      className: "inline-flex rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] text-amber-100",
    };
  };

  return (
    <div className="bg-[#090f1f] text-ink-100 p-4">
      <div className="grid min-w-0 gap-4 md:grid-cols-2">
        <div className="min-w-0 rounded-md border border-[#2a3558] bg-[#0d152b] p-3 min-h-[360px]">
          <div className="text-xs text-[#90a4d8] mb-2">
            {t("runDetail.boardTaskListTitle")}
          </div>
          <div className="space-y-2">
            {board.listNodes.map((n) => {
              const cpBadge = checkpointBadge(n);
              return (
                <div
                  key={`list-${n.id}`}
                  className="w-full text-left rounded-md border border-[#2a3558] bg-[#111c39] px-3 py-2"
                >
                  <div className="flex items-start justify-between gap-2">
                    <div className="min-w-0 flex-1 text-sm text-[#e8eeff] truncate">
                      {n.subject}
                    </div>
                    {n.state === "completed" && n.durationMinutes != null && (
                      <span className="shrink-0 inline-flex rounded-full bg-[#1a3a2a] px-2 py-0.5 text-[10px] text-emerald-200">
                        {t("runDetail.boardTaskDurationMinutes", {
                          minutes: formatBoardDurationMinutes(n.durationMinutes),
                        })}
                      </span>
                    )}
                  </div>
                  <div className="mt-1 flex items-center justify-between">
                    <span className="text-[11px] text-[#9ab0df] font-mono">
                      {n.ownerAgentId}
                    </span>
                    <div className="flex items-center gap-1.5">
                      {cpBadge && (
                        <>
                          <span className="inline-flex rounded-full bg-amber-500/20 px-2 py-0.5 text-[10px] text-amber-100">
                            {t("runDetail.boardCheckpointBadge")}
                          </span>
                          <span className={cpBadge.className}>
                            {cpBadge.label}
                          </span>
                        </>
                      )}
                      <span
                        className={
                          n.state === "dispatched"
                            ? "inline-flex rounded-full bg-[#2148a8] px-2 py-0.5 text-[10px] text-white"
                            : "inline-flex rounded-full bg-[#1f3348] px-2 py-0.5 text-[10px] text-[#c4d6ff]"
                        }
                      >
                        {n.state === "dispatched"
                          ? t("runDetail.boardNodeRunning")
                          : t("runDetail.boardNodeDone")}
                      </span>
                    </div>
                  </div>
                </div>
              );
            })}
          </div>
        </div>
        <div className="min-w-0 w-full min-h-[360px] overflow-hidden rounded-md border border-[#2a3558] bg-[#0d152b]">
          <div className="relative w-full">
            <svg
              className="w-full h-auto block"
              viewBox={`0 0 ${board.width} ${board.height}`}
              preserveAspectRatio="xMidYMid meet"
            >
              <defs>
                <marker
                  id="arrow-open-normal"
                  markerWidth="10"
                  markerHeight="10"
                  refX="9"
                  refY="5"
                  orient="auto"
                >
                  <path d="M1,1 L9,5 L1,9" fill="none" stroke="#4d679d" strokeWidth="1.6" />
                </marker>
                <marker
                  id="arrow-open-highlight"
                  markerWidth="10"
                  markerHeight="10"
                  refX="9"
                  refY="5"
                  orient="auto"
                >
                  <path d="M1,1 L9,5 L1,9" fill="none" stroke="#f5b942" strokeWidth="1.8" />
                </marker>
              </defs>
              {board.edges.map((e) => {
                const from = board.nodeById.get(e.from);
                const to = board.nodeById.get(e.to);
                if (!from || !to) return null;
                const sx = from.x;
                const sy = from.y;
                const tx = to.x;
                const ty = to.y;
                const dx = tx - sx;
                const dy = ty - sy;
                const dist = Math.sqrt(dx * dx + dy * dy) || 1;
                const endGap = nodeRadius(to) + 3;
                const ex = tx - (dx / dist) * endGap;
                const ey = ty - (dy / dist) * endGap;
                const stroke = e.highlight ? "#f5b942" : "#4d679d";
                const marker = e.highlight
                  ? "url(#arrow-open-highlight)"
                  : "url(#arrow-open-normal)";
                return (
                  <g key={`edge-${e.from}-${e.to}`}>
                    <line
                      x1={sx}
                      y1={sy}
                      x2={ex}
                      y2={ey}
                      fill="none"
                      stroke={stroke}
                      strokeWidth={1.8}
                      strokeLinejoin="round"
                      strokeLinecap="round"
                      markerEnd={marker}
                      opacity={0.7}
                    />
                    {e.animate && (
                      <line
                        x1={sx}
                        y1={sy}
                        x2={ex}
                        y2={ey}
                        fill="none"
                        stroke={stroke}
                        strokeWidth={2}
                        strokeLinejoin="round"
                        strokeLinecap="round"
                        markerEnd={marker}
                        className="dep-edge-flow"
                      />
                    )}
                  </g>
                );
              })}
              {board.visibleNodes.map((n) => {
                const ringStroke = n.isLeaderSummary
                  ? "#f5b942"
                  : n.state === "dispatched"
                  ? "#5e8bff"
                  : "#6f87be";
                const fill = n.isLeaderSummary
                  ? "#f5b942"
                  : n.state === "dispatched"
                  ? "#5e8bff"
                  : "#8fa8de";
                const radius = nodeRadius(n);
                const checkpointColor = n.checkpointState === "approved"
                  ? "#22c55e"
                  : n.checkpointState === "rerun_requested"
                  ? "#f59e0b"
                  : "#facc15";
                return (
                  <g
                    key={`node-${n.id}`}
                    onMouseEnter={() => setHoverNodeId(n.id)}
                    onMouseLeave={() => setHoverNodeId((cur) => (cur === n.id ? null : cur))}
                    style={{ cursor: "default" }}
                  >
                    {n.state === "dispatched" && (
                      <circle
                        cx={n.x}
                        cy={n.y}
                        r={radius + 6}
                        className="dep-node-pulse"
                        stroke={ringStroke}
                        fill="none"
                      />
                    )}
                    <circle
                      cx={n.x}
                      cy={n.y}
                      r={radius}
                      fill={fill}
                      stroke={ringStroke}
                      strokeWidth={n.isLeaderSummary ? 2.2 : 1.6}
                    >
                      <title>{`${n.subject} · ${n.ownerAgentId}`}</title>
                    </circle>
                    {n.hasCheckpoint && n.checkpointState !== "none" && (
                      <g>
                        <circle
                          cx={n.x + radius + 5}
                          cy={n.y - radius - 5}
                          r={4.5}
                          fill={checkpointColor}
                          stroke="#0b1220"
                          strokeWidth={1}
                        />
                        <text
                          x={n.x + radius + 5}
                          y={n.y - radius - 3.6}
                          textAnchor="middle"
                          fontSize={6}
                          fontWeight="700"
                          fill="#0b1220"
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
                className="pointer-events-none absolute z-10 -translate-x-1/2 -translate-y-full rounded-md bg-[#020817] px-2 py-1 text-xs text-white shadow-md whitespace-nowrap"
                style={{
                  left: `${(hovered.x / board.width) * 100}%`,
                  top: `${(hovered.y / board.height) * 100}%`,
                }}
              >
                {hovered.subject} · {hovered.ownerAgentId}
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────


function mergeById(prev: RunWsEvent[], next: RunWsEvent): RunWsEvent[] {
  if (prev.some((e) => e.id === next.id)) return prev;
  return [...prev, next].sort((a, b) => a.id - b.id);
}

function extractActiveCheckpoint(events: RunWsEvent[]): ActiveCheckpoint | null {
  let active: ActiveCheckpoint | null = null;
  const ordered = [...events].sort((a, b) => a.id - b.id);
  for (const e of ordered) {
    if (e.type === "task_checkpoint_waiting" || e.type === "task_checkpoint_updated") {
      active = parseCheckpointPayload(e.payload);
      continue;
    }
    if (e.type === "task_checkpoint_cleared") {
      active = null;
    }
  }
  return active;
}

function parseCheckpointPayload(payload: Record<string, unknown>): ActiveCheckpoint | null {
  const downstreamTaskId = firstNonEmptyString(
    payload["downstream_task_id"],
    payload["downstreamTaskId"],
  );
  if (!downstreamTaskId) return null;
  const downstreamSubject = firstNonEmptyString(
    payload["downstream_subject"],
    payload["downstreamSubject"],
  ) ?? "";
  const downstreamOwnerAgentId = firstNonEmptyString(
    payload["downstream_owner_agent_id"],
    payload["downstreamOwnerAgentId"],
  ) ?? "";
  const allApproved = Boolean(payload["all_approved"] ?? payload["allApproved"]);
  const rawItems = Array.isArray(payload["items"]) ? payload["items"] : [];
  const items: CheckpointItem[] = [];
  for (const raw of rawItems) {
    if (!raw || typeof raw !== "object") continue;
    const row = raw as Record<string, unknown>;
    const taskId = firstNonEmptyString(row["task_id"], row["taskId"]);
    if (!taskId) continue;
    const subject = firstNonEmptyString(row["subject"]) ?? "";
    const ownerAgentId = firstNonEmptyString(
      row["owner_agent_id"],
      row["ownerAgentId"],
      row["from_agent"],
      row["fromAgent"],
    ) ?? "";
    const summary = firstNonEmptyString(row["summary"]);
    const decisionRaw = firstNonEmptyString(row["decision"]) ?? "pending";
    const decision: CheckpointDecision =
      decisionRaw === "approved"
        ? "approved"
        : decisionRaw === "rerun_requested"
        ? "rerun_requested"
        : "pending";
    const rerunCountRaw = row["rerun_count"] ?? row["rerunCount"] ?? 0;
    const rerunCount = Number.isFinite(Number(rerunCountRaw))
      ? Number(rerunCountRaw)
      : 0;
    const lastFeedback = firstNonEmptyString(
      row["last_feedback"],
      row["lastFeedback"],
    );
    const hasUnreadUpdate = Boolean(
      row["has_unread_update"] ?? row["hasUnreadUpdate"],
    );
    items.push({
      taskId,
      subject,
      ownerAgentId,
      summary,
      decision,
      rerunCount,
      lastFeedback,
      hasUnreadUpdate,
    });
  }
  return {
    downstreamTaskId,
    downstreamSubject,
    downstreamOwnerAgentId,
    allApproved,
    items,
  };
}

function checkpointDecisionToBoardState(decision: CheckpointDecision): CheckpointBoardState {
  if (decision === "approved") return "approved";
  if (decision === "rerun_requested") return "rerun_requested";
  return "pending";
}

function collectCheckpointBoardStates(
  events: RunWsEvent[],
  activeCheckpoint: ActiveCheckpoint | null,
): Map<string, CheckpointBoardState> {
  const states = new Map<string, CheckpointBoardState>();
  const applyCheckpointPayload = (payload: Record<string, unknown>) => {
    const cp = parseCheckpointPayload(payload);
    if (!cp) return;
    for (const item of cp.items) {
      states.set(item.taskId, checkpointDecisionToBoardState(item.decision));
    }
  };
  const ordered = [...events].sort((a, b) => a.id - b.id);
  for (const e of ordered) {
    if (
      e.type === "task_checkpoint_waiting"
      || e.type === "task_checkpoint_updated"
      || e.type === "task_checkpoint_cleared"
    ) {
      applyCheckpointPayload(e.payload ?? {});
    }
  }
  if (activeCheckpoint) {
    for (const item of activeCheckpoint.items) {
      states.set(item.taskId, checkpointDecisionToBoardState(item.decision));
    }
  }
  return states;
}


function extractMergeFailures(events: RunWsEvent[]): MergeFailureItem[] {
  const byKey = new Map<string, MergeFailureItem>();
  const ordered = [...events].sort((a, b) => b.id - a.id);
  for (const e of ordered) {
    if (e.type !== "merge_conflict" && e.type !== "merge_error") continue;
    const payload = e.payload ?? {};
    const sourceBranch = firstNonEmptyString(
      payload["source_branch"],
      payload["sourceBranch"],
      payload["branch"],
    );
    if (!sourceBranch) continue;
    const targetBranch = firstNonEmptyString(
      payload["target_branch"],
      payload["targetBranch"],
      DEFAULT_TARGET_BRANCH,
    ) ?? DEFAULT_TARGET_BRANCH;
    const repoRoot = firstNonEmptyString(
      payload["repo_root"],
      payload["repoRoot"],
    );
    const reason = firstNonEmptyString(payload["stderr"]) ?? "";
    const failureKindRaw = firstNonEmptyString(
      payload["failure_kind"],
      payload["failureKind"],
    );
    const failureKind: MergeFailureKind = failureKindRaw === "conflict"
      ? "conflict"
      : failureKindRaw === "environment_error"
      ? "environment_error"
      : e.type === "merge_conflict"
      ? "conflict"
      : e.type === "merge_error"
      ? "environment_error"
      : "unknown";
    const agentId = e.agentId ?? "—";
    const key = `${agentId}::${sourceBranch}::${targetBranch}`;
    if (byKey.has(key)) continue;
    byKey.set(key, {
      eventId: e.id,
      eventType: e.type,
      agentId,
      sourceBranch,
      targetBranch,
      repoRoot,
      failureKind,
      reason,
    });
  }
  return [...byKey.values()].sort((a, b) => b.eventId - a.eventId);
}


function firstNonEmptyString(...values: unknown[]): string | null {
  for (const value of values) {
    if (typeof value !== "string") continue;
    const text = value.trim();
    if (text) return text;
  }
  return null;
}


function PendingMergeCard({
  pending,
  onMerge,
  onDismiss,
}: {
  pending: PendingMerge;
  onMerge: () => void;
  onDismiss: () => void;
}) {
  const { t } = useTranslation();
  const targetBranch = pending.targetBranch?.trim() || DEFAULT_TARGET_BRANCH;
  const diff = pending.diffSummary as {
    files_changed?: number | string[];
    insertions?: number;
    deletions?: number;
    commit_count?: number;
  };
  const fileCount = Array.isArray(diff?.files_changed)
    ? diff.files_changed.length
    : diff?.files_changed;
  return (
    <div className="rounded-md border border-amber-200 bg-amber-50/40 p-4 flex items-center justify-between gap-3">
      <div>
        <div className="font-medium text-ink-900">
          {pending.agentId}{" "}
          <span className="font-mono text-xs text-ink-500">
            ({pending.branch} → {targetBranch})
          </span>
        </div>
        <div className="text-xs text-ink-500 mt-1">
          {t("runDetail.mergeTarget")}: {targetBranch}
        </div>
        <div className="text-xs text-ink-500 mt-1">
          {fileCount != null
            ? `${fileCount} files · +${diff.insertions ?? 0} / -${diff.deletions ?? 0} · ${diff.commit_count ?? 0} commits`
            : t("common.none")}
        </div>
        {pending.leaderSuggestion && (
          <div className="text-xs text-ink-700 mt-2 italic">
            {t("runDetail.leaderSuggestion")}: “{pending.leaderSuggestion}”
          </div>
        )}
      </div>
      <div className="flex items-center gap-2 shrink-0">
        <button className="btn-primary" onClick={onMerge}>
          {t("runDetail.merge")}
        </button>
        <button className="btn-outline" onClick={onDismiss}>
          {t("runDetail.dismiss")}
        </button>
      </div>
    </div>
  );
}


function EventTable({ events }: { events: RunWsEvent[] }) {
  const { t } = useTranslation();
  const [page, setPage] = useState(1);
  // Newest at the top for at-a-glance debugging. Hooks must run on every
  // render — useMemo MUST come before any conditional return.
  const ordered = useMemo(() => [...events].sort((a, b) => b.id - a.id), [events]);
  const totalPages = Math.max(1, Math.ceil(ordered.length / EVENT_PAGE_SIZE));
  const pageStart = (page - 1) * EVENT_PAGE_SIZE;
  const pageItems = ordered.slice(pageStart, pageStart + EVENT_PAGE_SIZE);

  useEffect(() => {
    setPage((prev) => Math.min(Math.max(prev, 1), totalPages));
  }, [totalPages]);

  if (events.length === 0) {
    return (
      <div className="text-sm text-ink-500 py-3">
        {t("common.none")}
      </div>
    );
  }
  return (
    <div className="max-h-[480px] overflow-auto">
      <table className="w-full text-xs">
        <thead className="bg-ink-50 text-ink-500 sticky top-0">
          <tr>
            <th className="text-left px-3 py-2 font-medium w-12">#</th>
            <th className="text-left px-3 py-2 font-medium w-44">{t("runDetail.eventTimestampColumn")}</th>
            <th className="text-left px-3 py-2 font-medium w-48">{t("runDetail.eventTypeColumn")}</th>
            <th className="text-left px-3 py-2 font-medium w-32">{t("runDetail.eventAgentColumn")}</th>
          </tr>
        </thead>
        <tbody>
          {pageItems.map((e) => (
            <tr key={e.id} className="border-t border-ink-100">
              <td className="px-3 py-1.5 text-ink-400 font-mono">{e.id}</td>
              <td className="px-3 py-1.5 text-ink-500 font-mono">
                {new Date(e.ts).toLocaleTimeString()}
              </td>
              <td className="px-3 py-1.5">
                <span className="pill-default">{e.type}</span>
              </td>
              <td className="px-3 py-1.5 text-ink-700 font-mono">{e.agentId ?? "—"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {ordered.length > EVENT_PAGE_SIZE && (
        <div className="flex items-center justify-end gap-2 border-t border-ink-100 px-3 py-2 text-xs text-ink-600">
          <button
            type="button"
            className="btn-outline"
            onClick={() => setPage((p) => Math.max(1, p - 1))}
            disabled={page <= 1}
          >
            {t("common.prevPage")}
          </button>
          <span className="tabular-nums">
            {t("common.pageInfo", { page, total: totalPages })}
          </span>
          <button
            type="button"
            className="btn-outline"
            onClick={() => setPage((p) => Math.min(totalPages, p + 1))}
            disabled={page >= totalPages}
          >
            {t("common.nextPage")}
          </button>
        </div>
      )}
    </div>
  );
}

function parseEventTsMs(ts: string): number | null {
  const ms = Date.parse(ts);
  return Number.isFinite(ms) ? ms : null;
}

function computeTaskDurationMinutes(
  dispatchMs: number | undefined,
  completedMs: number | undefined,
): number | null {
  if (dispatchMs == null || completedMs == null || completedMs < dispatchMs) return null;
  return (completedMs - dispatchMs) / 60_000;
}

function formatBoardDurationMinutes(minutes: number): string {
  if (minutes < 1) {
    return String(Math.max(0.1, Math.round(minutes * 10) / 10));
  }
  const rounded = Math.round(minutes * 10) / 10;
  return Number.isInteger(rounded) ? String(Math.round(rounded)) : String(rounded);
}

function buildTaskBoard(
  run: RunDetailT,
  events: RunWsEvent[],
  activeCheckpoint: ActiveCheckpoint | null,
): TaskBoardModel {
  const tasks = Array.isArray(run.specSnapshot?.tasks) ? run.specSnapshot.tasks : [];
  if (tasks.length === 0) {
    return {
      visibleNodes: [],
      listNodes: [],
      edges: [],
      nodeById: new Map(),
      width: 720,
      height: 320,
    };
  }

  const tasksById = new Map(tasks.map((t) => [t.id, t]));
  const states = new Map<string, TaskRuntimeState>();
  const checkpointStates = collectCheckpointBoardStates(events, activeCheckpoint);
  const dispatchOrder = new Map<string, number>();
  const dispatchTimestamps = new Map<string, number>();
  const completedTimestamps = new Map<string, number>();
  let orderCounter = 0;
  for (const t of tasks) states.set(t.id, "pending");

  const orderedEvents = [...events].sort((a, b) => a.id - b.id);
  for (const e of orderedEvents) {
    const tid = typeof e.taskId === "string" ? e.taskId : "";
    if (tid && tasksById.has(tid)) {
      if (e.type === "task_dispatched") {
        const tsMs = parseEventTsMs(e.ts);
        if (tsMs != null) dispatchTimestamps.set(tid, tsMs);
        if (states.get(tid) !== "completed") states.set(tid, "dispatched");
        if (!dispatchOrder.has(tid)) {
          orderCounter += 1;
          dispatchOrder.set(tid, orderCounter);
        }
      } else if (e.type === "task_completed") {
        const tsMs = parseEventTsMs(e.ts);
        if (tsMs != null) completedTimestamps.set(tid, tsMs);
        states.set(tid, "completed");
        if (!dispatchOrder.has(tid)) {
          orderCounter += 1;
          dispatchOrder.set(tid, orderCounter);
        }
      }
    }
    if (e.type !== "run_terminal_execution_log") continue;
    const rows = Array.isArray((e.payload as Record<string, unknown>)?.tasks)
      ? ((e.payload as Record<string, unknown>).tasks as Array<Record<string, unknown>>)
      : [];
    for (const row of rows) {
      const rowId = String(row.task_id ?? row.taskId ?? "");
      if (!rowId || !tasksById.has(rowId)) continue;
      const rowState = String(row.state ?? "").toLowerCase();
      if (rowState === "completed") {
        states.set(rowId, "completed");
      } else if (rowState === "dispatched" || rowState === "running" || rowState === "in_progress") {
        if (states.get(rowId) !== "completed") states.set(rowId, "dispatched");
      }
      if (rowState !== "pending" && !dispatchOrder.has(rowId)) {
        orderCounter += 1;
        dispatchOrder.set(rowId, orderCounter);
      }
    }
  }

  const levelMemo = new Map<string, number>();
  const levelOf = (id: string, stack: Set<string> = new Set()): number => {
    const hit = levelMemo.get(id);
    if (hit != null) return hit;
    if (stack.has(id)) return 0;
    stack.add(id);
    const task = tasksById.get(id);
    if (!task) return 0;
    const deps = Array.isArray(task.dependsOn) ? task.dependsOn.filter((d) => tasksById.has(d)) : [];
    const level = deps.length > 0
      ? Math.max(...deps.map((d) => levelOf(d, new Set(stack)))) + 1
      : 0;
    levelMemo.set(id, level);
    return level;
  };

  const visibleNodesRaw = tasks
    .map((task) => ({
      task,
      state: states.get(task.id) ?? "pending",
      level: levelOf(task.id),
      order: dispatchOrder.get(task.id) ?? Number.MAX_SAFE_INTEGER,
    }))
    .filter((n) => n.state !== "pending");

  if (visibleNodesRaw.length === 0) {
    return {
      visibleNodes: [],
      listNodes: [],
      edges: [],
      nodeById: new Map(),
      width: 720,
      height: 320,
    };
  }

  const visibleById = new Map(visibleNodesRaw.map((n) => [n.task.id, n]));
  const maxLevel = Math.max(0, ...visibleNodesRaw.map((n) => n.level));
  const n = visibleNodesRaw.length;
  const width = Math.max(TASK_CANVAS_MIN_WIDTH, 260 + n * 84);
  const height = Math.max(TASK_CANVAS_MIN_HEIGHT, 210 + Math.ceil(n / 2) * 72);
  const innerWidth = width - TASK_PAD_X * 2;
  const innerHeight = height - TASK_PAD_Y * 2;

  type ForceNode = {
    id: string;
    x: number;
    y: number;
    fx: number;
    fy: number;
    targetX: number;
  };
  const seedRand = (seed: string) => {
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
  };

  const fNodes: ForceNode[] = visibleNodesRaw.map((row) => {
    const rand = seedRand(row.task.id);
    const targetX = maxLevel > 0
      ? TASK_PAD_X + (row.level / maxLevel) * innerWidth
      : TASK_PAD_X + innerWidth / 2;
    return {
      id: row.task.id,
      x: targetX + (rand() - 0.5) * 26,
      y: TASK_PAD_Y + rand() * innerHeight,
      fx: 0,
      fy: 0,
      targetX,
    };
  });
  const fById = new Map(fNodes.map((n2) => [n2.id, n2] as const));

  const edgeSpecs: Array<{
    from: string;
    to: string;
    highlight: boolean;
    animate: boolean;
  }> = [];
  for (const row of visibleNodesRaw) {
    for (const dep of row.task.dependsOn ?? []) {
      if (!visibleById.has(dep)) continue;
      edgeSpecs.push({
        from: dep,
        to: row.task.id,
        highlight: Boolean(row.task.isLeaderSummary),
        animate: row.state === "dispatched",
      });
    }
  }

  const ITERS = 140;
  const k = Math.sqrt((width * height) / Math.max(1, n)) * 0.6;
  for (let iter = 0; iter < ITERS; iter += 1) {
    for (const p of fNodes) {
      p.fx = 0;
      p.fy = 0;
    }
    for (let i = 0; i < fNodes.length; i += 1) {
      for (let j = i + 1; j < fNodes.length; j += 1) {
        const a = fNodes[i];
        const b = fNodes[j];
        let dx = a.x - b.x;
        let dy = a.y - b.y;
        let d2 = dx * dx + dy * dy;
        if (d2 < 0.5) {
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
    for (const e of edgeSpecs) {
      const a = fById.get(e.from);
      const b = fById.get(e.to);
      if (!a || !b) continue;
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
    for (const p of fNodes) {
      p.fx += (p.targetX - p.x) * 0.22;
    }
    const t = (1 - iter / ITERS) * 13 + 1;
    for (const p of fNodes) {
      const fmag = Math.sqrt(p.fx * p.fx + p.fy * p.fy) || 1;
      const step = Math.min(fmag, t);
      p.x += (p.fx / fmag) * step;
      p.y += (p.fy / fmag) * step;
      p.x = Math.max(TASK_PAD_X - 6, Math.min(width - TASK_PAD_X + 6, p.x));
      p.y = Math.max(TASK_PAD_Y - 6, Math.min(height - TASK_PAD_Y + 6, p.y));
    }
  }

  const minX = Math.min(...fNodes.map((p) => p.x));
  const maxX = Math.max(...fNodes.map((p) => p.x));
  const minY = Math.min(...fNodes.map((p) => p.y));
  const maxY = Math.max(...fNodes.map((p) => p.y));
  const offsetX = TASK_PAD_X - minX;
  const offsetY = TASK_PAD_Y - minY;
  const tightWidth = Math.max(TASK_CANVAS_MIN_WIDTH, (maxX - minX) + TASK_PAD_X * 2);
  const tightHeight = Math.max(TASK_CANVAS_MIN_HEIGHT, (maxY - minY) + TASK_PAD_Y * 2);

  const visibleNodes: TaskBoardNode[] = visibleNodesRaw.map((row) => {
    const p = fById.get(row.task.id)!;
    const hasCheckpoint = Boolean(
      (row.task as { requiresHumanCheckpoint?: unknown; requires_human_checkpoint?: unknown })
        .requiresHumanCheckpoint
        ?? (row.task as { requires_human_checkpoint?: unknown }).requires_human_checkpoint,
    );
    const checkpointState: CheckpointBoardState = hasCheckpoint && row.state !== "pending"
      ? (checkpointStates.get(row.task.id) ?? (row.state === "completed" ? "pending" : "none"))
      : "none";
    const durationMinutes = row.state === "completed"
      ? computeTaskDurationMinutes(
          dispatchTimestamps.get(row.task.id),
          completedTimestamps.get(row.task.id),
        )
      : null;
    return {
      id: row.task.id,
      subject: row.task.subject,
      ownerAgentId: row.task.ownerAgentId,
      dependsOn: row.task.dependsOn ?? [],
      isLeaderSummary: Boolean(row.task.isLeaderSummary),
      hasCheckpoint,
      checkpointState,
      state: row.state,
      durationMinutes,
      order: row.order,
      level: row.level,
      x: p.x + offsetX,
      y: p.y + offsetY,
    };
  });
  const nodeById = new Map(visibleNodes.map((n2) => [n2.id, n2] as const));

  const edges: TaskBoardEdge[] = edgeSpecs.filter((e) =>
    nodeById.has(e.from) && nodeById.has(e.to));

  const listNodes = [...visibleNodes].sort((a, b) => {
    if (a.state !== b.state) return a.state === "dispatched" ? -1 : 1;
    if (a.order !== b.order) return a.order - b.order;
    return a.id.localeCompare(b.id);
  });

  return { visibleNodes, listNodes, edges, nodeById, width: tightWidth, height: tightHeight };
}

type WorkerReportHistoryItem = {
  from_agent?: string;
  summary?: string;
};

function leaderAgentId(run: RunDetailT): string | null {
  const agents = run.specSnapshot?.agents;
  if (!Array.isArray(agents)) return null;
  for (const a of agents) {
    if (a && typeof a.id === "string" && a.isLeader) return a.id;
  }
  return null;
}

function extractLeaderReply(run: RunDetailT, events: RunWsEvent[]): string | null {
  const leaderId = leaderAgentId(run);
  const needle = "leader final reply:";
  const ordered = [...events].sort((a, b) => b.id - a.id);
  for (const e of ordered) {
    if (e.type !== "run_terminal_execution_log") continue;
    const reports = (e.payload?.worker_report_history ?? []) as WorkerReportHistoryItem[];
    for (let i = reports.length - 1; i >= 0; i -= 1) {
      const report = reports[i];
      const raw = typeof report?.summary === "string" ? report.summary.trim() : "";
      if (!raw) continue;
      const from = typeof report?.from_agent === "string" ? report.from_agent : "";
      const lower = raw.toLowerCase();
      if (lower.startsWith(needle)) {
        const stripped = raw.slice(needle.length).trim();
        return stripped || raw;
      }
      if (leaderId && from === leaderId) return raw;
    }
  }
  return null;
}
