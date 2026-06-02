import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  FlowSummary,
  RunScheduleExecutionDetail,
  RunScheduleExecutionSummary,
  RunScheduleSummary,
  api,
} from "@/lib/api";
import { Card, EmptyState, ErrorBox, Loading, Modal } from "@/components/ui";
import { FlowIcon } from "@/components/icons";
import { getRunInputFields } from "@/lib/flowRuntime";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";

const RUN_MODES = ["parallel", "serial"] as const;
const EXECUTE_MODES = ["once", "recurring"] as const;

interface EditableItem {
  flowId: string;
  inputs: Record<string, string>;
}

interface ItemEditorState {
  mode: "create" | "edit";
  index: number | null;
  flowId: string;
  inputs: Record<string, string>;
  requiredFields: string[];
}

function toDatetimeLocal(iso: string): string {
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return "";
  const offsetMs = d.getTimezoneOffset() * 60_000;
  return new Date(d.getTime() - offsetMs).toISOString().slice(0, 16);
}

function toIsoFromDatetimeLocal(localValue: string): string | null {
  const d = new Date(localValue);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

function normalizeInputMap(source: Record<string, unknown> | null | undefined): Record<string, string> {
  const out: Record<string, string> = {};
  if (!source) return out;
  for (const [rawKey, rawValue] of Object.entries(source)) {
    const key = rawKey.trim();
    if (!key) continue;
    const value = String(rawValue ?? "").trim();
    if (!value) continue;
    out[key] = value;
  }
  return out;
}

function summarizeInputs(inputs: Record<string, string>): string {
  const parts = Object.keys(inputs)
    .sort()
    .map((k) => `${k}: ${inputs[k]}`)
    .filter((line) => line.trim().length > 0);
  return parts.length > 0 ? parts.join(" | ") : "—";
}

function stripToStringMap(inputs: Record<string, string>): Record<string, string> {
  const out: Record<string, string> = {};
  for (const [rawKey, rawValue] of Object.entries(inputs)) {
    const key = rawKey.trim();
    if (!key) continue;
    const value = String(rawValue ?? "").trim();
    if (!value) continue;
    out[key] = value;
  }
  return out;
}

export function ScheduledFlows() {
  const { t } = useTranslation();
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [schedules, setSchedules] = useState<RunScheduleSummary[]>([]);
  const [executions, setExecutions] = useState<RunScheduleExecutionSummary[]>([]);
  const [flows, setFlows] = useState<FlowSummary[]>([]);
  const [flowFields, setFlowFields] = useState<Record<string, string[]>>({});
  const [deletingId, setDeletingId] = useState<string | null>(null);

  const [editorOpen, setEditorOpen] = useSessionBackedModalFlag(
    "scheduled-flows:editor-open",
  );
  const [editingScheduleId, setEditingScheduleId] = useSessionBackedState<string | null>(
    "scheduled-flows:editing-schedule-id",
    null,
    { isClosed: (value) => value === null },
  );
  const [formName, setFormName] = useSessionBackedState(
    "scheduled-flows:form-name",
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [formRunAt, setFormRunAt] = useSessionBackedState(
    "scheduled-flows:form-run-at",
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [formRunMode, setFormRunMode] = useSessionBackedState<(typeof RUN_MODES)[number]>(
    "scheduled-flows:form-run-mode",
    "serial",
    { isClosed: (value) => value === "serial" },
  );
  const [formExecuteMode, setFormExecuteMode] = useSessionBackedState<(typeof EXECUTE_MODES)[number]>(
    "scheduled-flows:form-execute-mode",
    "once",
    { isClosed: (value) => value === "once" },
  );
  const [formIntervalDays, setFormIntervalDays] = useSessionBackedState(
    "scheduled-flows:form-interval-days",
    "1",
    { isClosed: (value) => value === "1" },
  );
  const [formItems, setFormItems] = useSessionBackedState<EditableItem[]>(
    "scheduled-flows:form-items",
    [],
    { isClosed: (value) => value.length === 0 },
  );
  const [formError, setFormError] = useSessionBackedState<string | null>(
    "scheduled-flows:form-error",
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [formSaving, setFormSaving] = useState(false);

  const [itemEditor, setItemEditor] = useSessionBackedState<ItemEditorState | null>(
    "scheduled-flows:item-editor",
    null,
    { isClosed: (value) => value === null },
  );
  const [itemEditorError, setItemEditorError] = useSessionBackedState<string | null>(
    "scheduled-flows:item-editor-error",
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [itemEditorLoading, setItemEditorLoading] = useState(false);
  const [executionDetailId, setExecutionDetailId] = useSessionBackedState<string | null>(
    "scheduled-flows:execution-detail-id",
    null,
    { isClosed: (value) => value === null },
  );
  const [executionDetail, setExecutionDetail] = useSessionBackedState<RunScheduleExecutionDetail | null>(
    "scheduled-flows:execution-detail",
    null,
    { isClosed: (value) => value === null },
  );
  const [executionDetailError, setExecutionDetailError] = useSessionBackedState<string | null>(
    "scheduled-flows:execution-detail-error",
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [executionDetailLoading, setExecutionDetailLoading] = useState(false);

  const flowNameById = useMemo(() => {
    const out: Record<string, string> = {};
    for (const flow of flows) out[flow.id] = flow.name;
    return out;
  }, [flows]);

  async function loadPageData(opts?: { silent?: boolean }) {
    const silent = opts?.silent ?? false;
    if (!silent) {
      setError(null);
      setLoading(true);
    }
    try {
      const [scheduleResp, executionResp, flowResp] = await Promise.all([
        api.listRunSchedules(),
        api.listRunScheduleExecutions({ limit: 100 }),
        api.listFlows(),
      ]);
      setSchedules(scheduleResp.items);
      setExecutions(executionResp.items);
      setFlows(flowResp.items);
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => {
    void loadPageData();
    const tid = setInterval(() => {
      void loadPageData({ silent: true });
    }, 5000);
    return () => clearInterval(tid);
  }, []);

  async function loadFlowFields(flowId: string): Promise<string[]> {
    if (!flowId.trim()) return [];
    const cached = flowFields[flowId];
    if (cached) return cached;
    const flow = await api.getFlow(flowId);
    const fields = getRunInputFields(flow.spec);
    setFlowFields((prev) => ({ ...prev, [flowId]: fields }));
    return fields;
  }

  function resetFormForCreate() {
    setEditingScheduleId(null);
    setFormName("");
    setFormRunAt("");
    setFormRunMode("serial");
    setFormExecuteMode("once");
    setFormIntervalDays("1");
    setFormItems([]);
    setFormError(null);
    setEditorOpen(true);
  }

  function openEditSchedule(schedule: RunScheduleSummary) {
    setEditingScheduleId(schedule.id);
    setFormName(schedule.name);
    setFormRunAt(toDatetimeLocal(schedule.nextRunAt));
    setFormRunMode(schedule.runMode);
    setFormExecuteMode(schedule.executeMode);
    setFormIntervalDays(String(schedule.intervalDays ?? 1));
    setFormItems(
      schedule.items.map((item) => ({
        flowId: item.flowId,
        inputs: normalizeInputMap(item.inputs),
      })),
    );
    setFormError(null);
    setEditorOpen(true);
  }

  async function openItemEditor(index: number) {
    const current = formItems[index];
    if (!current) return;
    setItemEditorError(null);
    setItemEditorLoading(true);
    try {
      const requiredFields = await loadFlowFields(current.flowId);
      const normalizedInputs: Record<string, string> = {};
      for (const field of requiredFields) {
        normalizedInputs[field] = current.inputs[field] ?? "";
      }
      setItemEditor({
        mode: "edit",
        index,
        flowId: current.flowId,
        inputs: requiredFields.length > 0 ? normalizedInputs : { ...current.inputs },
        requiredFields,
      });
    } catch (e) {
      setItemEditorError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setItemEditorLoading(false);
    }
  }

  async function addFlowItem() {
    if (flows.length === 0) {
      setFormError(t("scheduledFlows.errors.noFlowOptions"));
      return;
    }
    const defaultFlowId = flows[0].id;
    setItemEditorLoading(true);
    setItemEditorError(null);
    try {
      const requiredFields = await loadFlowFields(defaultFlowId);
      const initialInputs: Record<string, string> = {};
      for (const field of requiredFields) initialInputs[field] = "";
      setItemEditor({
        mode: "create",
        index: null,
        flowId: defaultFlowId,
        inputs: initialInputs,
        requiredFields,
      });
    } catch (e) {
      setFormError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setItemEditorLoading(false);
    }
  }

  function moveItem(index: number, direction: -1 | 1) {
    const target = index + direction;
    if (target < 0 || target >= formItems.length) return;
    setFormItems((prev) => {
      const next = prev.slice();
      const temp = next[index];
      next[index] = next[target];
      next[target] = temp;
      return next;
    });
  }

  function removeItem(index: number) {
    setFormItems((prev) => prev.filter((_, i) => i !== index));
  }

  async function onItemFlowChange(flowId: string) {
    if (!itemEditor) return;
    setItemEditorLoading(true);
    setItemEditorError(null);
    try {
      const requiredFields = await loadFlowFields(flowId);
      const nextInputs: Record<string, string> = {};
      for (const field of requiredFields) {
        nextInputs[field] = itemEditor.inputs[field] ?? "";
      }
      setItemEditor({
        ...itemEditor,
        flowId,
        requiredFields,
        inputs: requiredFields.length > 0 ? nextInputs : {},
      });
    } catch (e) {
      setItemEditorError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setItemEditorLoading(false);
    }
  }

  function onSaveItemEditor() {
    if (!itemEditor) return;
    if (!itemEditor.flowId.trim()) {
      setItemEditorError(t("scheduledFlows.errors.flowRequired"));
      return;
    }
    for (const field of itemEditor.requiredFields) {
      if (!String(itemEditor.inputs[field] ?? "").trim()) {
        setItemEditorError(t("scheduledFlows.errors.inputRequired", { field }));
        return;
      }
    }
    const nextItem: EditableItem = {
      flowId: itemEditor.flowId,
      inputs: stripToStringMap(itemEditor.inputs),
    };
    if (itemEditor.mode === "create" || itemEditor.index === null) {
      setFormItems((prev) => [...prev, nextItem]);
    } else {
      setFormItems((prev) =>
        prev.map((item, idx) =>
          idx === itemEditor.index
            ? nextItem
            : item,
        ),
      );
    }
    setItemEditor(null);
    setItemEditorError(null);
  }

  async function validateFormItems(items: EditableItem[]): Promise<void> {
    if (items.length === 0) {
      throw new Error(t("scheduledFlows.errors.itemsRequired"));
    }
    for (let idx = 0; idx < items.length; idx += 1) {
      const item = items[idx];
      if (!item.flowId.trim()) {
        throw new Error(t("scheduledFlows.errors.flowRequired"));
      }
      const required = await loadFlowFields(item.flowId);
      for (const field of required) {
        const value = String(item.inputs[field] ?? "").trim();
        if (!value) {
          throw new Error(t("scheduledFlows.errors.itemMissingField", { index: idx + 1, field }));
        }
      }
    }
  }

  async function onSaveSchedule() {
    if (formSaving) return;
    setFormError(null);
    const name = formName.trim();
    if (!name) {
      setFormError(t("scheduledFlows.errors.nameRequired"));
      return;
    }
    const runAtIso = toIsoFromDatetimeLocal(formRunAt);
    if (!runAtIso) {
      setFormError(t("scheduledFlows.errors.runAtRequired"));
      return;
    }
    if (formExecuteMode === "recurring" && Number(formIntervalDays) < 1) {
      setFormError(t("scheduledFlows.errors.intervalRequired"));
      return;
    }
    try {
      await validateFormItems(formItems);
    } catch (e) {
      setFormError(e instanceof Error ? e.message : String(e));
      return;
    }

    setFormSaving(true);
    try {
      const payload = {
        name,
        runMode: formRunMode,
        executeMode: formExecuteMode,
        intervalDays: formExecuteMode === "recurring" ? Number(formIntervalDays) : null,
        runAt: runAtIso,
        items: formItems.map((item) => ({
          flowId: item.flowId,
          inputs: normalizeInputMap(item.inputs),
        })),
      };
      if (editingScheduleId) {
        await api.updateRunSchedule(editingScheduleId, payload);
      } else {
        await api.createRunSchedule(payload);
      }
      setEditorOpen(false);
      await loadPageData({ silent: true });
    } catch (e) {
      setFormError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setFormSaving(false);
    }
  }

  async function onDeleteSchedule(scheduleId: string) {
    if (deletingId) return;
    setDeletingId(scheduleId);
    setError(null);
    try {
      await api.deleteRunSchedule(scheduleId);
      await loadPageData({ silent: true });
    } catch (e) {
      setError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setDeletingId(null);
    }
  }

  function executionStatusLabel(status: string): string {
    if (status === "succeeded") return t("scheduledFlows.execution.status.succeeded");
    if (status === "partial_failed") return t("scheduledFlows.execution.status.partialFailed");
    if (status === "failed") return t("scheduledFlows.execution.status.failed");
    return status;
  }

  async function openExecutionDetail(executionId: string) {
    setExecutionDetailId(executionId);
    setExecutionDetailLoading(true);
    setExecutionDetailError(null);
    setExecutionDetail(null);
    try {
      const detail = await api.getRunScheduleExecution(executionId);
      setExecutionDetail(detail);
    } catch (e) {
      setExecutionDetailError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setExecutionDetailLoading(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-center justify-between">
        <h1 className="text-xl font-semibold text-ink-900">{t("scheduledFlows.title")}</h1>
        <button type="button" className="btn-primary" onClick={resetFormForCreate}>
          {t("scheduledFlows.createButton")}
        </button>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {loading && <Loading />}

      {!loading && schedules.length === 0 && (
        <EmptyState
          icon={<FlowIcon className="h-10 w-10" />}
          title={t("scheduledFlows.empty")}
          hint={t("scheduledFlows.emptyHint")}
          action={
            <button type="button" className="btn-primary" onClick={resetFormForCreate}>
              {t("scheduledFlows.createButton")}
            </button>
          }
        />
      )}

      {!loading && schedules.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500">
              <tr>
                <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.name")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.nextRun")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.mode")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.flows")}</th>
                <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {schedules.map((schedule) => (
                <tr key={schedule.id} className="table-row">
                  <td className="px-4 py-3 font-medium text-ink-900">
                    {schedule.name || schedule.id}
                  </td>
                  <td className="px-4 py-3 text-ink-600">
                    {new Date(schedule.nextRunAt).toLocaleString()}
                  </td>
                  <td className="px-4 py-3 text-ink-600">
                    {t("scheduledFlows.modeSummary", {
                      runMode:
                        schedule.runMode === "parallel"
                          ? t("scheduledFlows.runMode.parallel")
                          : t("scheduledFlows.runMode.serial"),
                      executeMode:
                        schedule.executeMode === "once"
                          ? t("scheduledFlows.executeMode.once")
                          : t("scheduledFlows.executeMode.recurring"),
                      intervalText:
                        schedule.executeMode === "recurring"
                          ? t("scheduledFlows.intervalSuffix", { days: schedule.intervalDays ?? 1 })
                          : "",
                    })}
                  </td>
                  <td className="px-4 py-3 text-ink-600">{schedule.items.length}</td>
                  <td className="px-4 py-3">
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        className="btn-outline"
                        onClick={() => openEditSchedule(schedule)}
                      >
                        {t("common.edit")}
                      </button>
                      <button
                        type="button"
                        className="btn-outline"
                        onClick={() => void onDeleteSchedule(schedule.id)}
                        disabled={deletingId === schedule.id}
                      >
                        {deletingId === schedule.id
                          ? t("scheduledFlows.deleting")
                          : t("common.delete")}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {!loading && (
        <Card className="space-y-2">
          <div className="text-base font-semibold text-ink-900">
            {t("scheduledFlows.execution.title")}
          </div>
          {executions.length === 0 ? (
            <div className="text-xs text-ink-500">{t("scheduledFlows.execution.empty")}</div>
          ) : (
            <Card className="p-0 overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-ink-50 text-ink-500">
                  <tr>
                    <th className="text-left px-4 py-2 font-medium">
                      {t("scheduledFlows.execution.columns.schedule")}
                    </th>
                    <th className="text-left px-4 py-2 font-medium">
                      {t("scheduledFlows.execution.columns.startedAt")}
                    </th>
                    <th className="text-left px-4 py-2 font-medium">
                      {t("scheduledFlows.execution.columns.result")}
                    </th>
                    <th className="text-left px-4 py-2 font-medium">
                      {t("scheduledFlows.execution.columns.failed")}
                    </th>
                    <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {executions.map((execution) => (
                    <tr key={execution.id} className="table-row">
                      <td className="px-4 py-3 text-ink-900">
                        {execution.scheduleName || execution.scheduleId}
                      </td>
                      <td className="px-4 py-3 text-ink-600">
                        {new Date(execution.startedAt).toLocaleString()}
                      </td>
                      <td className="px-4 py-3 text-ink-600">
                        {executionStatusLabel(execution.status)}
                      </td>
                      <td className="px-4 py-3 text-ink-600">
                        {execution.failedItems}/{execution.totalItems}
                      </td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end">
                          <button
                            type="button"
                            className="btn-outline"
                            onClick={() => void openExecutionDetail(execution.id)}
                          >
                            {t("scheduledFlows.execution.viewDetail")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Card>
          )}
        </Card>
      )}

      <Modal
        open={editorOpen}
        onClose={() => {
          if (formSaving) return;
          setEditorOpen(false);
          setFormError(null);
        }}
        title={editingScheduleId ? t("scheduledFlows.editTitle") : t("scheduledFlows.createTitle")}
        width="max-w-3xl"
      >
        <div className="space-y-4">
          <div className="grid gap-3 md:grid-cols-2">
            <div>
              <label className="label">{t("scheduledFlows.form.nameLabel")}</label>
              <input
                className="input"
                value={formName}
                onChange={(e) => setFormName(e.target.value)}
                placeholder={t("scheduledFlows.form.namePlaceholder")}
                disabled={formSaving}
              />
            </div>
            <div>
              <label className="label">{t("scheduledFlows.form.runAtLabel")}</label>
              <input
                type="datetime-local"
                className="input"
                value={formRunAt}
                onChange={(e) => setFormRunAt(e.target.value)}
                disabled={formSaving}
              />
            </div>
            <div>
              <label className="label">{t("scheduledFlows.form.runModeLabel")}</label>
              <select
                className="select"
                value={formRunMode}
                onChange={(e) => setFormRunMode(e.target.value as (typeof RUN_MODES)[number])}
                disabled={formSaving}
              >
                <option value="parallel">{t("scheduledFlows.runMode.parallel")}</option>
                <option value="serial">{t("scheduledFlows.runMode.serial")}</option>
              </select>
            </div>
            <div>
              <label className="label">{t("scheduledFlows.form.executeModeLabel")}</label>
              <select
                className="select"
                value={formExecuteMode}
                onChange={(e) =>
                  setFormExecuteMode(e.target.value as (typeof EXECUTE_MODES)[number])
                }
                disabled={formSaving}
              >
                <option value="once">{t("scheduledFlows.executeMode.once")}</option>
                <option value="recurring">{t("scheduledFlows.executeMode.recurring")}</option>
              </select>
            </div>
            {formExecuteMode === "recurring" && (
              <div>
                <label className="label">{t("scheduledFlows.form.intervalDaysLabel")}</label>
                <input
                  type="number"
                  min={1}
                  className="input"
                  value={formIntervalDays}
                  onChange={(e) => setFormIntervalDays(e.target.value)}
                  disabled={formSaving}
                />
              </div>
            )}
          </div>

          <div className="space-y-2">
            <div className="flex items-center justify-between">
              <div className="text-sm font-semibold text-ink-900">
                {t("scheduledFlows.form.flowListTitle")}
              </div>
              <button type="button" className="btn-outline" onClick={() => void addFlowItem()}>
                {t("scheduledFlows.form.addFlow")}
              </button>
            </div>
            <Card className="p-0 overflow-hidden">
              <table className="w-full text-sm">
                <thead className="bg-ink-50 text-ink-500">
                  <tr>
                    <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.flow")}</th>
                    <th className="text-left px-4 py-2 font-medium">{t("scheduledFlows.columns.inputs")}</th>
                    <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
                  </tr>
                </thead>
                <tbody>
                  {formItems.map((item, idx) => (
                    <tr key={`editable-item-${idx}`} className="table-row">
                      <td className="px-4 py-3 text-ink-900">
                        {flowNameById[item.flowId] || item.flowId}
                      </td>
                      <td className="px-4 py-3 text-ink-600">{summarizeInputs(item.inputs)}</td>
                      <td className="px-4 py-3">
                        <div className="flex justify-end gap-2">
                          <button
                            type="button"
                            className="btn-outline"
                            onClick={() => void openItemEditor(idx)}
                          >
                            {t("common.edit")}
                          </button>
                          <button
                            type="button"
                            className="btn-outline"
                            onClick={() => moveItem(idx, -1)}
                            disabled={idx === 0}
                          >
                            {t("scheduledFlows.form.moveUp")}
                          </button>
                          <button
                            type="button"
                            className="btn-outline"
                            onClick={() => moveItem(idx, 1)}
                            disabled={idx === formItems.length - 1}
                          >
                            {t("scheduledFlows.form.moveDown")}
                          </button>
                          <button
                            type="button"
                            className="btn-outline"
                            onClick={() => removeItem(idx)}
                          >
                            {t("common.delete")}
                          </button>
                        </div>
                      </td>
                    </tr>
                  ))}
                  {formItems.length === 0 && (
                    <tr>
                      <td colSpan={3} className="px-4 py-3 text-xs text-ink-500">
                        {t("scheduledFlows.form.flowListEmpty")}
                      </td>
                    </tr>
                  )}
                </tbody>
              </table>
            </Card>
          </div>

          {formError && <ErrorBox>{formError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (formSaving) return;
                setEditorOpen(false);
                setFormError(null);
              }}
              disabled={formSaving}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSaveSchedule()}
              disabled={formSaving}
            >
              {formSaving ? t("scheduledFlows.saving") : t("common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={!!itemEditor}
        onClose={() => {
          if (itemEditorLoading) return;
          setItemEditor(null);
          setItemEditorError(null);
        }}
        title={t("scheduledFlows.itemEditor.title")}
      >
        <div className="space-y-3">
          {!itemEditor && <Loading />}
          {itemEditor && (
            <>
              <div>
                <label className="label">{t("scheduledFlows.itemEditor.flowLabel")}</label>
                <select
                  className="select"
                  value={itemEditor.flowId}
                  onChange={(e) => void onItemFlowChange(e.target.value)}
                  disabled={itemEditorLoading}
                >
                  <option value="">{t("scheduledFlows.itemEditor.flowPlaceholder")}</option>
                  {flows.map((flow) => (
                    <option key={flow.id} value={flow.id}>
                      {flow.name} ({flow.id})
                    </option>
                  ))}
                </select>
              </div>

              {itemEditor.requiredFields.length === 0 ? (
                <div className="text-xs text-ink-500">{t("scheduledFlows.itemEditor.noFields")}</div>
              ) : (
                <div className="space-y-2">
                  {itemEditor.requiredFields.map((field) => (
                    <div key={field}>
                      <label className="label">{field} *</label>
                      <input
                        className="input"
                        value={itemEditor.inputs[field] ?? ""}
                        onChange={(e) =>
                          setItemEditor((prev) =>
                            prev
                              ? {
                                  ...prev,
                                  inputs: {
                                    ...prev.inputs,
                                    [field]: e.target.value,
                                  },
                                }
                              : prev,
                          )
                        }
                        disabled={itemEditorLoading}
                      />
                    </div>
                  ))}
                </div>
              )}
            </>
          )}

          {itemEditorError && <ErrorBox>{itemEditorError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (itemEditorLoading) return;
                setItemEditor(null);
                setItemEditorError(null);
              }}
              disabled={itemEditorLoading}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={onSaveItemEditor}
              disabled={itemEditorLoading}
            >
              {t("common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={!!executionDetailId}
        onClose={() => {
          if (executionDetailLoading) return;
          setExecutionDetailId(null);
          setExecutionDetail(null);
          setExecutionDetailError(null);
        }}
        title={t("scheduledFlows.execution.detailTitle")}
        width="max-w-3xl"
      >
        <div className="space-y-3">
          {executionDetailLoading && <Loading />}
          {executionDetailError && <ErrorBox>{executionDetailError}</ErrorBox>}
          {executionDetail && (
            <>
              <div className="text-xs text-ink-500">
                {t("scheduledFlows.execution.detailSummary", {
                  status: executionStatusLabel(executionDetail.status),
                  startedAt: new Date(executionDetail.startedAt).toLocaleString(),
                  finishedAt: executionDetail.finishedAt
                    ? new Date(executionDetail.finishedAt).toLocaleString()
                    : "—",
                })}
              </div>
              <Card className="p-0 overflow-hidden">
                <table className="w-full text-sm">
                  <thead className="bg-ink-50 text-ink-500">
                    <tr>
                      <th className="text-left px-4 py-2 font-medium">
                        {t("scheduledFlows.execution.columns.flow")}
                      </th>
                      <th className="text-left px-4 py-2 font-medium">
                        {t("scheduledFlows.execution.columns.itemStatus")}
                      </th>
                      <th className="text-left px-4 py-2 font-medium">
                        {t("scheduledFlows.execution.columns.reason")}
                      </th>
                    </tr>
                  </thead>
                  <tbody>
                    {executionDetail.itemResults.map((item) => (
                      <tr key={`${executionDetail.id}-${item.index}-${item.flowId}`} className="table-row">
                        <td className="px-4 py-3 text-ink-900">
                          {item.flowName || flowNameById[item.flowId] || item.flowId || "—"}
                        </td>
                        <td className="px-4 py-3 text-ink-600">
                          {executionStatusLabel(item.status)}
                          {item.runId
                            ? t("scheduledFlows.execution.itemRunLabel", {
                              runId: item.runId,
                            })
                            : ""}
                        </td>
                        <td className="px-4 py-3 text-ink-600">{item.reason || "—"}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </Card>
            </>
          )}
          <div className="flex justify-end">
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                if (executionDetailLoading) return;
                setExecutionDetailId(null);
                setExecutionDetail(null);
                setExecutionDetailError(null);
              }}
            >
              {t("common.close")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

