/**
 * Profiles page — list + friendly create/edit UX.
 *
 * Every save goes through ``POST /api/profiles/{name}`` which the backend
 * proxies to ``clawteam profile set ...``; deletes invoke ``clawteam
 * profile remove``. Form fields mirror the CLI flags one-for-one so the
 * persisted profile matches what a power-user would have typed manually.
 *
 * The editor splits fields into three sections to reduce visual load:
 * Basics, Network & Auth, Advanced. Lists (envs / env-maps / args) get a
 * lightweight row editor with add/remove buttons.
 */

import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  ProfileSetPayload,
  ProfileSummary,
  api,
} from "@/lib/api";
import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  Modal,
} from "@/components/ui";
import { SettingsIcon } from "@/components/icons";
import { useSessionBackedState } from "@/lib/sessionState";

const AGENT_CHOICES = [
  "claude",
  "codex",
  "cursor",
  "gemini",
  "kimi",
  "nanobot",
  "openclaw",
  "qwen",
  "opencode",
  "pi",
  "qoder",
  "codebuddy",
  "custom",
];

interface FormState {
  agent: string;
  description: string;
  command: string;
  model: string;
  baseUrl: string;
  baseUrlEnv: string;
  apiKeyEnv: string;
  apiKeyTargetEnv: string;
  envs: string[];
  envMaps: string[];
  args: string[];
}

const EMPTY_FORM: FormState = {
  agent: "",
  description: "",
  command: "",
  model: "",
  baseUrl: "",
  baseUrlEnv: "",
  apiKeyEnv: "",
  apiKeyTargetEnv: "",
  envs: [],
  envMaps: [],
  args: [],
};

function rawToForm(raw: Record<string, unknown>): FormState {
  const str = (k: string) => {
    const v = raw[k];
    return typeof v === "string" ? v : "";
  };
  const list = (k: string) => {
    const v = raw[k];
    return Array.isArray(v) ? v.map((x) => String(x)) : [];
  };
  // ClawTeam stores `env` as either KEY=VALUE strings or {KEY: VALUE} map;
  // tolerate both so we can round-trip what the CLI wrote.
  const envField = raw["env"];
  let envs: string[] = [];
  if (Array.isArray(envField)) {
    envs = envField.map((x) => String(x));
  } else if (envField && typeof envField === "object") {
    envs = Object.entries(envField as Record<string, unknown>).map(
      ([k, v]) => `${k}=${v}`,
    );
  }
  const mapField = raw["env_map"] ?? raw["envMap"];
  let envMaps: string[] = [];
  if (Array.isArray(mapField)) {
    envMaps = mapField.map((x) => String(x));
  } else if (mapField && typeof mapField === "object") {
    envMaps = Object.entries(mapField as Record<string, unknown>).map(
      ([k, v]) => `${k}=${v}`,
    );
  }
  return {
    agent: str("agent"),
    description: str("description"),
    command: str("command"),
    model: str("model"),
    baseUrl: str("base_url") || str("baseUrl"),
    baseUrlEnv: str("base_url_env") || str("baseUrlEnv"),
    apiKeyEnv: str("api_key_env") || str("apiKeyEnv"),
    apiKeyTargetEnv:
      str("api_key_target_env") || str("apiKeyTargetEnv"),
    envs,
    envMaps,
    args: list("args"),
  };
}

function formToPayload(f: FormState): ProfileSetPayload {
  // Drop empty strings so the backend gets `null` and the CLI's "leave
  // as-is" semantics kick in. Lists are always sent so add/remove takes
  // effect (an empty list still wipes any existing entries).
  const opt = (s: string) => (s.trim() ? s : null);
  return {
    agent: opt(f.agent),
    description: opt(f.description),
    command: opt(f.command),
    model: opt(f.model),
    baseUrl: opt(f.baseUrl),
    baseUrlEnv: opt(f.baseUrlEnv),
    apiKeyEnv: opt(f.apiKeyEnv),
    apiKeyTargetEnv: opt(f.apiKeyTargetEnv),
    envs: f.envs.map((s) => s.trim()).filter(Boolean),
    envMaps: f.envMaps.map((s) => s.trim()).filter(Boolean),
    args: f.args.map((s) => s.trim()).filter(Boolean),
  };
}


