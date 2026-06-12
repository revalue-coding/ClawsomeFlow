import { useEffect, useState } from "react";
import { Link, useNavigate } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { ApiError, FlowSummary, api } from "@/lib/api";
import { Card, EmptyState, ErrorBox, Loading, Modal } from "@/components/ui";
import { FlowIcon, RunIcon } from "@/components/icons";
import { getRunInputFields } from "@/lib/flowRuntime";
import { useSessionBackedState } from "@/lib/sessionState";

const PAGE_SIZE = 10;

export function FlowList() {
  const [items, setItems] = useState<FlowSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [runDialogFlow, setRunDialogFlow] = useSessionBackedState<FlowSummary | null>(
    "flow-list:run-dialog-flow",
    null,
    { isClosed: (value) => value === null },
  );
  const [runInputFields, setRunInputFields] = useSessionBackedState<string[]>(
    "flow-list:run-input-fields",
    [],
    { isClosed: (value) => value.length === 0 },
  );
  const [runInputValues, setRunInputValues] = useSessionBackedState<Record<string, string>>(
    "flow-list:run-input-values",
    {},
    { isClosed: (value) => Object.keys(value).length === 0 },
  );
  const [runDialogLoading, setRunDialogLoading] = useState(false);
  const [runDialogError, setRunDialogError] = useSessionBackedState<string | null>(
    "flow-list:run-dialog-error",
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [pendingDelete, setPendingDelete] = useSessionBackedState<FlowSummary | null>(
    "flow-list:pending-delete",
    null,
    { isClosed: (value) => value === null },
  );
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);
  const [page, setPage] = useState(1);
  /**
   * "Already-running" confirmation modal payload. ``activeStatuses`` is a
   * comma-joined list of the in-flight runs' user-facing statuses; we
   * keep it cheap (no extra round-trip) by deriving it from the list-
   * runs response right when the user clicks Run.
   */
  const [pendingRunAgain, setPendingRunAgain] = useSessionBackedState<{
    flow: FlowSummary;
    activeCount: number;
    activeStatuses: string;
  } | null>("flow-list:pending-run-again", null, { isClosed: (value) => value === null });
  const navigate = useNavigate();
  const { t } = useTranslation();

  async function load() {
    try {
      const r = await api.listFlows();
      setItems(r.items);
    } catch (e) {
      if (e instanceof ApiError) setError(e.message);
      else setError(String(e));
    }
  }

  useEffect(() => {
    load();
  }, []);

  const totalItems = items?.length ?? 0;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const pageStart = (page - 1) * PAGE_SIZE;
  const pageItems = items ? items.slice(pageStart, pageStart + PAGE_SIZE) : [];

  useEffect(() => {
    setPage((prev) => Math.min(Math.max(prev, 1), totalPages));
  }, [totalPages]);

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    const f = pendingDelete;
    try {
      await api.deleteFlow(f.id);
      setPendingDelete(null);
      await load();
    } catch (e) {
      setDeleteError(
        e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
      );
    } finally {
      setDeleting(false);
    }
  }

  async function triggerRunDirectly(flow: FlowSummary) {
    setBusy(flow.id);
    setRunDialogError(null);
    try {
      const r = await api.triggerRun(flow.id, {});
      navigate(`/runs/${r.id}`);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setRunDialogError(msg);
      setError(msg);
    } finally {
      setBusy(null);
    }
  }

  async function openRunDialog(flow: FlowSummary) {
    setRunDialogLoading(true);
    setRunDialogError(null);
    setRunDialogFlow(null);
    setRunInputFields([]);
    setRunInputValues({});
    try {
      const detail = await api.getFlow(flow.id);
      const fields = getRunInputFields(detail.spec);
      if (fields.length === 0) {
        await triggerRunDirectly(flow);
        return;
      }
      setRunInputFields(fields);
      setRunInputValues(Object.fromEntries(fields.map((x) => [x, ""])));
      setRunDialogFlow(flow);
    } catch (e) {
      const msg = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setRunDialogError(msg);
      setError(msg);
    } finally {
      setRunDialogLoading(false);
    }
  }

  /**
   * Click on "Run" goes through here first: look up any in-flight runs
   * of this Flow and, if there are some, pause to confirm a new
   * concurrent instance. Each Run still gets a unique team_name
   * (``csflow-{run_id_short}``) and unique worktree path, so concurrent
   * execution is safe — we just want the user to *intend* it.
   *
   * Errors during the lookup are non-fatal: if listing fails (offline?
   * permission?) we open the run dialog anyway rather than blocking a
   * legitimate launch.
   */
  async function onRunClicked(flow: FlowSummary) {
    const TERMINAL = new Set([
      "completed",
      "completed_with_conflicts",
      "complaint_failed",
      "failed",
      "aborted",
    ]);
    setBusy(flow.id);
    try {
      let active: { id: string; status: string }[] = [];
      try {
        const r = await api.listRuns({ flowId: flow.id });
        active = (r.items || [])
          .filter((row) => !TERMINAL.has(row.status))
          .map((row) => ({ id: row.id, status: row.status }));
      } catch {
        // Soft-fail: skip preflight on error.
        active = [];
      }
      if (active.length === 0) {
        await openRunDialog(flow);
        return;
      }
      setPendingRunAgain({
        flow,
        activeCount: active.length,
        activeStatuses: active.map((a) => a.status).join(", "),
      });
    } finally {
      setBusy(null);
    }
  }

  async function onTriggerConfirm() {
    if (!runDialogFlow) return;
    for (const field of runInputFields) {
      if (!(runInputValues[field] || "").trim()) {
        setRunDialogError(
          t("flowList.runDialog.requiredFieldError", { name: field }),
        );
        return;
      }
    }
    const inputs = Object.fromEntries(
      runInputFields.map((field) => [field, (runInputValues[field] || "").trim()]),
    );
    setBusy(runDialogFlow.id);
    setRunDialogError(null);
    try {
      const r = await api.triggerRun(runDialogFlow.id, {
        inputs,
      });
      setRunDialogFlow(null);
      navigate(`/runs/${r.id}`);
    } catch (e) {
      setRunDialogError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setBusy(null);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-900">{t("flowList.title")}</h1>
          <p className="mt-1 text-sm text-ink-500">{t("flowList.titleHint")}</p>
        </div>
        <Link to="/flows/new" className="btn-primary">
          + {t("flowList.new")}
        </Link>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}

      {items && items.length === 0 && (
        <EmptyState
          icon={<FlowIcon className="h-10 w-10" />}
          title={t("flowList.empty")}
          hint={t("flowList.emptyHint")}
          action={
            <Link to="/flows/new" className="btn-primary">
              + {t("flowList.new")}
            </Link>
          }
        />
      )}

      {items && items.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500">
              <tr>
                <th className="text-left px-4 py-2 font-medium">{t("flowList.columnName")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("flowList.columnGoal")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("flowList.columnLeader")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("flowList.columnUpdated")}</th>
                <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {pageItems.map((f) => {
                const goal = f.description.trim();
                const inflight = busy === f.id;
                return (
                  <tr key={f.id} className="table-row align-top">
                    <td className="px-4 py-3 max-w-[220px] align-top">
                      <div className="flex items-center gap-1.5">
                        <Link
                          to={`/flows/${f.id}`}
                          className="font-medium text-ink-900 hover:text-brand-600"
                        >
                          {f.name}
                        </Link>
                        {f.easyMode && (
                          <span
                            className="h-2 w-2 shrink-0 rounded-full bg-emerald-500"
                            title={t("flowEditor.easyModeSub")}
                            aria-label={t("flowEditor.easyMode")}
                          />
                        )}
                      </div>
                      <div className="text-[11px] text-ink-400 font-mono mt-0.5">
                        {f.id}
                      </div>
                    </td>
                    <td className="px-4 py-3 max-w-[360px] align-top text-ink-700">
                      {goal ? (
                        <div className="leading-6 line-clamp-2 whitespace-pre-line">
                          {goal}
                        </div>
                      ) : (
                        <span className="text-ink-400">
                          {t("flowList.goalEmpty")}
                        </span>
                      )}
                    </td>
                    <td className="px-4 py-3 align-top">
                      <LeaderCell
                        leaderId={f.leaderAgentId ?? null}
                        leaderKind={f.leaderKind ?? null}
                      />
                    </td>
                    <td className="px-4 py-3 align-top text-sm leading-6 text-ink-700 tabular-nums">
                      {new Date(f.updatedAt).toLocaleString()}
                    </td>
                    <td className="px-4 py-3 align-top">
                      <div className="flex items-center justify-end gap-2 whitespace-nowrap">
                        <button
                          className="btn-primary"
                          disabled={inflight}
                          onClick={() => onRunClicked(f)}
                        >
                          <RunIcon className="h-4 w-4" />
                          {inflight ? t("flowList.runningButton") : t("flowList.runButton")}
                        </button>
                        <button
                          type="button"
                          className="btn-outline"
                          onClick={() => navigate(`/flows/${f.id}`)}
                        >
                          {t("common.edit")}
                        </button>
                        <button
                          className="btn-danger"
                          disabled={inflight}
                          onClick={() => {
                            setDeleteError(null);
                            setPendingDelete(f);
                          }}
                        >
                          {t("common.delete")}
                        </button>
                      </div>
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
          {totalItems > PAGE_SIZE && (
            <div className="flex items-center justify-end gap-2 border-t border-ink-100 px-4 py-3 text-xs text-ink-600">
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
        </Card>
      )}

      <Modal
        open={!!runDialogFlow}
        onClose={() => {
          if (busy) return;
          setRunDialogFlow(null);
          setRunInputFields([]);
          setRunInputValues({});
          setRunDialogError(null);
        }}
        title={t("flowList.runDialog.title")}
        width="max-w-2xl"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-700">{t("flowList.runDialog.fieldsHint")}</p>
          <div className="space-y-2">
            {runInputFields.map((field) => (
              <div key={field}>
                <label className="label">{field}</label>
                <input
                  className="input"
                  placeholder={t("flowList.runDialog.fieldPlaceholder", { name: field })}
                  value={runInputValues[field] || ""}
                  onChange={(e) =>
                    setRunInputValues((prev) => ({ ...prev, [field]: e.target.value }))
                  }
                  disabled={!!busy || runDialogLoading}
                />
              </div>
            ))}
          </div>
          {runDialogLoading && <Loading label={t("common.loading")} />}
          {runDialogError && <ErrorBox>{runDialogError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setRunDialogFlow(null);
                setRunInputFields([]);
                setRunInputValues({});
                setRunDialogError(null);
              }}
              disabled={!!busy}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={onTriggerConfirm}
              disabled={!runDialogFlow || !!busy || runDialogLoading}
            >
              {busy ? t("flowList.runDialog.starting") : t("flowList.runDialog.start")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={!!pendingDelete}
        onClose={() => {
          if (deleting) return;
          setPendingDelete(null);
          setDeleteError(null);
        }}
        title={t("flowList.deleteModalTitle")}
        width="max-w-md"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-700">
            {pendingDelete &&
              t("flowList.deleteModalConfirm", { name: pendingDelete.name })}
          </p>
          {deleteError && <ErrorBox>{deleteError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setPendingDelete(null);
                setDeleteError(null);
              }}
              disabled={deleting}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-danger"
              onClick={confirmDelete}
              disabled={deleting}
            >
              {deleting ? t("flowList.deleting") : t("flowList.deleteModalOk")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={!!pendingRunAgain}
        onClose={() => setPendingRunAgain(null)}
        title={t("flowList.runAgainModal.title")}
        width="max-w-md"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-700">
            {pendingRunAgain &&
              t(
                pendingRunAgain.activeCount === 1
                  ? "flowList.runAgainModal.bodyOne"
                  : "flowList.runAgainModal.bodyMany",
                {
                  name: pendingRunAgain.flow.name,
                  count: pendingRunAgain.activeCount,
                  statuses: pendingRunAgain.activeStatuses,
                },
              )}
          </p>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => setPendingRunAgain(null)}
            >
              {t("flowList.runAgainModal.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                const flow = pendingRunAgain?.flow;
                setPendingRunAgain(null);
                if (flow) void openRunDialog(flow);
              }}
            >
              {t("flowList.runAgainModal.confirm")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}


/**
 * Render the Flow's leader as a small id + kind chip. Legacy summaries
 * may not carry the leader fields; fall back to a muted dash.
 */
function LeaderCell({
  leaderId,
  leaderKind,
}: {
  leaderId: string | null;
  leaderKind: string | null;
}) {
  const { t } = useTranslation();
  if (!leaderId) {
    return <span className="text-ink-400">{t("flowList.leaderEmpty")}</span>;
  }
  const labelOf = (k: string | null) => {
    if (!k) return "";
    const key = `flowList.agentKind${k.charAt(0).toUpperCase()}${k.slice(1)}`;
    const label = t(key);
    return label === key ? k : label;
  };
  const styleOf = (k: string | null) => {
    switch (k) {
      case "openclaw":
        return "bg-brand-50 text-brand-700 border-brand-200";
      case "claude":
        return "bg-violet-50 text-violet-700 border-violet-200";
      case "codex":
        return "bg-amber-50 text-amber-700 border-amber-200";
      case "cursor":
        return "bg-sky-50 text-sky-700 border-sky-200";
      case "gemini":
        return "bg-emerald-50 text-emerald-700 border-emerald-200";
      default:
        return "bg-ink-50 text-ink-700 border-ink-200";
    }
  };
  return (
    <div className="flex items-center gap-2 min-w-0 text-ink-800">
      <span className="truncate" title={leaderId}>
        {leaderId}
      </span>
      {leaderKind && (
        <span
          className={`inline-flex items-center rounded-md border px-1.5 py-0.5 font-medium shrink-0 ${styleOf(
            leaderKind,
          )}`}
        >
          {labelOf(leaderKind)}
        </span>
      )}
    </div>
  );
}
