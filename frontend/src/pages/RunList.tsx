import { useEffect, useMemo, useState } from "react";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import { ApiError, RunSummary, api } from "@/lib/api";
import { Card, EmptyState, ErrorBox, Loading, Modal, StatusPill } from "@/components/ui";
import { RunIcon } from "@/components/icons";
import { useDialog } from "@/components/dialog";

const STATUS_OPTIONS = [
  "pending",
  "compiling",
  "running",
  "awaiting_user_checkpoint",
  "awaiting_user_review",
  "awaiting_user_complaint",
  "complaint_processing",
  "complaint_failed",
  "completed",
  "completed_with_conflicts",
  "failed",
  "aborted",
  "orphaned",
] as const;

const ACTIVE_STATUSES = new Set([
  "pending",
  "compiling",
  "running",
  "awaiting_user_checkpoint",
  "awaiting_user_review",
  "awaiting_user_complaint",
  // NOTE: "complaint_processing" is intentionally NOT active — once the user
  // has submitted a complaint the Run is treated as ended and shown in the
  // history list (no in-progress card while the background fix runs).
]);

const HISTORY_PAGE_SIZE = 10;

function formatRunInputs(inputs: Record<string, unknown> | null | undefined): string {
  if (!inputs) return "—";
  const parts: string[] = [];
  for (const key of Object.keys(inputs).sort()) {
    const k = key.trim();
    if (!k) continue;
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
    parts.push(`${k}: ${value}`);
  }
  return parts.length > 0 ? parts.join(" | ") : "—";
}

