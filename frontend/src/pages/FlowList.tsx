import { useEffect, useMemo, useState } from "react";
import { useNavigate } from "react-router-dom";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  FlowSummary,
  FlowWebhookChannel,
  NOTIFY_WEBHOOK_FORMATS,
  api,
} from "@/lib/api";
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
  const [searchQuery, setSearchQuery] = useState("");
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

  const normalizedSearch = searchQuery.trim().toLowerCase();
  const filteredItems = useMemo(() => {
    if (!items) return [];
    if (!normalizedSearch) return items;
    return items.filter((item) => item.name.toLowerCase().includes(normalizedSearch));
  }, [items, normalizedSearch]);
  const totalItems = filteredItems.length;
  const totalPages = Math.max(1, Math.ceil(totalItems / PAGE_SIZE));
  const pageStart = (page - 1) * PAGE_SIZE;
  const pageItems = filteredItems.slice(pageStart, pageStart + PAGE_SIZE);

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
        <SilentLink to="/flows/new" className="btn-primary">
          + {t("flowList.new")}
        </SilentLink>
      </div>

      <div className="max-w-md">
        <input
          type="search"
          className="input"
          value={searchQuery}
          onChange={(e) => {
            setPage(1);
            setSearchQuery(e.target.value);
          }}
          placeholder={t("flowList.searchPlaceholder")}
          aria-label={t("flowList.searchByName")}
        />
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}

      {items && items.length === 0 && (
        <EmptyState
          icon={<FlowIcon className="h-10 w-10" />}
          title={t("flowList.empty")}
          hint={t("flowList.emptyHint")}
          action={
            <SilentLink to="/flows/new" className="btn-primary">
              + {t("flowList.new")}
            </SilentLink>
          }
        />
      )}

      {items && items.length > 0 && filteredItems.length === 0 && (
        <EmptyState
          icon={<FlowIcon className="h-10 w-10" />}
          title={t("flowList.searchEmpty")}
          hint={t("flowList.searchEmptyHint")}
        />
      )}

      {items && filteredItems.length > 0 && (
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
                      <SilentLink
                        to={`/flows/${f.id}`}
                        className="font-medium text-ink-900 hover:text-brand-600"
                        title={f.id}
                      >
                        {f.name}
                      </SilentLink>
                      <FlowBadgeRow flow={f} />
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
                        <FlowNotifyButton flow={f} onSaved={load} />
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
 * Per-Flow webhook notification button + modal. Lives in the Flow list
 * "actions" column next to Run. Highlighted (brand) when the Flow has ≥1
 * channel configured, plain outline otherwise. The modal manages a list of
 * channels (URL + message format) and can test each row before saving.
 * ``onSaved`` reloads the list so the highlight/count refresh.
 */
type NotifyRow = { url: string; format: string; effectiveFormat?: string | null };

function FlowNotifyButton({ flow, onSaved }: { flow: FlowSummary; onSaved: () => void }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [rows, setRows] = useState<NotifyRow[]>([]);
  const [saving, setSaving] = useState(false);
  const [testingIdx, setTestingIdx] = useState<number | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const configured = (flow.notifyChannelCount ?? 0) > 0;

  function toRows(channels: FlowWebhookChannel[]): NotifyRow[] {
    return channels.map((c) => ({
      url: c.url,
      format: c.format ?? "auto",
      effectiveFormat: c.effectiveFormat,
    }));
  }

  async function openModal() {
    setOpen(true);
    setNotice(null);
    setError(null);
    setLoading(true);
    try {
      const r = await api.getFlowNotifyWebhooks(flow.id);
      setRows(toRows(r.channels));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  function addRow() {
    setRows((prev) => [...prev, { url: "", format: "auto" }]);
  }
  function removeRow(i: number) {
    setRows((prev) => prev.filter((_, idx) => idx !== i));
  }
  function updateRow(i: number, patch: Partial<NotifyRow>) {
    setRows((prev) => prev.map((r, idx) => (idx === i ? { ...r, ...patch } : r)));
  }

  const toChannels = (): FlowWebhookChannel[] =>
    rows
      .map((r) => ({ url: r.url.trim(), format: r.format === "auto" ? null : r.format }))
      .filter((r) => r.url);

  async function onSave() {
    setSaving(true);
    setNotice(null);
    setError(null);
    try {
      const cleaned = toChannels();
      const r = await api.setFlowNotifyWebhooks(flow.id, cleaned);
      setRows(toRows(r.channels));
      setNotice(cleaned.length ? t("flowNotify.saved") : t("flowNotify.cleared"));
      onSaved();
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function onTest(i: number) {
    const row = rows[i];
    if (!row.url.trim()) return;
    setTestingIdx(i);
    setNotice(null);
    setError(null);
    try {
      const r = await api.testFlowNotifyWebhooks(flow.id, {
        url: row.url.trim(),
        format: row.format === "auto" ? null : row.format,
      });
      if (r.success) setNotice(t("flowNotify.testOk", { detail: r.message }));
      else setError(t("flowNotify.testFail", { detail: r.message }));
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setTestingIdx(null);
    }
  }

  const busy = loading || saving || testingIdx !== null;

  return (
    <>
      <button
        type="button"
        className={
          configured
            ? "btn-primary shadow-sm shadow-brand-500/20 ring-2 ring-brand-100"
            : "btn-outline"
        }
        onClick={() => void openModal()}
        title={configured ? t("flowNotify.configuredTitle", { count: flow.notifyChannelCount }) : t("flowNotify.button")}
      >
        {t("flowNotify.button")}
        {configured ? ` (${flow.notifyChannelCount})` : ""}
      </button>
      <Modal
        open={open}
        onClose={() => {
          if (saving || testingIdx !== null) return;
          setOpen(false);
        }}
        title={t("flowNotify.title", { name: flow.name })}
        width="max-w-2xl"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-600">{t("flowNotify.hint")}</p>
          <p className="rounded-md bg-ink-50 px-3 py-2 text-xs leading-relaxed text-ink-600">
            {t("flowNotify.steps")}
          </p>

          {loading ? (
            <Loading label={t("common.loading")} />
          ) : (
            <div className="space-y-3">
              {rows.length === 0 && (
                <div className="rounded-md border border-dashed border-ink-200 px-3 py-4 text-center text-xs text-ink-500">
                  {t("flowNotify.channelsEmpty")}
                </div>
              )}
              {rows.map((row, i) => (
                <div key={i} className="rounded-md border border-ink-200 p-3 space-y-2">
                  <div className="flex items-start gap-2">
                    <div className="flex-1 space-y-2">
                      <input
                        className="input font-mono text-xs"
                        value={row.url}
                        placeholder="https://open.feishu.cn/open-apis/bot/v2/hook/…"
                        onChange={(e) => updateRow(i, { url: e.target.value })}
                        disabled={busy}
                      />
                      <div className="flex items-center gap-2">
                        <select
                          className="input text-xs w-auto"
                          value={row.format}
                          onChange={(e) => updateRow(i, { format: e.target.value })}
                          disabled={busy}
                        >
                          {NOTIFY_WEBHOOK_FORMATS.map((f) => (
                            <option key={f} value={f}>
                              {t(`flowNotify.formats.${f}`)}
                            </option>
                          ))}
                        </select>
                        <button
                          type="button"
                          className="btn-outline text-xs"
                          onClick={() => void onTest(i)}
                          disabled={busy || !row.url.trim()}
                        >
                          {testingIdx === i ? t("flowNotify.testing") : t("flowNotify.test")}
                        </button>
                      </div>
                    </div>
                    <button
                      type="button"
                      className="btn-danger text-xs shrink-0"
                      onClick={() => removeRow(i)}
                      disabled={busy}
                    >
                      {t("flowNotify.removeChannel")}
                    </button>
                  </div>
                </div>
              ))}
              <button
                type="button"
                className="btn-outline text-xs"
                onClick={addRow}
                disabled={busy}
              >
                + {t("flowNotify.addChannel")}
              </button>
            </div>
          )}

          {notice && (
            <div className="rounded-md border border-emerald-200 bg-emerald-50/60 px-3 py-2 text-xs text-emerald-800">
              {notice}
            </div>
          )}
          {error && <ErrorBox>{error}</ErrorBox>}
          <div className="flex items-center justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => setOpen(false)}
              disabled={saving || testingIdx !== null}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSave()}
              disabled={busy}
            >
              {saving ? t("flowNotify.saving") : t("flowNotify.save")}
            </button>
          </div>
        </div>
      </Modal>
    </>
  );
}


/**
 * Compact metadata row under the Flow name: mode dots plus the distinct agent
 * kinds in the spec. The raw id stays reachable via the name's tooltip.
 */
function FlowBadgeRow({ flow }: { flow: FlowSummary }) {
  const { t } = useTranslation();
  const kinds = (flow.agentKinds ?? []).slice(0, 4);
  const extraKinds = Math.max(0, (flow.agentKinds ?? []).length - kinds.length);
  const hasBadges = flow.easyMode || flow.devMode || kinds.length > 0;
  if (!hasBadges) return null;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1">
      {flow.easyMode && (
        <span
          className="inline-block h-2.5 w-2.5 rounded-full bg-emerald-500 ring-2 ring-emerald-100"
          title={t("flowEditor.easyModeSub")}
          aria-label={t("flowEditor.easyMode")}
        />
      )}
      {flow.devMode && (
        <span
          className="inline-block h-2.5 w-2.5 rounded-full bg-purple-500 ring-2 ring-purple-100"
          title={t("flowEditor.devModeSub")}
          aria-label={t("flowEditor.devMode")}
        />
      )}
      {kinds.map((k) => (
        <span
          key={k}
          className="inline-flex items-center rounded-full border border-ink-200 bg-ink-50 px-1.5 py-0.5 text-[10px] text-ink-600"
        >
          {k}
        </span>
      ))}
      {extraKinds > 0 && (
        <span className="inline-flex items-center rounded-full border border-ink-200 bg-ink-50 px-1.5 py-0.5 text-[10px] text-ink-500">
          +{extraKinds}
        </span>
      )}
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
      case "kimi":
        return "bg-rose-50 text-rose-700 border-rose-200";
      case "qwen":
        return "bg-indigo-50 text-indigo-700 border-indigo-200";
      case "opencode":
        return "bg-teal-50 text-teal-700 border-teal-200";
      case "pi":
        return "bg-lime-50 text-lime-700 border-lime-200";
      case "qoder":
        return "bg-cyan-50 text-cyan-700 border-cyan-200";
      case "codebuddy":
        return "bg-orange-50 text-orange-700 border-orange-200";
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