export function Profiles() {
  const { t } = useTranslation();
  const [items, setItems] = useState<ProfileSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [showing, setShowing] = useSessionBackedState<string | null>(
    "profiles:showing",
    null,
    { isClosed: (value) => value === null },
  );
  const [showingDetail, setShowingDetail] = useSessionBackedState<Record<string, unknown> | null>(
    "profiles:showing-detail",
    null,
    { isClosed: (value) => value === null },
  );
  const [testing, setTesting] = useState<string | null>(null);
  const [testOutput, setTestOutput] = useSessionBackedState<{
    name: string;
    success: boolean;
    output: string;
  } | null>("profiles:test-output", null, { isClosed: (value) => value === null });
  const [editorOpen, setEditorOpen] = useSessionBackedState<
    | { mode: "create" }
    | { mode: "edit"; name: string; initial: FormState }
    | null
  >("profiles:editor-open", null, { isClosed: (value) => value === null });
  const [pendingDelete, setPendingDelete] = useSessionBackedState<ProfileSummary | null>(
    "profiles:pending-delete",
    null,
    { isClosed: (value) => value === null },
  );
  const [deleting, setDeleting] = useState(false);
  const [deleteError, setDeleteError] = useState<string | null>(null);

  async function load() {
    try {
      const r = await api.listProfiles();
      setItems(r.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }
  useEffect(() => {
    load();
  }, []);

  async function showProfile(name: string) {
    setShowing(name);
    setShowingDetail(null);
    try {
      const r = await api.getProfile(name);
      setShowingDetail(r.raw);
    } catch (e) {
      setShowingDetail({
        error: e instanceof ApiError ? e.message : String(e),
      });
    }
  }

  useEffect(() => {
    if (!showing || showingDetail) return;
    void showProfile(showing);
  }, [showing, showingDetail]);

  async function openEditFor(name: string) {
    try {
      const r = await api.getProfile(name);
      setEditorOpen({ mode: "edit", name, initial: rawToForm(r.raw) });
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }

  async function testProfile(name: string) {
    setTesting(name);
    setTestOutput(null);
    try {
      const r = await api.testProfile(name);
      setTestOutput(r);
    } catch (e) {
      setTestOutput({
        name,
        success: false,
        output: e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
      });
    } finally {
      setTesting(null);
    }
  }

  async function confirmDelete() {
    if (!pendingDelete) return;
    setDeleting(true);
    setDeleteError(null);
    try {
      await api.removeProfile(pendingDelete.name);
      setPendingDelete(null);
      await load();
    } catch (e) {
      setDeleteError(
        t("profiles.deleteFailed", {
          message:
            e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
        }),
      );
    } finally {
      setDeleting(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between">
        <div>
          <h1 className="text-xl font-semibold text-ink-900">{t("profiles.title")}</h1>
          <p className="text-sm text-ink-500">{t("profiles.hint")}</p>
        </div>
        <button
          className="btn-primary"
          onClick={() => setEditorOpen({ mode: "create" })}
        >
          {t("profiles.new")}
        </button>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}
      {items && items.length === 0 && (
        <EmptyState
          icon={<SettingsIcon className="h-10 w-10" />}
          title={t("profiles.empty")}
          hint={t("profiles.emptyHint")}
          action={
            <button
              className="btn-primary"
              onClick={() => setEditorOpen({ mode: "create" })}
            >
              {t("profiles.new")}
            </button>
          }
        />
      )}

      {items && items.length > 0 && (
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500">
              <tr>
                <th className="text-left px-4 py-2 font-medium">{t("profiles.columnId")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("profiles.columnProvider")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("profiles.columnModel")}</th>
                <th className="text-left px-4 py-2 font-medium">
                  {t("profiles.columnBaseUrl")}
                </th>
                <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((p) => (
                <tr key={p.name} className="table-row align-top">
                  <td className="px-4 py-3">
                    <button
                      onClick={() => showProfile(p.name)}
                      className="font-medium text-ink-900 hover:text-brand-600"
                    >
                      {p.name}
                    </button>
                    {p.description && (
                      <div className="text-xs text-ink-500 mt-0.5 line-clamp-1">
                        {p.description}
                      </div>
                    )}
                  </td>
                  <td className="px-4 py-3 text-ink-700">{p.agent ?? "—"}</td>
                  <td className="px-4 py-3 text-ink-700">{p.model ?? "—"}</td>
                  <td className="px-4 py-3 text-ink-500 font-mono text-xs break-all">
                    {p.baseUrl ?? "—"}
                  </td>
                  <td className="px-4 py-3 align-middle">
                    <div className="flex items-center justify-end gap-2 whitespace-nowrap">
                      <button className="btn-outline" onClick={() => showProfile(p.name)}>
                        {t("profiles.show")}
                      </button>
                      <button
                        className="btn-outline"
                        onClick={() => openEditFor(p.name)}
                      >
                        {t("profiles.edit")}
                      </button>
                      <button
                        className="btn-primary"
                        onClick={() => testProfile(p.name)}
                        disabled={testing === p.name}
                      >
                        {testing === p.name ? t("profiles.testing") : t("profiles.test")}
                      </button>
                      <button
                        className="btn-danger"
                        onClick={() => {
                          setDeleteError(null);
                          setPendingDelete(p);
                        }}
                      >
                        {t("profiles.delete")}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Modal
        open={!!showing}
        onClose={() => setShowing(null)}
        title={t("profiles.showTitle", { id: showing ?? "" })}
        width="max-w-2xl"
      >
        {!showingDetail && <Loading />}
        {showingDetail && (
          <pre className="text-xs bg-ink-900 text-ink-100 rounded-md p-3 overflow-auto max-h-96">
            {JSON.stringify(showingDetail, null, 2)}
          </pre>
        )}
      </Modal>

      <Modal
        open={!!testOutput}
        onClose={() => setTestOutput(null)}
        title={`${t("profiles.test")}: ${testOutput?.name ?? ""}`}
        width="max-w-2xl"
      >
        {testOutput && (
          <div className="space-y-3">
            <div>
              {testOutput.success ? (
                <span className="pill-success">{t("common.success")}</span>
              ) : (
                <span className="pill-danger">{t("common.failed")}</span>
              )}
            </div>
            <pre className="text-xs bg-ink-900 text-ink-100 rounded-md p-3 overflow-auto max-h-96 whitespace-pre-wrap">
              {testOutput.output || t("common.none")}
            </pre>
          </div>
        )}
      </Modal>

      {editorOpen && (
        <ProfileEditorModal
          state={editorOpen}
          onClose={() => setEditorOpen(null)}
          onSaved={async () => {
            setEditorOpen(null);
            await load();
          }}
        />
      )}

      <Modal
        open={!!pendingDelete}
        onClose={() => {
          if (deleting) return;
          setPendingDelete(null);
          setDeleteError(null);
        }}
        title={t("profiles.deleteModalTitle")}
        width="max-w-md"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-700">
            {pendingDelete &&
              t("profiles.deleteModalConfirm", { name: pendingDelete.name })}
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
              {deleting ? t("profiles.deleting") : t("profiles.deleteModalOk")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}


// ──────────────────────────────────────────────────────────────────────
// Editor modal
// ──────────────────────────────────────────────────────────────────────


function ProfileEditorModal({
  state,
  onClose,
  onSaved,
}: {
  state:
    | { mode: "create" }
    | { mode: "edit"; name: string; initial: FormState };
  onClose: () => void;
  onSaved: () => Promise<void> | void;
}) {
  const { t } = useTranslation();
  const isCreate = state.mode === "create";
  const [name, setName] = useState(isCreate ? "" : state.name);
  const [form, setForm] = useState<FormState>(
    isCreate ? EMPTY_FORM : state.initial,
  );
  const [saving, setSaving] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  function patch(p: Partial<FormState>) {
    setForm((f) => ({ ...f, ...p }));
  }

  function localValidate(): string | null {
    if (isCreate) {
      if (!name.trim()) return t("profiles.editor.nameRequired");
      if (!/^[A-Za-z0-9_-]+$/.test(name.trim()))
        return t("profiles.editor.nameInvalid");
    }
    for (const s of form.envs) {
      if (s.trim() && (!s.includes("=") || !s.split("=", 1)[0].trim())) {
        return t("profiles.editor.kvFormatError", { label: t("profiles.editor.envs") });
      }
    }
    for (const s of form.envMaps) {
      if (s.trim() && (!s.includes("=") || !s.split("=", 1)[0].trim())) {
        return t("profiles.editor.kvFormatError", {
          label: t("profiles.editor.envMaps"),
        });
      }
    }
    return null;
  }

  async function onSave() {
    const v = localValidate();
    if (v) {
      setErr(v);
      return;
    }
    setErr(null);
    setSaving(true);
    try {
      await api.setProfile(name.trim(), formToPayload(form));
      await onSaved();
    } catch (e) {
      setErr(
        t("profiles.editor.saveError", {
          message:
            e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
        }),
      );
    } finally {
      setSaving(false);
    }
  }

  const title = useMemo(
    () =>
      isCreate
        ? t("profiles.editor.titleNew")
        : t("profiles.editor.titleEdit", { name: state.mode === "edit" ? state.name : "" }),
    [isCreate, state, t],
  );

  return (
    <Modal open={true} onClose={onClose} title={title} width="max-w-3xl">
      <div className="space-y-5">
        {/* ── Basics ─────────────────────────────────────────── */}
        <Section title={t("profiles.editor.sectionBasic")}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field
              label={`${t("profiles.editor.name")} *`}
              hint={t("profiles.editor.nameHint")}
            >
              <input
                className="input"
                value={name}
                placeholder={t("profiles.editor.namePlaceholder")}
                readOnly={!isCreate}
                onChange={(e) => setName(e.target.value)}
              />
            </Field>
            <Field
              label={t("profiles.editor.agent")}
              hint={t("profiles.editor.agentHint")}
            >
              <select
                className="select"
                value={form.agent}
                onChange={(e) => patch({ agent: e.target.value })}
              >
                <option value="">—</option>
                {AGENT_CHOICES.map((a) => (
                  <option key={a} value={a}>
                    {a}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("profiles.editor.model")} className="md:col-span-2">
              <input
                className="input"
                value={form.model}
                placeholder={t("profiles.editor.modelPlaceholder")}
                onChange={(e) => patch({ model: e.target.value })}
              />
            </Field>
            <Field label={t("profiles.editor.description")} className="md:col-span-2">
              <input
                className="input"
                value={form.description}
                placeholder={t("profiles.editor.descriptionPlaceholder")}
                onChange={(e) => patch({ description: e.target.value })}
              />
            </Field>
          </div>
        </Section>

        {/* ── Network & auth ─────────────────────────────────── */}
        <Section title={t("profiles.editor.sectionNetwork")}>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            <Field label={t("profiles.editor.baseUrl")}>
              <input
                className="input"
                value={form.baseUrl}
                placeholder={t("profiles.editor.baseUrlPlaceholder")}
                onChange={(e) => patch({ baseUrl: e.target.value })}
              />
            </Field>
            <Field label={t("profiles.editor.baseUrlEnv")}>
              <input
                className="input"
                value={form.baseUrlEnv}
                placeholder={t("profiles.editor.baseUrlEnvPlaceholder")}
                onChange={(e) => patch({ baseUrlEnv: e.target.value })}
              />
            </Field>
            <Field label={t("profiles.editor.apiKeyEnv")}>
              <input
                className="input"
                value={form.apiKeyEnv}
                placeholder={t("profiles.editor.apiKeyEnvPlaceholder")}
                onChange={(e) => patch({ apiKeyEnv: e.target.value })}
              />
            </Field>
            <Field label={t("profiles.editor.apiKeyTargetEnv")}>
              <input
                className="input"
                value={form.apiKeyTargetEnv}
                placeholder={t("profiles.editor.apiKeyTargetEnvPlaceholder")}
                onChange={(e) => patch({ apiKeyTargetEnv: e.target.value })}
              />
            </Field>
          </div>
        </Section>

        {/* ── Advanced ───────────────────────────────────────── */}
        <Section title={t("profiles.editor.sectionAdvanced")}>
          <Field
            label={t("profiles.editor.command")}
            hint={t("profiles.editor.commandHint")}
          >
            <input
              className="input"
              value={form.command}
              placeholder={t("profiles.editor.commandPlaceholder")}
              onChange={(e) => patch({ command: e.target.value })}
            />
          </Field>
          <ListEditor
            label={t("profiles.editor.envs")}
            hint={t("profiles.editor.envsHint")}
            placeholder={t("profiles.editor.placeholderKv")}
            values={form.envs}
            onChange={(envs) => patch({ envs })}
            addLabel={t("profiles.editor.addItem")}
            removeLabel={t("profiles.editor.removeItem")}
          />
          <ListEditor
            label={t("profiles.editor.envMaps")}
            hint={t("profiles.editor.envMapsHint")}
            placeholder={t("profiles.editor.placeholderEnvMap")}
            values={form.envMaps}
            onChange={(envMaps) => patch({ envMaps })}
            addLabel={t("profiles.editor.addItem")}
            removeLabel={t("profiles.editor.removeItem")}
          />
          <ListEditor
            label={t("profiles.editor.args")}
            hint={t("profiles.editor.argsHint")}
            placeholder={t("profiles.editor.placeholderArg")}
            values={form.args}
            onChange={(args) => patch({ args })}
            addLabel={t("profiles.editor.addItem")}
            removeLabel={t("profiles.editor.removeItem")}
          />
        </Section>

        {err && <ErrorBox>{err}</ErrorBox>}
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            className="btn-outline"
            onClick={onClose}
            disabled={saving}
          >
            {t("profiles.editor.cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onSave}
            disabled={saving}
          >
            {saving ? t("profiles.editor.saving") : t("profiles.editor.save")}
          </button>
        </div>
      </div>
    </Modal>
  );
}


function Section({
  title,
  children,
}: {
  title: string;
  children: React.ReactNode;
}) {
  return (
    <div className="space-y-3">
      <div className="text-[11px] uppercase tracking-wider font-semibold text-ink-400 border-b border-ink-100 pb-1">
        {title}
      </div>
      <div className="space-y-3">{children}</div>
    </div>
  );
}


function Field({
  label,
  hint,
  className,
  children,
}: {
  label: string;
  hint?: string;
  className?: string;
  children: React.ReactNode;
}) {
  return (
    <div className={className}>
      <label className="label">{label}</label>
      {children}
      {hint && <div className="text-xs text-ink-500 mt-1">{hint}</div>}
    </div>
  );
}


function ListEditor({
  label,
  hint,
  placeholder,
  values,
  onChange,
  addLabel,
  removeLabel,
}: {
  label: string;
  hint?: string;
  placeholder: string;
  values: string[];
  onChange: (next: string[]) => void;
  addLabel: string;
  removeLabel: string;
}) {
  function setAt(i: number, v: string) {
    const next = values.slice();
    next[i] = v;
    onChange(next);
  }
  function removeAt(i: number) {
    onChange(values.filter((_, j) => j !== i));
  }
  function add() {
    onChange([...values, ""]);
  }
  return (
    <div>
      <label className="label">{label}</label>
      <div className="space-y-2">
        {values.length === 0 && (
          <div className="text-xs text-ink-400">—</div>
        )}
        {values.map((v, i) => (
          <div key={i} className="flex gap-2">
            <input
              className="input flex-1 font-mono text-xs"
              value={v}
              placeholder={placeholder}
              onChange={(e) => setAt(i, e.target.value)}
            />
            <button
              type="button"
              className="btn-outline"
              onClick={() => removeAt(i)}
            >
              {removeLabel}
            </button>
          </div>
        ))}
        <button type="button" className="btn-outline" onClick={add}>
          {addLabel}
        </button>
      </div>
      {hint && <div className="text-xs text-ink-500 mt-1">{hint}</div>}
    </div>
  );
}