export function RunList() {
  const [items, setItems] = useState<RunSummary[] | null>(null);
  const [flowNameById, setFlowNameById] = useState<Record<string, string>>({});
  const [error, setError] = useState<string | null>(null);
  const [statusFilter, setStatusFilter] = useState<string>("");
  const [flowSearch, setFlowSearch] = useState("");
  const [historyPage, setHistoryPage] = useState(1);
  const [clearing, setClearing] = useState(false);
  const { t, i18n } = useTranslation();
  const { confirm, alert } = useDialog();

  async function load() {
    setError(null);
    try {
      const [runs, flows] = await Promise.all([
        api.listRuns(statusFilter ? { status: statusFilter } : {}),
        api.listFlows(),
      ]);
      setItems(runs.items);
      const names: Record<string, string> = {};
      for (const f of flows.items) {
        names[f.id] = f.name;
      }
      setFlowNameById(names);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  useEffect(() => {
    void load();
    const tid = setInterval(() => {
      void load();
    }, 5000);
    return () => clearInterval(tid);
  }, [statusFilter]);

  async function handleClearHistory() {
    const ok = await confirm(t("runList.clearHistoryConfirm"), {
      danger: true,
      okText: t("runList.clearHistory"),
    });
    if (!ok) return;
    setClearing(true);
    try {
      const res = await api.clearRunHistory();
      setHistoryPage(1);
      await load();
      void alert(t("runList.clearHistoryDone", { count: res.runsDeleted }));
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setClearing(false);
    }
  }

  const sortedRuns = useMemo(
    () =>
      [...(items ?? [])].sort(
        (a, b) => new Date(b.startedAt).getTime() - new Date(a.startedAt).getTime(),
      ),
    [items],
  );
  const normalizedFlowSearch = flowSearch.trim().toLowerCase();
  const filteredRuns = useMemo(() => {
    if (!normalizedFlowSearch) return sortedRuns;
    return sortedRuns.filter((run) => {
      const flowName = flowNameById[run.flowId] || run.flowId;
      return flowName.toLowerCase().includes(normalizedFlowSearch);
    });
  }, [sortedRuns, flowNameById, normalizedFlowSearch]);

  const activeRuns = useMemo(
    () => filteredRuns.filter((r) => ACTIVE_STATUSES.has(r.status)),
    [filteredRuns],
  );

  const historyRuns = useMemo(
    () => filteredRuns.filter((r) => !ACTIVE_STATUSES.has(r.status)),
    [filteredRuns],
  );

  const historyTotalPages = Math.max(1, Math.ceil(historyRuns.length / HISTORY_PAGE_SIZE));
  const historyStart = (historyPage - 1) * HISTORY_PAGE_SIZE;
  const historyPageItems = historyRuns.slice(historyStart, historyStart + HISTORY_PAGE_SIZE);

  useEffect(() => {
    setHistoryPage((prev) => Math.min(Math.max(prev, 1), historyTotalPages));
  }, [historyTotalPages]);

  return (
    <div className="space-y-5">
      <div className="flex flex-wrap items-center justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-900">{t("runList.title")}</h1>
        </div>
        <div className="flex w-full flex-col gap-2 sm:w-auto sm:flex-row sm:items-center">
          <NotifyWebhookButton />
          <input
            type="search"
            className="input w-full sm:w-72"
            value={flowSearch}
            onChange={(e) => {
              setHistoryPage(1);
              setFlowSearch(e.target.value);
            }}
            placeholder={t("runList.searchPlaceholder")}
            aria-label={t("runList.searchByFlowName")}
          />
          <select
            className="select w-full sm:max-w-xs"
            value={statusFilter}
            onChange={(e) => {
              setHistoryPage(1);
              setStatusFilter(e.target.value);
            }}
            aria-label={t("runList.filterStatus")}
          >
            <option value="">{t("runList.filterStatusAll")}</option>
            {STATUS_OPTIONS.map((s) => {
              const k = `statusLabel.${s}`;
              return (
                <option key={s} value={s}>
                  {i18n.exists(k) ? t(k) : s}
                </option>
              );
            })}
          </select>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}

      {items && items.length === 0 && (
        <EmptyState
          icon={<RunIcon className="h-10 w-10" />}
          title={t("runList.empty")}
          hint={t("runList.emptyHint")}
        />
      )}

      {items && items.length > 0 && filteredRuns.length === 0 && (
        <EmptyState
          icon={<RunIcon className="h-10 w-10" />}
          title={t("runList.searchEmpty")}
          hint={t("runList.searchEmptyHint")}
        />
      )}

      {items && filteredRuns.length > 0 && (
        <div className="space-y-4">
          <div className="grid gap-3 sm:grid-cols-3">
            <Card>
              <div className="text-xs text-ink-500">{t("runList.kpiTotal")}</div>
              <div className="text-2xl font-semibold text-ink-900">{filteredRuns.length}</div>
            </Card>
            <Card>
              <div className="text-xs text-ink-500">{t("runList.kpiActive")}</div>
              <div className="text-2xl font-semibold text-brand-700">{activeRuns.length}</div>
            </Card>
            <Card>
              <div className="text-xs text-ink-500">{t("runList.kpiFinished")}</div>
              <div className="text-2xl font-semibold text-ink-900">{historyRuns.length}</div>
            </Card>
          </div>

          {activeRuns.length > 0 && (
            <Card>
              <div className="mb-3">
                <h2 className="text-base font-semibold text-ink-900">{t("runList.activeTitle")}</h2>
                {t("runList.activeHint") ? (
                  <div className="text-xs text-ink-500">{t("runList.activeHint")}</div>
                ) : null}
              </div>
              <div className="grid gap-3 md:grid-cols-2">
                {activeRuns.map((r) => (
                  <SilentLink
                    key={r.id}
                    as="div"
                    to={`/runs/${r.id}`}
                    className="block rounded-md border border-ink-200 p-3 hover:border-brand-300 transition-colors"
                  >
                    <div className="flex items-center justify-between gap-3">
                      <div className="font-medium text-ink-900">{flowNameById[r.flowId] || r.flowId}</div>
                      <StatusPill status={r.status} />
                    </div>
                    <div className="mt-2 text-xs text-ink-500 font-mono">{r.teamName}</div>
                    <div className="mt-1 text-xs text-ink-500">
                      {t("runList.columnStarted")}: {new Date(r.startedAt).toLocaleString()}
                    </div>
                    <div className="mt-2 text-xs text-ink-500">
                      {t("runList.columnInputs")}:
                    </div>
                    <div className="mt-1 overflow-x-auto whitespace-nowrap text-xs text-ink-600">
                      {formatRunInputs(r.inputs)}
                    </div>
                  </SilentLink>
                ))}
              </div>
            </Card>
          )}

          {historyRuns.length > 0 && (
            <Card className="p-0 overflow-hidden">
              <div className="flex items-start justify-between gap-3 px-4 py-3 border-b border-ink-100">
                <div>
                  <h2 className="text-base font-semibold text-ink-900">{t("runList.historyTitle")}</h2>
                  {t("runList.historyHint") ? (
                    <div className="text-xs text-ink-500">{t("runList.historyHint")}</div>
                  ) : null}
                </div>
                <button
                  type="button"
                  className="btn-outline shrink-0"
                  onClick={() => void handleClearHistory()}
                  disabled={clearing}
                >
                  {t("runList.clearHistory")}
                </button>
              </div>
              <table className="w-full text-sm">
                <thead className="bg-ink-50 text-ink-500">
                  <tr>
                    <th className="text-left px-4 py-2 font-medium">{t("runList.columnFlow")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("runDetail.teamLabel")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("runList.columnInputs")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("runList.columnStatus")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("runList.columnStarted")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("runList.columnFinished")}</th>
                  </tr>
                </thead>
                <tbody>
                  {historyPageItems.map((r) => (
                    <tr key={r.id} className="table-row">
                      <td className="px-4 py-3">
                        <SilentLink to={`/runs/${r.id}`} className="font-medium text-ink-900 hover:text-brand-600">
                          {flowNameById[r.flowId] || r.flowId}
                        </SilentLink>
                      </td>
                      <td className="px-4 py-3 font-mono text-xs text-ink-700">{r.teamName}</td>
                      <td className="px-4 py-3 align-middle">
                        <div className="w-72 max-w-72 overflow-x-auto whitespace-nowrap text-xs text-ink-600">
                          {formatRunInputs(r.inputs)}
                        </div>
                      </td>
                      <td className="px-4 py-3">
                        <StatusPill status={r.status} />
                      </td>
                      <td className="px-4 py-3 text-xs text-ink-500">{new Date(r.startedAt).toLocaleString()}</td>
                      <td className="px-4 py-3 text-xs text-ink-500">
                        {r.finishedAt ? new Date(r.finishedAt).toLocaleString() : "—"}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
              {historyRuns.length > HISTORY_PAGE_SIZE && (
                <div className="flex items-center justify-end gap-2 border-t border-ink-100 px-4 py-3 text-xs text-ink-600">
                  <button
                    type="button"
                    className="btn-outline"
                    onClick={() => setHistoryPage((p) => Math.max(1, p - 1))}
                    disabled={historyPage <= 1}
                  >
                    {t("common.prevPage")}
                  </button>
                  <span className="tabular-nums">
                    {t("common.pageInfo", { page: historyPage, total: historyTotalPages })}
                  </span>
                  <button
                    type="button"
                    className="btn-outline"
                    onClick={() => setHistoryPage((p) => Math.min(historyTotalPages, p + 1))}
                    disabled={historyPage >= historyTotalPages}
                  >
                    {t("common.nextPage")}
                  </button>
                </div>
              )}
            </Card>
          )}
        </div>
      )}
    </div>
  );
}


/**
 * Small settings entry for the run-terminal webhook. The URL is stored in
 * backend config (``notify_webhook_url``); an empty URL disables the
 * feature. "Send test" posts a sample payload with the SAVED config, so it
 * is disabled while the input differs from what's persisted.
 */
function NotifyWebhookButton() {
  const { t } = useTranslation();
  const [open, setOpen] = useState(false);
  const [loading, setLoading] = useState(false);
  const [savedUrl, setSavedUrl] = useState<string | null>(null);
  const [url, setUrl] = useState("");
  const [saving, setSaving] = useState(false);
  const [testing, setTesting] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function openModal() {
    setOpen(true);
    setNotice(null);
    setError(null);
    setLoading(true);
    try {
      const r = await api.getNotifyWebhook();
      setSavedUrl(r.url);
      setUrl(r.url ?? "");
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setLoading(false);
    }
  }

  async function onSave() {
    setSaving(true);
    setNotice(null);
    setError(null);
    try {
      const r = await api.setNotifyWebhook(url.trim() || null);
      setSavedUrl(r.url);
      setUrl(r.url ?? "");
      setNotice(r.url ? t("runList.notify.saved") : t("runList.notify.cleared"));
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setSaving(false);
    }
  }

  async function onTest() {
    setTesting(true);
    setNotice(null);
    setError(null);
    try {
      const r = await api.testNotifyWebhook();
      if (r.success) setNotice(t("runList.notify.testOk", { detail: r.message }));
      else setError(t("runList.notify.testFail", { detail: r.message }));
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setTesting(false);
    }
  }

  const dirty = url.trim() !== (savedUrl ?? "");

  return (
    <>
      <button type="button" className="btn-outline shrink-0" onClick={() => void openModal()}>
        {t("runList.notify.button")}
      </button>
      <Modal
        open={open}
        onClose={() => {
          if (saving || testing) return;
          setOpen(false);
        }}
        title={t("runList.notify.title")}
        width="max-w-lg"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-600">{t("runList.notify.hint")}</p>
          <div>
            <label className="label">{t("runList.notify.urlLabel")}</label>
            <input
              className="input font-mono text-xs"
              value={url}
              placeholder="https://example.com/webhook"
              onChange={(e) => setUrl(e.target.value)}
              disabled={loading || saving || testing}
            />
            <div className="text-xs text-ink-500 mt-1">
              {t("runList.notify.emptyHint")}
            </div>
          </div>
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
              onClick={() => void onTest()}
              disabled={loading || saving || testing || dirty || !savedUrl}
              title={dirty || !savedUrl ? t("runList.notify.testNeedsSave") : undefined}
            >
              {testing ? t("runList.notify.testing") : t("runList.notify.test")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSave()}
              disabled={loading || saving || testing || !dirty}
            >
              {saving ? t("runList.notify.saving") : t("runList.notify.save")}
            </button>
          </div>
        </div>
      </Modal>
    </>
  );
}

