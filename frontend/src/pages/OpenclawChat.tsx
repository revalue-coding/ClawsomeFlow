/**
 * OpenClaw Chat — direct conversation with one user-managed OpenClaw agent.
 *
 * Per plan §6.1 / DEV.md §5.5: the chat session uses
 * ``user-chat-{user}-{agent_id}`` (server-derived from the auth context),
 * fully isolated from any Flow-dispatch session. Different users hitting
 * the same agent get **different sessions** automatically — backend reads
 * `current_user()` for the prefix.
 *
 * If no `agentId` route param is present, we render a picker so the user
 * can choose any of their OpenClaw agents.
 */

import { FormEvent, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useLocation, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  ExternalOpenclawImportCandidate,
  ExternalOpenclawImportFailure,
  ExternalOpenclawImportResult,
  OpenclawAgentCronSetting,
  OpenclawAgentRemoveMode,
  OpenclawRestorableAgent,
  OpenclawAgentDetail,
  OpenclawAgentHookSetting,
  OpenclawAgentSkillSetting,
  OpenclawAgentSummary,
  OpenclawTeam,
  api,
} from "@/lib/api";
import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  Modal,
} from "@/components/ui";
import { ChatMarkdown } from "@/components/ChatMarkdown";
import { ChatIcon, DesktopIcon, EditIcon, SettingsIcon, StoreIcon } from "@/components/icons";
import { cn } from "@/lib/cn";
import {
  clearChatHistory,
  loadChatHistory,
  saveChatHistory,
} from "@/lib/chatHistory";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
}

type SettingsTab = "skills" | "cron" | "hooks" | "agents";
type SkillEditorMode = "create" | "edit";
type CronEditorMode = "create" | "edit";
type HookEditorMode = "create" | "edit";
type CronScheduleMode = "daily" | "weekly" | "monthly";

type _SettingsData = {
  skills: OpenclawAgentSkillSetting[] | null;
  cronJobs: OpenclawAgentCronSetting[] | null;
  hooks: OpenclawAgentHookSetting[] | null;
  agentsUserCustomSection: string | null;
};

type _SettingsCacheEntry = {
  data: _SettingsData;
  cachedAt: number;
};

const CREATE_TEAM_SENTINEL = "__create_team__";
const OPENCLAW_AGENTS_UPDATED_EVENT = "csflow:openclaw-agents-updated";
const SETTINGS_CACHE_TTL_MS = 15_000;
const CREATE_CANCEL_VERIFY_TIMEOUT_MS = 30 * 1000;
const CREATE_CANCEL_VERIFY_POLL_MS = 800;
const SETTINGS_CACHE = new Map<string, _SettingsCacheEntry>();

type CreateCancelState = {
  agentId: string;
  cancelling: boolean;
};

function createEmptySettingsData(): _SettingsData {
  return {
    skills: null,
    cronJobs: null,
    hooks: null,
    agentsUserCustomSection: null,
  };
}

function notifyOpenclawAgentsUpdated() {
  if (typeof window === "undefined") return;
  window.dispatchEvent(new Event(OPENCLAW_AGENTS_UPDATED_EVENT));
}

function isAbortError(value: unknown): boolean {
  return value instanceof Error && value.name === "AbortError";
}

export function OpenclawChat() {
  const { t } = useTranslation();
  const { id } = useParams();
  const [runtimeReady, setRuntimeReady] = useState<boolean | null>(null);
  const [runtimeChecking, setRuntimeChecking] = useState(false);
  const [runtimeCheckError, setRuntimeCheckError] = useState<string | null>(null);
  const [runtimeGatewayUrl, setRuntimeGatewayUrl] = useState<string | null>(null);
  const runtimeProbeTokenRef = useRef(0);

  const verifyOpenclawRuntimeStrict = useCallback(async (token: number) => {
    try {
      const strictStatus = await api.getOpenclawRuntimeStatus("strict");
      if (runtimeProbeTokenRef.current !== token) return;
      setRuntimeGatewayUrl(strictStatus.gatewayUrl ?? null);
      if (!strictStatus.running) {
        setRuntimeReady(false);
        setRuntimeCheckError(strictStatus.reason);
      }
    } catch (e) {
      if (runtimeProbeTokenRef.current !== token) return;
      setRuntimeGatewayUrl(null);
      setRuntimeReady(false);
      setRuntimeCheckError(
        e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
      );
    }
  }, []);

  const checkOpenclawRuntime = useCallback(async () => {
    const token = runtimeProbeTokenRef.current + 1;
    runtimeProbeTokenRef.current = token;
    setRuntimeChecking(true);
    setRuntimeCheckError(null);
    try {
      const status = await api.getOpenclawRuntimeStatus("fast");
      if (runtimeProbeTokenRef.current !== token) return;
      setRuntimeGatewayUrl(status.gatewayUrl ?? null);
      setRuntimeReady(status.running);
      if (status.running) {
        // First probe succeeded: unlock UI immediately, strict check continues in background.
        void verifyOpenclawRuntimeStrict(token);
      }
    } catch (e) {
      if (runtimeProbeTokenRef.current !== token) return;
      setRuntimeGatewayUrl(null);
      setRuntimeReady(false);
      setRuntimeCheckError(
        e instanceof ApiError ? `${e.code}: ${e.message}` : String(e),
      );
    } finally {
      if (runtimeProbeTokenRef.current === token) {
        setRuntimeChecking(false);
      }
    }
  }, [verifyOpenclawRuntimeStrict]);

  useEffect(() => {
    void checkOpenclawRuntime();
  }, [checkOpenclawRuntime]);

  if (runtimeReady === null) {
    return (
      <div className="flex min-h-[45vh] items-center justify-center">
        <Loading />
      </div>
    );
  }

  if (!runtimeReady) {
    return (
      <div className="flex min-h-[55vh] items-center justify-center">
        <Card className="w-full max-w-2xl px-8 py-10 text-center">
          <p className="text-base font-medium text-ink-900">
            {t("chat.runtimeUnavailableMessage")}
          </p>
          {runtimeCheckError && (
            <p className="mt-3 text-xs text-rose-600">{runtimeCheckError}</p>
          )}
          <div className="mt-6">
            <button
              type="button"
              className="btn-primary"
              onClick={() => {
                void checkOpenclawRuntime();
              }}
              disabled={runtimeChecking}
            >
              {runtimeChecking ? t("chat.runtimeChecking") : t("chat.runtimeRetry")}
            </button>
          </div>
        </Card>
      </div>
    );
  }

  if (id) return <ChatRoom agentId={id} runtimeGatewayUrl={runtimeGatewayUrl} />;
  return (
    <div className="space-y-5">
      <AgentQuickActions />
      <ChatPicker />
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────


function AgentQuickActions() {
  const { t } = useTranslation();
  const location = useLocation();
  const navigate = useNavigate();
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [teamsError, setTeamsError] = useState<string | null>(null);
  const [createModalOpen, setCreateModalOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:create-modal-open",
  );
  const [createAgentId, setCreateAgentId] = useState("");
  const [createAgentName, setCreateAgentName] = useState("");
  const [createResponsibility, setCreateResponsibility] = useState("");
  const [createExtra, setCreateExtra] = useState("");
  const [createTeamChoice, setCreateTeamChoice] = useState("");
  const [createTeamName, setCreateTeamName] = useState("");
  const [createError, setCreateError] = useState<string | null>(null);
  const [removeModalOpen, setRemoveModalOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:remove-modal-open",
  );
  const [removeTargets, setRemoveTargets] = useState<OpenclawAgentSummary[]>([]);
  const [removeLoading, setRemoveLoading] = useState(false);
  const [removeSubmitting, setRemoveSubmitting] = useState(false);
  const [removeTargetId, setRemoveTargetId] = useState("");
  const [removeMode, setRemoveMode] = useState<OpenclawAgentRemoveMode>("unregister");
  const [removeError, setRemoveError] = useState<string | null>(null);
  const [restoreModalOpen, setRestoreModalOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:restore-modal-open",
  );
  const [restoreTargets, setRestoreTargets] = useState<OpenclawRestorableAgent[]>([]);
  const [restoreLoading, setRestoreLoading] = useState(false);
  const [restoreSubmitting, setRestoreSubmitting] = useState(false);
  const [restoreTargetId, setRestoreTargetId] = useState("");
  const [restoreError, setRestoreError] = useState<string | null>(null);
  const [importModalOpen, setImportModalOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:import-modal-open",
  );
  const [importCandidates, setImportCandidates] = useState<
    ExternalOpenclawImportCandidate[] | null
  >(null);
  const [importLoading, setImportLoading] = useState(false);
  const [importing, setImporting] = useState(false);
  const [importError, setImportError] = useState<string | null>(null);
  const [selectedImportIds, setSelectedImportIds] = useState<string[]>([]);
  const [importTeamChoice, setImportTeamChoice] = useState("");
  const [importTeamName, setImportTeamName] = useState("");
  const [lastImportResult, setLastImportResult] = useState<{
    imported: ExternalOpenclawImportResult[];
    failed: ExternalOpenclawImportFailure[];
    requestedCount: number;
  } | null>(null);
  const [workPopupOpen, setWorkPopupOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:work-popup-open",
  );
  const [workPopupRunning, setWorkPopupRunning] = useState(false);
  const [workPopupSuccess, setWorkPopupSuccess] = useState(true);
  const [workPopupText, setWorkPopupText] = useSessionBackedState(
    "openclaw-chat:quick-actions:work-popup-text",
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [createCancelState, setCreateCancelState] = useSessionBackedState<CreateCancelState | null>(
    "openclaw-chat:quick-actions:create-cancel-state",
    null,
    { isClosed: (value) => value === null },
  );
  const createRequestAbortRef = useRef<AbortController | null>(null);
  const createCancelRequestedRef = useRef(false);
  const [storeComingSoonOpen, setStoreComingSoonOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:store-coming-soon-open",
  );

  function openStoreComingSoon() {
    setStoreComingSoonOpen(true);
  }

  useEffect(() => {
    void loadTeams();
  }, []);

  useEffect(() => {
    const query = new URLSearchParams(location.search);
    const shouldOpenCreate = query.get("createAgent") === "1";
    const shouldOpenStore = query.get("storeComingSoon") === "1";
    const shouldOpenImport = query.get("importAgent") === "1";
    if (!shouldOpenCreate && !shouldOpenStore && !shouldOpenImport) return;
    if (shouldOpenCreate) onOpenCreateModal();
    if (shouldOpenStore) openStoreComingSoon();
    if (shouldOpenImport) void openImportModal();
    query.delete("createAgent");
    query.delete("storeComingSoon");
    query.delete("importAgent");
    const nextSearch = query.toString();
    navigate(
      {
        pathname: location.pathname,
        search: nextSearch ? `?${nextSearch}` : "",
      },
      { replace: true },
    );
  }, [location.pathname, location.search]);

  async function loadTeams() {
    setTeamsLoading(true);
    setTeamsError(null);
    try {
      const r = await api.listOpenclawTeams();
      setTeams(r.items);
      setCreateTeamChoice((prev) => {
        if (prev === CREATE_TEAM_SENTINEL) return prev;
        if (prev && r.items.some((item) => item.id === prev)) return prev;
        return "";
      });
      setImportTeamChoice((prev) => {
        if (prev === CREATE_TEAM_SENTINEL) return prev;
        if (prev && r.items.some((item) => item.id === prev)) return prev;
        return "";
      });
    } catch (e) {
      setTeamsError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setTeamsLoading(false);
    }
  }

  function resetCreateCancelState() {
    setCreateCancelState(null);
    createRequestAbortRef.current = null;
    createCancelRequestedRef.current = false;
  }

  function resetWorkPopupDisplayState() {
    setWorkPopupRunning(false);
    setWorkPopupSuccess(true);
    setWorkPopupText("");
  }

  function openWorkPopup() {
    setWorkPopupOpen(true);
    setWorkPopupRunning(true);
    setWorkPopupSuccess(true);
    setWorkPopupText(t("assistant.workPopup.running"));
  }

  function finishWorkPopup(success: boolean, detail?: string) {
    setWorkPopupRunning(false);
    setWorkPopupSuccess(success);
    setWorkPopupText(
      success
        ? detail || t("assistant.workPopup.done")
        : detail || t("assistant.workPopup.failed"),
    );
    resetCreateCancelState();
  }

  async function onCancelCreate() {
    if (createCancelState === null || createCancelState.cancelling) return;
    createCancelRequestedRef.current = true;
    createRequestAbortRef.current?.abort();
    setCreateCancelState((prev) =>
      prev === null
        ? prev
        : {
            ...prev,
            cancelling: true,
          },
    );
    setWorkPopupText(t("assistant.workPopup.cancelRunning"));
    try {
      await api.cancelOpenclawAgentCreate(createCancelState.agentId);
      setWorkPopupText(t("assistant.workPopup.cancelVerifying"));
      const verifyDeadline = Date.now() + CREATE_CANCEL_VERIFY_TIMEOUT_MS;
      while (true) {
        const listed = await api.listOpenclawAgents();
        if (!listed.items.some((item) => item.id === createCancelState.agentId)) break;
        if (Date.now() >= verifyDeadline) {
          throw new Error(t("assistant.workPopup.cancelAgentStillVisible"));
        }
        await new Promise((resolve) => window.setTimeout(resolve, CREATE_CANCEL_VERIFY_POLL_MS));
      }
      notifyOpenclawAgentsUpdated();
      setWorkPopupOpen(false);
      resetWorkPopupDisplayState();
      resetCreateCancelState();
    } catch (e) {
      const err = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      finishWorkPopup(false, t("assistant.workPopup.cancelFailed", { message: err }));
    }
  }

  async function resolveCreateTeamId(): Promise<string | null> {
    if (createTeamChoice === CREATE_TEAM_SENTINEL) {
      const teamName = createTeamName.trim();
      if (!teamName) {
        throw new Error(t("assistant.teamSelect.newTeamRequired"));
      }
      const created = await api.createOpenclawTeam({ name: teamName });
      setCreateTeamChoice(created.id);
      setCreateTeamName("");
      await loadTeams();
      return created.id;
    }
    const selected = createTeamChoice.trim();
    return selected || null;
  }

  async function resolveImportTeamId(): Promise<string | null> {
    if (importTeamChoice === CREATE_TEAM_SENTINEL) {
      const teamName = importTeamName.trim();
      if (!teamName) {
        throw new Error(t("assistant.teamSelect.newTeamRequired"));
      }
      const created = await api.createOpenclawTeam({ name: teamName });
      setImportTeamChoice(created.id);
      setImportTeamName("");
      await loadTeams();
      return created.id;
    }
    const selected = importTeamChoice.trim();
    return selected || null;
  }

  async function loadImportCandidates() {
    setImportLoading(true);
    setImportError(null);
    try {
      const r = await api.listOpenclawImportCandidates();
      setImportCandidates(r.items);
      setSelectedImportIds((prev) => prev.filter((id) => r.items.some((it) => it.id === id)));
    } catch (e) {
      setImportError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setImportLoading(false);
    }
  }

  function toggleImportSelection(agentId: string) {
    setSelectedImportIds((prev) =>
      prev.includes(agentId) ? prev.filter((id) => id !== agentId) : [...prev, agentId],
    );
  }

  async function onImportSelected() {
    if (importing || selectedImportIds.length === 0) return;
    let teamId: string | null = null;
    try {
      teamId = await resolveImportTeamId();
    } catch (e) {
      setImportError(e instanceof Error ? e.message : String(e));
      return;
    }
    setImporting(true);
    setImportError(null);
    openWorkPopup();
    try {
      const out = await api.importOpenclawAgents({ agentIds: selectedImportIds, teamId });
      setLastImportResult(out);
      setSelectedImportIds([]);
      await loadImportCandidates();
      if (out.imported.length > 0) notifyOpenclawAgentsUpdated();
      finishWorkPopup(out.failed.length === 0);
    } catch (e) {
      const err = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setImportError(err);
      finishWorkPopup(false, err);
    } finally {
      setImporting(false);
    }
  }

  async function onImportAll() {
    if (importing) return;
    let teamId: string | null = null;
    try {
      teamId = await resolveImportTeamId();
    } catch (e) {
      setImportError(e instanceof Error ? e.message : String(e));
      return;
    }
    setImporting(true);
    setImportError(null);
    openWorkPopup();
    try {
      const out = await api.importOpenclawAgents({ importAll: true, teamId });
      setLastImportResult(out);
      setSelectedImportIds([]);
      await loadImportCandidates();
      if (out.imported.length > 0) notifyOpenclawAgentsUpdated();
      finishWorkPopup(out.failed.length === 0);
    } catch (e) {
      const err = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setImportError(err);
      finishWorkPopup(false, err);
    } finally {
      setImporting(false);
    }
  }

  function onOpenCreateModal() {
    setCreateError(null);
    setCreateTeamName("");
    setCreateModalOpen(true);
    void loadTeams();
  }

  async function onSubmitCreateForm() {
    const agentId = createAgentId.trim();
    const agentName = createAgentName.trim();
    const responsibility = createResponsibility.trim();
    const extra = createExtra.trim();
    if (!agentId || !agentName || !responsibility) return;
    if (/\s/.test(agentId)) {
      setCreateError(t("assistant.createModal.invalidAgentIdNoSpaces"));
      return;
    }
    if (/[\u3400-\u9FFF]/.test(agentId)) {
      setCreateError(t("assistant.createModal.invalidAgentIdNoChinese"));
      return;
    }
    let teamId: string | null = null;
    try {
      teamId = await resolveCreateTeamId();
    } catch (e) {
      setCreateError(e instanceof Error ? e.message : String(e));
      return;
    }
    const description = [responsibility, extra ? `Additional requirements: ${extra}` : ""]
      .filter(Boolean)
      .join("\n\n");
    setCreateModalOpen(false);
    setCreateError(null);
    const abortController = new AbortController();
    createRequestAbortRef.current = abortController;
    createCancelRequestedRef.current = false;
    setCreateCancelState({
      agentId,
      cancelling: false,
    });
    openWorkPopup();
    try {
      const created = await api.createOpenclawAgent(
        {
          id: agentId,
          name: agentName,
          description,
          teamId,
          nlPrompt: description,
        },
        { signal: abortController.signal },
      );
      finishWorkPopup(
        true,
        t("assistant.workPopup.createdWithPath", {
          id: created.id,
          workspace: created.workspacePath,
        }),
      );
      setCreateAgentId("");
      setCreateAgentName("");
      setCreateResponsibility("");
      setCreateExtra("");
      notifyOpenclawAgentsUpdated();
    } catch (e) {
      if (createCancelRequestedRef.current || isAbortError(e)) return;
      const err = e instanceof Error ? e.message : String(e);
      finishWorkPopup(false, err);
    } finally {
      if (createRequestAbortRef.current === abortController) {
        createRequestAbortRef.current = null;
      }
    }
  }

  async function openRemoveModal() {
    setRemoveModalOpen(true);
    setRemoveError(null);
    setRemoveMode("unregister");
    setRemoveLoading(true);
    try {
      const r = await api.listOpenclawAgents();
      const items = r.items;
      setRemoveTargets(items);
      setRemoveTargetId((prev) => {
        if (prev && items.some((x) => x.id === prev)) return prev;
        return items[0]?.id ?? "";
      });
    } catch (e) {
      setRemoveError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setRemoveLoading(false);
    }
  }

  async function openRestoreModal() {
    setRestoreModalOpen(true);
    setRestoreError(null);
    setRestoreLoading(true);
    try {
      const r = await api.listOpenclawRestoreCandidates();
      setRestoreTargets(r.items);
      setRestoreTargetId((prev) => {
        if (prev && r.items.some((x) => x.id === prev)) return prev;
        return r.items[0]?.id ?? "";
      });
    } catch (e) {
      setRestoreError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setRestoreLoading(false);
    }
  }

  function buildFlowInUseError(e: ApiError): string {
    const raw = e.details?.["flow_names"];
    if (!Array.isArray(raw) || raw.length === 0) return e.message;
    const lines = raw
      .map((item) => String(item).trim())
      .filter(Boolean)
      .map((name) => `- ${name}`);
    if (lines.length === 0) return e.message;
    return `Cannot remove this agent yet. Reassign its work in the following flow(s) first:\n${lines.join("\n")}`;
  }

  async function onSubmitRemoveTarget() {
    if (!removeTargetId) return;
    const target = removeTargets.find((item) => item.id === removeTargetId);
    const targetText = target ? `${target.name} (${target.id})` : removeTargetId;
    const confirmText =
      removeMode === "purge"
        ? t("assistant.removeModal.confirmPurge", { target: targetText })
        : t("assistant.removeModal.confirmUnregister", { target: targetText });
    if (!window.confirm(confirmText)) return;
    setRemoveSubmitting(true);
    setRemoveError(null);
    try {
      await api.deleteOpenclawAgent(removeTargetId, removeMode);
      setRemoveModalOpen(false);
      setRemoveTargets((prev) => prev.filter((item) => item.id !== removeTargetId));
      setRemoveTargetId("");
      notifyOpenclawAgentsUpdated();
    } catch (e) {
      if (e instanceof ApiError) {
        setRemoveError(e.code === "AGENT_IN_USE" ? buildFlowInUseError(e) : `${e.code}: ${e.message}`);
      } else {
        setRemoveError(String(e));
      }
    } finally {
      setRemoveSubmitting(false);
    }
  }

  async function onSubmitRestoreTarget() {
    if (!restoreTargetId) return;
    setRestoreSubmitting(true);
    setRestoreError(null);
    try {
      await api.restoreOpenclawAgent(restoreTargetId);
      setRestoreModalOpen(false);
      setRestoreTargets((prev) => prev.filter((item) => item.id !== restoreTargetId));
      setRestoreTargetId("");
      notifyOpenclawAgentsUpdated();
    } catch (e) {
      setRestoreError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setRestoreSubmitting(false);
    }
  }

  async function openImportModal() {
    setImportModalOpen(true);
    await Promise.all([loadImportCandidates(), loadTeams()]);
  }

  const allCandidateIds = (importCandidates ?? []).map((item) => item.id);
  const allSelected = allCandidateIds.length > 0 && selectedImportIds.length === allCandidateIds.length;
  const importHint = t("assistant.importAgents.hint").trim();
  const createTeamReady =
    createTeamChoice !== CREATE_TEAM_SENTINEL || createTeamName.trim().length > 0;
  const importTeamReady =
    importTeamChoice !== CREATE_TEAM_SENTINEL || importTeamName.trim().length > 0;
  const canSubmitCreate = Boolean(
    createAgentId.trim() &&
    createAgentName.trim() &&
    createResponsibility.trim() &&
    createTeamReady,
  );
  const workPopupBusy = workPopupRunning || createCancelState !== null;
  const showCreateCancelAction = createCancelState !== null;
  const createCancelEnabled =
    showCreateCancelAction && !(createCancelState?.cancelling ?? false);
  const workPopupDisplayText =
    workPopupText || (workPopupOpen ? t("assistant.workPopup.running") : "");
  const heroTitle = t("assistant.toOpenclaw");
  const heroHighlight = t("assistant.toOpenclawHighlight");
  const highlightIndex = heroTitle.indexOf(heroHighlight);
  const heroTitleFont = {
    fontFamily:
      '"Inter","SF Pro Display","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Helvetica Neue",Arial,sans-serif',
  } as const;

  return (
    <>
      <Card className="relative overflow-hidden border-brand-200/80 bg-gradient-to-br from-brand-50 via-rose-50 to-orange-50 p-0">
        <div className="pointer-events-none absolute -left-16 -top-16 h-52 w-52 rounded-full bg-brand-300/25 blur-3xl" />
        <div className="pointer-events-none absolute -right-20 bottom-0 h-56 w-56 rounded-full bg-rose-300/20 blur-3xl" />
        <div className="relative space-y-4 px-5 py-5 text-center md:px-8 md:py-7">
          <div className="space-y-1.5">
            <h2
              className="text-xl font-black tracking-tight text-slate-700 md:text-2xl"
              style={heroTitleFont}
            >
              {highlightIndex >= 0 ? (
                <>
                  {heroTitle.slice(0, highlightIndex)}
                  <span className="bg-gradient-to-r from-brand-500 via-fuchsia-500 to-indigo-500 bg-clip-text text-transparent">
                    {heroHighlight}
                  </span>
                  {heroTitle.slice(highlightIndex + heroHighlight.length)}
                </>
              ) : (
                <span className="bg-gradient-to-r from-brand-600 via-fuchsia-500 to-indigo-500 bg-clip-text text-transparent">
                  {heroTitle}
                </span>
              )}
            </h2>
            <p
              className="mx-auto max-w-4xl text-xs leading-5 text-ink-600 md:text-sm"
              style={heroTitleFont}
            >
              {t("assistant.hint")}
            </p>
          </div>
          <div className="flex flex-wrap items-center justify-center gap-2.5">
            <button
              className="btn inline-flex rounded-full border border-brand-500 bg-brand-500 px-4 py-1.5 text-sm font-semibold text-white shadow-glow-sm hover:bg-brand-600"
              onClick={onOpenCreateModal}
            >
              {t("assistant.askCreate")}
            </button>
            <button
              className="btn inline-flex rounded-full border border-brand-200 bg-white/85 px-4 py-1.5 text-sm font-semibold text-brand-700 hover:bg-brand-50"
              onClick={() => void openRemoveModal()}
            >
              {t("assistant.askRemove")}
            </button>
            <button
              className="btn inline-flex rounded-full border border-brand-200 bg-white/85 px-4 py-1.5 text-sm font-semibold text-brand-700 hover:bg-brand-50"
              onClick={() => void openRestoreModal()}
            >
              {t("assistant.askRestore")}
            </button>
            <button
              className="btn inline-flex rounded-full border border-brand-200 bg-white/85 px-4 py-1.5 text-sm font-semibold text-brand-700 hover:bg-brand-50"
              onClick={() => void openImportModal()}
              disabled={importing}
            >
              {t("assistant.askImport")}
            </button>
            <button
              type="button"
              className="btn inline-flex items-center gap-1.5 rounded-full border border-fuchsia-300 bg-gradient-to-r from-indigo-500 via-fuchsia-500 to-orange-500 px-4 py-1.5 text-sm font-semibold text-white shadow-[0_0_18px_-6px_rgba(217,70,239,0.75)] hover:from-indigo-600 hover:to-orange-600"
              onClick={openStoreComingSoon}
            >
              <StoreIcon className="h-4 w-4" />
              {t("store.entryButton")}
            </button>
          </div>
        </div>
      </Card>

      <StoreComingSoonModal
        open={storeComingSoonOpen}
        onClose={() => setStoreComingSoonOpen(false)}
      />

      <Modal
        open={createModalOpen}
        onClose={() => {
          setCreateModalOpen(false);
          setCreateError(null);
        }}
        title={t("assistant.createModal.title")}
        width="max-w-2xl"
      >
        <div className="space-y-3">
          <div>
            <label className="label">{t("assistant.createModal.agentIdLabel")}</label>
            <input
              className="input"
              value={createAgentId}
              onChange={(e) => setCreateAgentId(e.target.value)}
              placeholder={t("assistant.createModal.agentIdPlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("assistant.createModal.agentNameLabel")}</label>
            <input
              className="input"
              value={createAgentName}
              onChange={(e) => setCreateAgentName(e.target.value)}
              placeholder={t("assistant.createModal.agentNamePlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("assistant.createModal.responsibilityLabel")}</label>
            <textarea
              className="textarea h-28"
              value={createResponsibility}
              onChange={(e) => setCreateResponsibility(e.target.value)}
              placeholder={t("assistant.createModal.responsibilityPlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("assistant.createModal.extraLabel")}</label>
            <textarea
              className="textarea h-24"
              value={createExtra}
              onChange={(e) => setCreateExtra(e.target.value)}
              placeholder={t("assistant.createModal.extraPlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("assistant.teamSelect.label")}</label>
            <select
              className="select"
              value={createTeamChoice}
              onChange={(e) => setCreateTeamChoice(e.target.value)}
              disabled={teamsLoading}
            >
              <option value="">{t("assistant.teamSelect.noneOptional")}</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name} ({team.id})
                </option>
              ))}
              <option value={CREATE_TEAM_SENTINEL}>{t("assistant.teamSelect.createNew")}</option>
            </select>
          </div>
          {createTeamChoice === CREATE_TEAM_SENTINEL ? (
            <div>
              <label className="label">{t("assistant.teamSelect.newTeamLabel")}</label>
              <input
                className="input"
                value={createTeamName}
                onChange={(e) => setCreateTeamName(e.target.value)}
                placeholder={t("assistant.teamSelect.newTeamPlaceholder")}
                disabled={teamsLoading}
              />
            </div>
          ) : null}
          {teamsError && <ErrorBox>{teamsError}</ErrorBox>}
          {createError && <ErrorBox>{createError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setCreateModalOpen(false);
                setCreateError(null);
              }}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSubmitCreateForm()}
              disabled={teamsLoading || !canSubmitCreate}
            >
              {t("assistant.createModal.submit")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={removeModalOpen}
        onClose={() => setRemoveModalOpen(false)}
        title={t("assistant.removeModal.title")}
        width="max-w-lg"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-600">{t("assistant.removeModal.hint")}</p>
          {removeLoading && <Loading />}
          {removeError && <ErrorBox>{removeError}</ErrorBox>}
          {!removeLoading && !removeError && (
            <>
              {removeTargets.length === 0 ? (
                <div className="text-sm text-ink-500">{t("assistant.removeModal.empty")}</div>
              ) : (
                <>
                  <div>
                    <label className="label">{t("assistant.removeModal.targetLabel")}</label>
                    <select
                      className="select"
                      value={removeTargetId}
                      onChange={(e) => setRemoveTargetId(e.target.value)}
                      disabled={removeSubmitting}
                    >
                      {removeTargets.map((item) => (
                        <option key={item.id} value={item.id}>
                          {item.name} ({item.id})
                        </option>
                      ))}
                    </select>
                  </div>
                  <div>
                    <label className="label">{t("assistant.removeModal.modeLabel")}</label>
                    <select
                      className="select"
                      value={removeMode}
                      onChange={(e) => setRemoveMode(e.target.value as OpenclawAgentRemoveMode)}
                      disabled={removeSubmitting}
                    >
                      <option value="unregister">{t("assistant.removeModal.modeUnregister")}</option>
                      <option value="purge">{t("assistant.removeModal.modePurge")}</option>
                    </select>
                  </div>
                </>
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => setRemoveModalOpen(false)}
                >
                  {t("common.cancel")}
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => void onSubmitRemoveTarget()}
                  disabled={removeSubmitting || !removeTargetId || removeTargets.length === 0}
                >
                  {removeSubmitting ? t("assistant.removeModal.removing") : t("assistant.removeModal.submit")}
                </button>
              </div>
            </>
          )}
        </div>
      </Modal>

      <Modal
        open={restoreModalOpen}
        onClose={() => setRestoreModalOpen(false)}
        title={t("assistant.restoreModal.title")}
        width="max-w-lg"
      >
        <div className="space-y-3">
          <p className="text-sm text-ink-600">{t("assistant.restoreModal.hint")}</p>
          {restoreLoading && <Loading />}
          {restoreError && <ErrorBox>{restoreError}</ErrorBox>}
          {!restoreLoading && !restoreError && (
            <>
              {restoreTargets.length === 0 ? (
                <div className="text-sm text-ink-500">{t("assistant.restoreModal.empty")}</div>
              ) : (
                <div>
                  <label className="label">{t("assistant.restoreModal.targetLabel")}</label>
                  <select
                    className="select"
                    value={restoreTargetId}
                    onChange={(e) => setRestoreTargetId(e.target.value)}
                    disabled={restoreSubmitting}
                  >
                    {restoreTargets.map((item) => (
                      <option key={item.id} value={item.id}>
                        {item.name} ({item.id})
                      </option>
                    ))}
                  </select>
                </div>
              )}
              <div className="flex justify-end gap-2">
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => setRestoreModalOpen(false)}
                >
                  {t("common.cancel")}
                </button>
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => void onSubmitRestoreTarget()}
                  disabled={restoreSubmitting || !restoreTargetId || restoreTargets.length === 0}
                >
                  {restoreSubmitting ? t("assistant.restoreModal.restoring") : t("assistant.restoreModal.submit")}
                </button>
              </div>
            </>
          )}
        </div>
      </Modal>

      <Modal
        open={importModalOpen}
        onClose={() => setImportModalOpen(false)}
        title={t("assistant.importAgents.title")}
        width="max-w-2xl"
      >
        <div className="space-y-3">
          {importHint ? (
            <p className="text-sm text-ink-600">{importHint}</p>
          ) : null}
          <div className="space-y-2 rounded-md border border-ink-200 bg-ink-50 p-3">
            <label className="label">{t("assistant.teamSelect.label")}</label>
            <select
              className="select"
              value={importTeamChoice}
              onChange={(e) => setImportTeamChoice(e.target.value)}
              disabled={teamsLoading || importing}
            >
              <option value="">{t("assistant.teamSelect.noneOptional")}</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name} ({team.id})
                </option>
              ))}
              <option value={CREATE_TEAM_SENTINEL}>{t("assistant.teamSelect.createNew")}</option>
            </select>
            {importTeamChoice === CREATE_TEAM_SENTINEL ? (
              <input
                className="input"
                value={importTeamName}
                onChange={(e) => setImportTeamName(e.target.value)}
                placeholder={t("assistant.teamSelect.newTeamPlaceholder")}
                disabled={teamsLoading || importing}
              />
            ) : null}
            {teamsError && <ErrorBox>{teamsError}</ErrorBox>}
          </div>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => void loadImportCandidates()}
              disabled={importLoading || importing}
            >
              {t("assistant.importAgents.refresh")}
            </button>
            <button
              type="button"
              className="btn-outline"
              onClick={onImportAll}
              disabled={
                importLoading ||
                importing ||
                teamsLoading ||
                !importTeamReady ||
                (importCandidates?.length ?? 0) === 0
              }
            >
              {t("assistant.importAgents.importAll")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={onImportSelected}
              disabled={
                importLoading ||
                importing ||
                teamsLoading ||
                !importTeamReady ||
                selectedImportIds.length === 0
              }
            >
              {importing
                ? t("assistant.importAgents.importing")
                : t("assistant.importAgents.importSelected", { count: selectedImportIds.length })}
            </button>
            <button
              type="button"
              className="btn-outline"
              onClick={() => setSelectedImportIds(allSelected ? [] : allCandidateIds)}
              disabled={importLoading || importing || (importCandidates?.length ?? 0) === 0}
            >
              {allSelected
                ? t("assistant.importAgents.clearSelection")
                : t("assistant.importAgents.selectAll")}
            </button>
          </div>

          {importLoading && <Loading />}
          {importError && <ErrorBox>{importError}</ErrorBox>}

          {importCandidates && importCandidates.length === 0 && !importLoading && (
            <div className="text-sm text-ink-500">{t("assistant.importAgents.empty")}</div>
          )}

          {importCandidates && importCandidates.length > 0 && (
            <div className="max-h-72 overflow-auto rounded-md border border-ink-200">
              {importCandidates.map((item) => {
                const checked = selectedImportIds.includes(item.id);
                return (
                  <label
                    key={item.id}
                    className="flex cursor-pointer items-start gap-2 border-b border-ink-100 px-3 py-2 last:border-b-0 hover:bg-ink-50"
                  >
                    <input
                      type="checkbox"
                      className="mt-1"
                      checked={checked}
                      onChange={() => toggleImportSelection(item.id)}
                      disabled={importing}
                    />
                    <span className="min-w-0 text-sm">
                      <span className="block text-ink-900">
                        {item.name} ({item.id})
                      </span>
                      <span className="block break-all text-xs text-ink-500">
                        {item.workspacePath}
                      </span>
                    </span>
                  </label>
                );
              })}
            </div>
          )}

          {lastImportResult && (
            <div className="rounded-md border border-ink-200 bg-ink-50 px-3 py-2 text-xs text-ink-700 space-y-1">
              <div>
                {t("assistant.importAgents.lastResult", {
                  requested: lastImportResult.requestedCount,
                  imported: lastImportResult.imported.length,
                  failed: lastImportResult.failed.length,
                })}
              </div>
              {lastImportResult.imported.map((item) => (
                <div key={item.targetAgentId}>
                  + {item.sourceAgentId} → {item.targetAgentId}
                </div>
              ))}
              {lastImportResult.failed.map((item) => (
                <div key={`${item.sourceAgentId}-${item.errorCode}`} className="text-rose-700">
                  - {item.sourceAgentId}: {item.errorCode} ({item.message})
                </div>
              ))}
            </div>
          )}
        </div>
      </Modal>

      <Modal
        open={workPopupOpen}
        onClose={() => {
          if (workPopupBusy) return;
          setWorkPopupOpen(false);
          resetWorkPopupDisplayState();
          resetCreateCancelState();
        }}
        title={t("assistant.workPopup.title")}
        width="max-w-xl"
        dismissible={!workPopupBusy}
      >
        <div className="space-y-4">
          <p
            className={
              workPopupBusy
                ? "text-sm text-ink-700"
                : workPopupSuccess
                ? "text-sm text-emerald-700"
                : "text-sm text-rose-700"
            }
          >
            {workPopupDisplayText}
          </p>
          {workPopupBusy && <Loading />}
          {showCreateCancelAction && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-outline"
                onClick={() => void onCancelCreate()}
                disabled={!createCancelEnabled}
              >
                {createCancelState?.cancelling
                  ? t("assistant.workPopup.cancellingCreate")
                  : t("assistant.workPopup.cancelCreate")}
              </button>
            </div>
          )}
          {!workPopupBusy && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-primary"
                onClick={() => {
                  setWorkPopupOpen(false);
                  resetWorkPopupDisplayState();
                  resetCreateCancelState();
                }}
              >
                {t("assistant.workPopup.close")}
              </button>
            </div>
          )}
        </div>
      </Modal>
    </>
  );
}


function ChatPicker() {
  const { t } = useTranslation();
  const [items, setItems] = useState<OpenclawAgentSummary[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [storeComingSoonOpen, setStoreComingSoonOpen] = useSessionBackedModalFlag(
    "openclaw-chat:picker:store-coming-soon-open",
  );
  const [viewMode, setViewMode] = useState<"card" | "list">("card");
  const [editingTeam, setEditingTeam] = useSessionBackedState<{ id: string; name: string } | null>(
    "openclaw-chat:picker:editing-team",
    null,
    { isClosed: (value) => value === null },
  );
  const [editingTeamName, setEditingTeamName] = useSessionBackedState(
    "openclaw-chat:picker:editing-team-name",
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [editingTeamError, setEditingTeamError] = useSessionBackedState<string | null>(
    "openclaw-chat:picker:editing-team-error",
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [editingTeamSaving, setEditingTeamSaving] = useState(false);

  const loadAgents = useCallback(async () => {
    setError(null);
    try {
      const r = await api.listOpenclawAgents();
      setItems(r.items);
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, []);

  useEffect(() => {
    void loadAgents();
  }, [loadAgents]);

  useEffect(() => {
    if (typeof window === "undefined") return;
    const onAgentsUpdated = () => {
      void loadAgents();
    };
    window.addEventListener(OPENCLAW_AGENTS_UPDATED_EVENT, onAgentsUpdated);
    return () => {
      window.removeEventListener(OPENCLAW_AGENTS_UPDATED_EVENT, onAgentsUpdated);
    };
  }, [loadAgents]);

  const groupedByTeam = useMemo(() => {
    const source = items ?? [];
    const groups = new Map<string, { teamName: string; agents: OpenclawAgentSummary[] }>();
    for (const agent of source) {
      const key = agent.teamId || "__ungrouped__";
      const teamName = agent.teamName || t("chat.ungroupedTeam");
      const current = groups.get(key);
      if (current) {
        current.agents.push(agent);
      } else {
        groups.set(key, { teamName, agents: [agent] });
      }
    }
    return Array.from(groups.entries())
      .map(([teamId, group]) => ({ teamId, ...group }))
      .sort((a, b) => {
        if (a.teamId === "__ungrouped__" && b.teamId !== "__ungrouped__") return -1;
        if (a.teamId !== "__ungrouped__" && b.teamId === "__ungrouped__") return 1;
        return a.teamName.localeCompare(b.teamName);
      })
      .map((group) => ({
        ...group,
        agents: group.agents.slice().sort((a, b) => a.name.localeCompare(b.name)),
      }));
  }, [items, t]);

  function openRenameTeam(teamId: string, teamName: string) {
    setEditingTeam({ id: teamId, name: teamName });
    setEditingTeamName(teamName);
    setEditingTeamError(null);
  }

  async function onConfirmRenameTeam() {
    if (!editingTeam || editingTeamSaving) return;
    const nextName = editingTeamName.trim();
    if (!nextName) {
      setEditingTeamError(t("chat.renameTeam.required"));
      return;
    }
    setEditingTeamSaving(true);
    setEditingTeamError(null);
    try {
      await api.patchOpenclawTeam(editingTeam.id, { name: nextName });
      setEditingTeam(null);
      setEditingTeamName("");
      await loadAgents();
    } catch (e) {
      setEditingTeamError(e instanceof ApiError ? `${e.code}: ${e.message}` : String(e));
    } finally {
      setEditingTeamSaving(false);
    }
  }

  return (
    <div className="space-y-5">
      <div>
        <h1 className="text-xl font-semibold text-ink-900">{t("chat.title")}</h1>
        <p className="text-sm text-ink-500">{t("chat.pickerLabel")}</p>
      </div>
      <div className="inline-flex rounded-lg border border-brand-200 bg-brand-50/60 p-0.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
        <button
          type="button"
          className={cn(
            "min-w-[52px] rounded-md px-2 py-1 text-xs font-semibold transition-all duration-200",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300",
            viewMode === "card"
              ? "bg-gradient-to-r from-brand-600 to-brand-400 text-white shadow-[0_8px_18px_-10px_theme(colors.brand.700)]"
              : "text-ink-600 hover:bg-white/70 hover:text-brand-700",
          )}
          onClick={() => setViewMode("card")}
        >
          {t("chat.viewCard")}
        </button>
        <button
          type="button"
          className={cn(
            "min-w-[52px] rounded-md px-2 py-1 text-xs font-semibold transition-all duration-200",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300",
            viewMode === "list"
              ? "bg-gradient-to-r from-brand-600 to-brand-400 text-white shadow-[0_8px_18px_-10px_theme(colors.brand.700)]"
              : "text-ink-600 hover:bg-white/70 hover:text-brand-700",
          )}
          onClick={() => setViewMode("list")}
        >
          {t("chat.viewList")}
        </button>
      </div>
      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}
      {items && items.length === 0 && (
        <EmptyState
          icon={<ChatIcon className="h-10 w-10" />}
          action={
            <div className="flex flex-col items-center gap-2">
              <div className="flex items-center gap-2">
                <Link to="/chat?createAgent=1" className="btn-primary">
                  {t("chat.pickerEmptyActionCreate")}
                </Link>
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => setStoreComingSoonOpen(true)}
                >
                  {t("chat.pickerEmptyActionLoadStore")}
                </button>
              </div>
              <Link to="/chat?importAgent=1" className="btn-outline">
                {t("assistant.askImport")}
              </Link>
            </div>
          }
        />
      )}
      {items && items.length > 0 && viewMode === "card" && (
        <div className="space-y-5">
          {groupedByTeam.map((group) => (
            <div key={group.teamId} className="space-y-3">
              <div className="inline-flex items-center gap-1 rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-semibold text-brand-700">
                <span>{t("chat.teamSectionTitle", { team: group.teamName })}</span>
                {group.teamId !== "__ungrouped__" && (
                  <button
                    type="button"
                    className="inline-flex h-5 w-5 items-center justify-center rounded-full text-brand-700 hover:bg-brand-100"
                    title={t("chat.renameTeam.action")}
                    onClick={() => openRenameTeam(group.teamId, group.teamName)}
                  >
                    <EditIcon className="h-3.5 w-3.5" />
                  </button>
                )}
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {group.agents.map((a) => (
                  <Link
                    key={a.id}
                    to={`/chat/${a.id}`}
                    className="group card p-5 hover:border-brand-300 hover:shadow-[0_0_24px_-6px_theme(colors.brand.300)] transition-all"
                  >
                    <div className="mb-3 inline-flex h-14 w-14 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 text-brand-500 shadow-[0_0_18px_-8px_theme(colors.brand.400)] transition-shadow group-hover:shadow-[0_0_22px_-6px_theme(colors.brand.400)]">
                      <ChatIcon className="h-9 w-9" />
                    </div>
                    <div className="font-semibold text-ink-900">{a.name}</div>
                    <div className="text-xs text-ink-500 font-mono mt-0.5">
                      {a.id}
                    </div>
                    {a.description && (
                      <div className="text-xs text-ink-500 mt-2 line-clamp-3">
                        {a.description}
                      </div>
                    )}
                  </Link>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}
      {items && items.length > 0 && viewMode === "list" && (
        <Card className="p-0 overflow-hidden">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500">
              <tr>
                <th className="text-left px-4 py-2 font-medium">{t("chat.agentLabel")}</th>
                <th className="text-left px-4 py-2 font-medium">
                  {t("chat.columnId")}
                </th>
                <th className="text-left px-4 py-2 font-medium">{t("chat.teamLabel")}</th>
                <th className="text-left px-4 py-2 font-medium">{t("common.description")}</th>
                <th className="text-right px-4 py-2 font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {items.map((a) => (
                <tr key={a.id} className="table-row">
                  <td className="px-4 py-3 text-ink-900 font-medium">{a.name}</td>
                  <td className="px-4 py-3 text-xs font-mono text-ink-500">{a.id}</td>
                  <td className="px-4 py-3 text-ink-600">{a.teamName || t("chat.ungroupedTeam")}</td>
                  <td className="px-4 py-3 text-ink-600">
                    {a.description || t("common.none")}
                  </td>
                  <td className="px-4 py-3 text-right">
                    <Link className="btn-primary" to={`/chat/${a.id}`}>
                      {t("agents.chatLink")}
                    </Link>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      <Modal
        open={!!editingTeam}
        onClose={() => {
          if (editingTeamSaving) return;
          setEditingTeam(null);
          setEditingTeamName("");
          setEditingTeamError(null);
        }}
        title={t("chat.renameTeam.title")}
      >
        <div className="space-y-3">
          <div>
            <label className="label">{t("chat.renameTeam.label")}</label>
            <input
              className="input"
              value={editingTeamName}
              onChange={(e) => setEditingTeamName(e.target.value)}
              placeholder={t("chat.renameTeam.placeholder")}
              disabled={editingTeamSaving}
            />
          </div>
          {editingTeamError && <ErrorBox>{editingTeamError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (editingTeamSaving) return;
                setEditingTeam(null);
                setEditingTeamName("");
                setEditingTeamError(null);
              }}
              disabled={editingTeamSaving}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onConfirmRenameTeam()}
              disabled={editingTeamSaving}
            >
              {editingTeamSaving ? t("chat.renameTeam.saving") : t("common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <StoreComingSoonModal
        open={storeComingSoonOpen}
        onClose={() => setStoreComingSoonOpen(false)}
      />
    </div>
  );
}

function StoreComingSoonModal({
  open,
  onClose,
}: {
  open: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const headingFont = {
    fontFamily:
      '"Inter","SF Pro Display","PingFang SC","Hiragino Sans GB","Microsoft YaHei","Helvetica Neue",Arial,sans-serif',
  } as const;
  return (
    <Modal open={open} onClose={onClose} title="" width="max-w-md">
      <div className="relative overflow-hidden rounded-2xl border border-brand-100 bg-gradient-to-br from-indigo-50 via-white to-fuchsia-50 px-6 py-7">
        <div className="pointer-events-none absolute -left-10 -top-10 h-28 w-28 rounded-full bg-indigo-300/30 blur-2xl" />
        <div className="pointer-events-none absolute -bottom-12 -right-12 h-32 w-32 rounded-full bg-fuchsia-300/30 blur-2xl" />
        <div className="relative space-y-4 text-center">
          <span className="mx-auto inline-flex h-20 w-20 items-center justify-center rounded-[22px] bg-gradient-to-br from-indigo-500 via-fuchsia-500 to-orange-500 text-white shadow-[0_0_24px_-6px_rgba(217,70,239,0.85)]">
            <StoreIcon className="h-9 w-9" />
          </span>
          <h4 className="text-xl font-semibold tracking-tight text-ink-900" style={headingFont}>
            {t("store.comingSoon.headline")}
          </h4>
          <div className="pt-1">
            <button
              type="button"
              className="inline-flex rounded-full border border-fuchsia-300 bg-gradient-to-r from-indigo-500 via-fuchsia-500 to-orange-500 px-5 py-2 text-sm font-semibold text-white shadow-[0_0_20px_-8px_rgba(217,70,239,0.8)] transition hover:from-indigo-600 hover:to-orange-600"
              onClick={onClose}
            >
              {t("store.comingSoon.action")}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}

// ──────────────────────────────────────────────────────────────────────


function ChatRoom({
  agentId,
  runtimeGatewayUrl,
}: {
  agentId: string;
  runtimeGatewayUrl?: string | null;
}) {
  const navigate = useNavigate();
  const { t } = useTranslation();
  const [agent, setAgent] = useState<OpenclawAgentDetail | null>(null);
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [streaming, setStreaming] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [teamEditOpen, setTeamEditOpen] = useSessionBackedModalFlag(
    `openclaw-chat:room:${agentId}:team-edit-open`,
  );
  const [teamEditChoice, setTeamEditChoice] = useSessionBackedState(
    `openclaw-chat:room:${agentId}:team-edit-choice`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [teamEditNewTeamName, setTeamEditNewTeamName] = useSessionBackedState(
    `openclaw-chat:room:${agentId}:team-edit-new-team-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [teamEditError, setTeamEditError] = useSessionBackedState<string | null>(
    `openclaw-chat:room:${agentId}:team-edit-error`,
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [teamEditSaving, setTeamEditSaving] = useState(false);
  const [settingsOpen, setSettingsOpen] = useSessionBackedModalFlag(
    `openclaw-chat:room:${agentId}:settings-open`,
  );
  const [openingMyDesktop, setOpeningMyDesktop] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    api
      .getOpenclawAgent(agentId)
      .then((row) => {
        setAgent(row);
        setTeamEditChoice(row.teamId || "");
      })
      .catch((e) => {
        setError(e instanceof ApiError ? e.message : String(e));
      });
  }, [agentId, t]);

  const refreshTeams = useCallback(async () => {
    try {
      const r = await api.listOpenclawTeams();
      setTeams(r.items);
    } catch {
      setTeams([]);
    }
  }, []);

  useEffect(() => {
    void refreshTeams();
  }, [refreshTeams]);

  // Restore the last ≤20 messages from localStorage whenever we switch
  // agents (incl. on initial mount / page refresh). Only the explicit
  // "Reset" button (or its localStorage entry being cleared) wipes them.
  useEffect(() => {
    setMessages(loadChatHistory(agentId));
    setActionError(null);
  }, [agentId]);

  // Persist on every transcript change. ``saveChatHistory`` keeps only
  // the trailing ``HISTORY_LIMIT`` entries so the storage stays bounded.
  useEffect(() => {
    if (messages.length === 0) return;
    saveChatHistory(agentId, messages);
  }, [agentId, messages]);

  // Auto-scroll on new content and once the chat panel is actually mounted.
  // When we restore history before agent detail finishes loading, the first
  // ``messages`` update can happen while the scroll container is still absent.
  // Including ``agent`` ensures we scroll to latest as soon as the panel appears.
  useEffect(() => {
    if (scrollRef.current) {
      scrollRef.current.scrollTop = scrollRef.current.scrollHeight;
    }
  }, [messages, agent]);

  async function onSend(e: FormEvent) {
    e.preventDefault();
    const text = input.trim();
    if (!text || streaming) return;
    setActionError(null);
    const turnMessage: Message = { role: "user", content: text };
    const next: Message[] = [...messages, turnMessage];
    setMessages(next);
    setInput("");
    setStreaming(true);
    // Add an empty assistant message to stream into.
    setMessages((m) => [...m, { role: "assistant", content: "" }]);

    try {
      const resp = await api.chatWithOpenclawAgent(agentId, {
        // Session context is maintained by backend session_key.
        // Send only this turn's message to avoid client-side context replay.
        messages: [turnMessage],
        stream: true,
      });
      if (!resp.ok) {
        const errBody = await resp.text();
        throw new Error(`HTTP ${resp.status}: ${errBody.slice(0, 300)}`);
      }
      const reader = resp.body?.getReader();
      const decoder = new TextDecoder();
      let buffer = "";
      while (reader) {
        const { value, done } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        let nl;
        while ((nl = buffer.indexOf("\n\n")) >= 0) {
          const block = buffer.slice(0, nl).trim();
          buffer = buffer.slice(nl + 2);
          if (!block.startsWith("data:")) continue;
          const payload = block.slice(5).trim();
          if (payload === "[DONE]") continue;
          try {
            const chunk = JSON.parse(payload);
            const delta = extractDelta(chunk);
            if (delta) {
              setMessages((m) => {
                const out = m.slice();
                out[out.length - 1] = {
                  role: "assistant",
                  content: out[out.length - 1].content + delta,
                };
                return out;
              });
            }
          } catch {
            /* ignore non-JSON keepalives */
          }
        }
      }
    } catch (e) {
      setMessages((m) => {
        const out = m.slice();
        out[out.length - 1] = {
          role: "assistant",
          content: `(error) ${e instanceof Error ? e.message : String(e)}`,
        };
        return out;
      });
    } finally {
      setStreaming(false);
    }
  }

  async function onResetConversation() {
    if (streaming || resetting) return;
    setActionError(null);
    setResetting(true);
    try {
      await api.resetOpenclawAgentChat(agentId);
      setMessages([]);
      setInput("");
      clearChatHistory(agentId);
    } catch (e) {
      setActionError(e instanceof ApiError ? e.message : String(e));
    } finally {
      setResetting(false);
    }
  }

  async function onSaveTeam() {
    if (!agent || teamEditSaving) return;
    let nextTeamId: string | null = null;
    if (teamEditChoice === CREATE_TEAM_SENTINEL) {
      const nextTeamName = teamEditNewTeamName.trim();
      if (!nextTeamName) {
        setTeamEditError(t("chat.teamEdit.newTeamRequired"));
        return;
      }
      try {
        const created = await api.createOpenclawTeam({ name: nextTeamName });
        nextTeamId = created.id;
      } catch (e) {
        const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
        setTeamEditError(text);
        setActionError(text);
        return;
      }
    } else {
      const selected = teamEditChoice.trim();
      nextTeamId = selected || null;
    }
    setTeamEditSaving(true);
    setTeamEditError(null);
    setActionError(null);
    try {
      const updated = await api.patchOpenclawAgent(agent.id, {
        teamId: nextTeamId,
      });
      setAgent(updated);
      setTeamEditChoice(updated.teamId || "");
      setTeamEditNewTeamName("");
      setTeamEditOpen(false);
      notifyOpenclawAgentsUpdated();
      await refreshTeams();
    } catch (e) {
      const text = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      setTeamEditError(text);
      setActionError(text);
    } finally {
      setTeamEditSaving(false);
    }
  }

  async function onOpenMyDesktop() {
    if (!agent || openingMyDesktop) return;
    if (isRemoteBrowser()) {
      if (typeof window !== "undefined") {
        window.alert(t("chat.myDesktop.remoteUnavailable"));
      }
      return;
    }
    const targetPath = buildAgentMyDesktopPath(agent.workspacePath);
    setActionError(null);
    setOpeningMyDesktop(true);
    try {
      await api.openDirectory({ path: targetPath });
    } catch (e) {
      const message = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      if (typeof window !== "undefined") {
        window.alert(t("chat.myDesktop.openFailed", { message }));
      } else {
        setActionError(message);
      }
    } finally {
      setOpeningMyDesktop(false);
    }
  }

  if (error)
    return (
      <div className="space-y-3">
        <ErrorBox>{error}</ErrorBox>
        <button
          className="btn-outline inline-flex h-9 items-center justify-center px-3 py-0 text-sm"
          onClick={() => navigate("/chat")}
          aria-label={t("common.back")}
          title={t("common.back")}
        >
          {t("common.back")}
        </button>
      </div>
    );
  if (!agent) return <Loading />;

  const openclawUrl = buildOpenclawChatUrl(agentId, runtimeGatewayUrl);

  return (
    <div className="flex h-[calc(100vh-6rem)] min-h-0 flex-col gap-5 overflow-hidden">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="text-xl font-semibold text-ink-900 flex items-center gap-3">
            <span className="inline-flex h-11 w-11 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 text-brand-500 shadow-[0_0_18px_-8px_theme(colors.brand.400)]">
              <ChatIcon className="h-7 w-7" />
            </span>
            <span className="truncate">{agent.name}</span>
            <button
              type="button"
              className="btn-outline inline-flex h-8 items-center justify-center gap-1.5 px-2.5 py-0 text-xs font-medium"
              onClick={() => {
                void onOpenMyDesktop();
              }}
              disabled={openingMyDesktop}
              title={t("chat.myDesktop.action")}
            >
              <DesktopIcon className="h-3.5 w-3.5" />
              {openingMyDesktop ? t("chat.myDesktop.opening") : t("chat.myDesktop.action")}
            </button>
          </h1>
          <div className="mt-2 inline-flex items-center gap-2 text-xs text-ink-600">
            <span>{t("chat.teamLabel")}:</span>
            <span className="rounded-full border border-ink-200 bg-ink-50 px-2 py-0.5">
              {agent.teamName || t("chat.ungroupedTeam")}
            </span>
            <button
              type="button"
              className="btn-primary !px-2.5 !py-1 text-[11px] shadow-[0_0_12px_-6px_theme(colors.brand.400)]"
              onClick={() => {
                setTeamEditChoice(agent.teamId || "");
                setTeamEditNewTeamName("");
                setTeamEditError(null);
                setTeamEditOpen(true);
              }}
            >
              {t("chat.teamEdit.action")}
            </button>
          </div>
        </div>
        <div className="shrink-0 inline-flex items-center gap-2">
          <button
            type="button"
            className="btn-outline inline-flex h-10 items-center justify-center px-4 py-0 text-sm font-medium"
            onClick={() => navigate("/chat")}
            aria-label={t("common.back")}
            title={t("common.back")}
          >
            {t("common.back")}
          </button>
          <button
            type="button"
            className="btn-outline inline-flex h-10 items-center justify-center gap-2 px-4 py-0 text-sm font-medium"
            onClick={() => setSettingsOpen(true)}
            aria-label={t("chat.settings.action")}
            title={t("chat.settings.action")}
          >
            <SettingsIcon className="h-4 w-4" />
            {t("chat.settings.action")}
          </button>
          <a
            href={openclawUrl}
            target="_blank"
            rel="noreferrer"
            className="inline-flex h-10 items-center justify-center rounded-full
                     bg-gradient-to-r from-brand-500 via-brand-400 to-orange-500
                     px-5 py-0 text-sm font-semibold tracking-wide text-white
                     shadow-[0_0_24px_-4px_theme(colors.brand.400)]
                     ring-1 ring-brand-300/60
                     hover:from-brand-600 hover:to-orange-600
                     hover:shadow-[0_0_32px_-2px_theme(colors.brand.400)]
                     hover:-translate-y-0.5
                     transition-all"
          >
            {t("chat.toOpenclaw")}
          </a>
        </div>
      </div>

      {actionError && <ErrorBox>{t("chat.error", { message: actionError })}</ErrorBox>}

      <Card className="flex min-h-0 flex-1 flex-col p-0 overflow-hidden">
        <div
          ref={scrollRef}
          className="min-h-[280px] flex-1 overflow-auto px-5 py-4 space-y-4 bg-ink-50/40"
        >
          {messages.map((m, i) => (
            <Bubble
              key={i}
              msg={m}
              pending={
                streaming &&
                i === messages.length - 1 &&
                m.role === "assistant" &&
                !m.content
              }
              noTextReply={t("chat.noTextReply")}
            />
          ))}
        </div>
        <form
          onSubmit={onSend}
          className="border-t border-ink-100 p-3 flex items-end gap-2"
        >
          <textarea
            className="textarea flex-1 resize-none h-20"
            placeholder={t("chat.inputPlaceholder")}
            value={input}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey) {
                e.preventDefault();
                if (!streaming) (e.currentTarget.form as HTMLFormElement).requestSubmit();
              }
            }}
            disabled={streaming}
          />
          <button
            type="button"
            className="btn-outline"
            disabled={streaming || resetting}
            onClick={onResetConversation}
          >
            {resetting ? t("chat.resetting") : t("chat.reset")}
          </button>
          <button
            type="submit"
            className="btn-primary"
            disabled={streaming || !input.trim()}
          >
            {streaming ? t("chat.sending") : t("chat.send")}
          </button>
        </form>
      </Card>

      <Modal
        open={teamEditOpen}
        onClose={() => {
          if (teamEditSaving) return;
          setTeamEditOpen(false);
          setTeamEditNewTeamName("");
          setTeamEditError(null);
        }}
        title={t("chat.teamEdit.title")}
      >
        <div className="space-y-3">
          <div>
            <label className="label">{t("chat.teamEdit.label")}</label>
            <select
              className="select"
              value={teamEditChoice}
              onChange={(e) => setTeamEditChoice(e.target.value)}
              disabled={teamEditSaving}
            >
              <option value="">{t("chat.teamEdit.noneOptional")}</option>
              {teams.map((team) => (
                <option key={team.id} value={team.id}>
                  {team.name}
                </option>
              ))}
              <option value={CREATE_TEAM_SENTINEL}>{t("chat.teamEdit.createNew")}</option>
            </select>
          </div>
          {teamEditChoice === CREATE_TEAM_SENTINEL ? (
            <div>
              <label className="label">{t("chat.teamEdit.newTeamLabel")}</label>
              <input
                className="input"
                value={teamEditNewTeamName}
                onChange={(e) => setTeamEditNewTeamName(e.target.value)}
                placeholder={t("chat.teamEdit.newTeamPlaceholder")}
                disabled={teamEditSaving}
              />
            </div>
          ) : null}
          {teamEditError && <ErrorBox>{teamEditError}</ErrorBox>}
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                if (teamEditSaving) return;
                setTeamEditOpen(false);
                setTeamEditNewTeamName("");
                setTeamEditError(null);
              }}
              disabled={teamEditSaving}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void onSaveTeam()}
              disabled={teamEditSaving}
            >
              {teamEditSaving ? t("chat.teamEdit.saving") : t("common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <AgentSettingsModal
        open={settingsOpen}
        onClose={() => setSettingsOpen(false)}
        agentId={agent.id}
        onError={setActionError}
      />
    </div>
  );
}

function formatActionError(error: unknown): string {
  if (error instanceof ApiError) return `${error.code}: ${error.message}`;
  if (error instanceof Error) return error.message;
  return String(error);
}

function _pad2(v: number): string {
  return String(v).padStart(2, "0");
}

function parseSimpleCronSchedule(expr: string): {
  mode: CronScheduleMode;
  time: string;
  weekday: number;
  dayOfMonth: number;
} | null {
  const parts = expr.trim().split(/\s+/);
  if (parts.length !== 5) return null;
  const [minRaw, hourRaw, domRaw, monthRaw, dowRaw] = parts;
  if (!/^\d+$/.test(minRaw) || !/^\d+$/.test(hourRaw)) return null;
  const minute = Number(minRaw);
  const hour = Number(hourRaw);
  if (minute < 0 || minute > 59 || hour < 0 || hour > 23) return null;
  const time = `${_pad2(hour)}:${_pad2(minute)}`;
  if (monthRaw !== "*") return null;
  if (domRaw === "*" && dowRaw === "*") {
    return { mode: "daily", time, weekday: 1, dayOfMonth: 1 };
  }
  if (domRaw === "*" && /^\d+$/.test(dowRaw)) {
    const weekday = Number(dowRaw);
    if (weekday < 0 || weekday > 6) return null;
    return { mode: "weekly", time, weekday, dayOfMonth: 1 };
  }
  if (dowRaw === "*" && /^\d+$/.test(domRaw)) {
    const dayOfMonth = Number(domRaw);
    if (dayOfMonth < 1 || dayOfMonth > 31) return null;
    return { mode: "monthly", time, weekday: 1, dayOfMonth };
  }
  return null;
}

function defaultHookMarkdown(name: string): string {
  return [
    "---",
    `name: ${name}`,
    "description: Describe this hook briefly",
    "metadata:",
    "  openclaw:",
    "    events:",
    "      - session:start",
    "---",
    "",
    `# ${name}`,
    "",
    "Explain the purpose and behavior of this hook.",
    "",
  ].join("\n");
}

function defaultHookHandler(): string {
  return [
    "export default async function handler(ctx: any) {",
    "  // TODO: implement hook logic",
    "  return ctx;",
    "}",
    "",
  ].join("\n");
}

function AgentSettingsModal({
  open,
  onClose,
  agentId,
  onError,
}: {
  open: boolean;
  onClose: () => void;
  agentId: string;
  onError: (value: string | null) => void;
}) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<SettingsTab>("skills");
  const [settings, setSettings] = useState<_SettingsData>(() => createEmptySettingsData());
  const [loadingByTab, setLoadingByTab] = useState<Record<SettingsTab, boolean>>({
    skills: false,
    cron: false,
    hooks: false,
    agents: false,
  });
  const [working, setWorking] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [customSectionDraft, setCustomSectionDraft] = useState("");

  const [expandedSkillName, setExpandedSkillName] = useState<string | null>(null);
  const [skillEditorOpen, setSkillEditorOpen] = useSessionBackedModalFlag(
    `openclaw-chat:settings:${agentId}:skill-editor-open`,
  );
  const [skillEditorMode, setSkillEditorMode] = useSessionBackedState<SkillEditorMode>(
    `openclaw-chat:settings:${agentId}:skill-editor-mode`,
    "create",
    { isClosed: (value) => value === "create" },
  );
  const [skillOriginalName, setSkillOriginalName] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:skill-original-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [skillFormName, setSkillFormName] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:skill-form-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [skillFormDescription, setSkillFormDescription] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:skill-form-description`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [skillFormContent, setSkillFormContent] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:skill-form-content`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [skillEditorError, setSkillEditorError] = useSessionBackedState<string | null>(
    `openclaw-chat:settings:${agentId}:skill-editor-error`,
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );

  const [cronEditorOpen, setCronEditorOpen] = useSessionBackedModalFlag(
    `openclaw-chat:settings:${agentId}:cron-editor-open`,
  );
  const [cronEditorMode, setCronEditorMode] = useSessionBackedState<CronEditorMode>(
    `openclaw-chat:settings:${agentId}:cron-editor-mode`,
    "create",
    { isClosed: (value) => value === "create" },
  );
  const [cronEditId, setCronEditId] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-edit-id`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [cronFormName, setCronFormName] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [cronFormMode, setCronFormMode] = useSessionBackedState<CronScheduleMode>(
    `openclaw-chat:settings:${agentId}:cron-form-mode`,
    "weekly",
    { isClosed: (value) => value === "weekly" },
  );
  const [cronFormTime, setCronFormTime] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-time`,
    "03:00",
    { isClosed: (value) => value === "03:00" },
  );
  const [cronFormWeekday, setCronFormWeekday] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-weekday`,
    1,
    { isClosed: (value) => value === 1 },
  );
  const [cronFormDayOfMonth, setCronFormDayOfMonth] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-day-of-month`,
    1,
    { isClosed: (value) => value === 1 },
  );
  const [cronFormMessage, setCronFormMessage] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-message`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [cronFormEnabled, setCronFormEnabled] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:cron-form-enabled`,
    true,
    { isClosed: (value) => value === true },
  );
  const [cronEditorError, setCronEditorError] = useSessionBackedState<string | null>(
    `openclaw-chat:settings:${agentId}:cron-editor-error`,
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const [agentsEditing, setAgentsEditing] = useState(false);

  const [expandedHookName, setExpandedHookName] = useState<string | null>(null);
  const [hookEditorOpen, setHookEditorOpen] = useSessionBackedModalFlag(
    `openclaw-chat:settings:${agentId}:hook-editor-open`,
  );
  const [hookEditorMode, setHookEditorMode] = useSessionBackedState<HookEditorMode>(
    `openclaw-chat:settings:${agentId}:hook-editor-mode`,
    "create",
    { isClosed: (value) => value === "create" },
  );
  const [hookOriginalName, setHookOriginalName] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:hook-original-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [hookFormName, setHookFormName] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:hook-form-name`,
    "",
    { isClosed: (value) => value.trim().length === 0 },
  );
  const [hookFormMd, setHookFormMd] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:hook-form-md`,
    defaultHookMarkdown("custom-hook"),
    { isClosed: (value) => value.trim() === defaultHookMarkdown("custom-hook").trim() },
  );
  const [hookFormHandler, setHookFormHandler] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:hook-form-handler`,
    defaultHookHandler(),
    { isClosed: (value) => value.trim() === defaultHookHandler().trim() },
  );
  const [hookFormEnabled, setHookFormEnabled] = useSessionBackedState(
    `openclaw-chat:settings:${agentId}:hook-form-enabled`,
    true,
    { isClosed: (value) => value === true },
  );
  const [hookEditorError, setHookEditorError] = useSessionBackedState<string | null>(
    `openclaw-chat:settings:${agentId}:hook-editor-error`,
    null,
    { isClosed: (value) => value === null || value.trim().length === 0 },
  );
  const agentsEditingRef = useRef(false);
  const loadSeqRef = useRef(0);

  useEffect(() => {
    agentsEditingRef.current = agentsEditing;
  }, [agentsEditing]);

  const loadSettings = useCallback(async (opts?: { force?: boolean }) => {
    if (!open) return;
    const requestSeq = ++loadSeqRef.current;
    const force = opts?.force === true;
    const now = Date.now();
    const cached = force ? null : SETTINGS_CACHE.get(agentId);
    const cacheValid = !!cached && (now - cached.cachedAt) < SETTINGS_CACHE_TTL_MS;
    const initialData = cacheValid && cached ? cached.data : createEmptySettingsData();

    setSettings(initialData);
    setCustomSectionDraft(initialData.agentsUserCustomSection || "");
    setAgentsEditing(false);
    agentsEditingRef.current = false;
    setLoadingByTab({
      skills: initialData.skills === null,
      cron: initialData.cronJobs === null,
      hooks: initialData.hooks === null,
      agents: initialData.agentsUserCustomSection === null,
    });
    setError(null);

    const applyPatch = (patch: Partial<_SettingsData>) => {
      if (loadSeqRef.current !== requestSeq) return;
      setSettings((prev) => {
        const next = { ...prev, ...patch };
        SETTINGS_CACHE.set(agentId, { data: next, cachedAt: Date.now() });
        return next;
      });
      if (
        patch.agentsUserCustomSection !== undefined
        && !agentsEditingRef.current
      ) {
        setCustomSectionDraft(patch.agentsUserCustomSection || "");
      }
    };

    const completeTab = (key: SettingsTab) => {
      if (loadSeqRef.current !== requestSeq) return;
      setLoadingByTab((prev) => ({ ...prev, [key]: false }));
    };

    const handleTabError = (e: unknown) => {
      const text = formatActionError(e);
      setError((prev) => prev || text);
      onError(text);
    };

    const loadSkills = async () => {
      try {
        const skills = await api.getOpenclawAgentSettingsSkills(agentId);
        applyPatch({ skills });
      } catch (e) {
        handleTabError(e);
      } finally {
        completeTab("skills");
      }
    };

    const loadCron = async () => {
      try {
        const cronJobs = await api.getOpenclawAgentSettingsCron(agentId);
        applyPatch({ cronJobs });
      } catch (e) {
        handleTabError(e);
      } finally {
        completeTab("cron");
      }
    };

    const loadHooks = async () => {
      try {
        const hooks = await api.getOpenclawAgentSettingsHooks(agentId);
        applyPatch({ hooks });
      } catch (e) {
        handleTabError(e);
      } finally {
        completeTab("hooks");
      }
    };

    const loadAgentsSection = async () => {
      try {
        const out = await api.getOpenclawAgentCustomSection(agentId);
        applyPatch({ agentsUserCustomSection: out.content || "" });
      } catch (e) {
        handleTabError(e);
      } finally {
        completeTab("agents");
      }
    };

    if (cacheValid && cached) {
      void Promise.all([
        loadSkills(),
        loadCron(),
        loadHooks(),
        loadAgentsSection(),
      ]);
      return;
    }
    await Promise.all([
      loadSkills(),
      loadCron(),
      loadHooks(),
      loadAgentsSection(),
    ]);
  }, [agentId, onError, open]);

  useEffect(() => {
    if (!open) return;
    void loadSettings();
  }, [open, loadSettings]);

  async function runAction(
    action: () => Promise<void>,
    opts?: {
      setScopedError?: (value: string | null) => void;
    },
  ) {
    if (working) return;
    const setScopedError = opts?.setScopedError;
    setWorking(true);
    setError(null);
    setScopedError?.(null);
    try {
      await action();
      await loadSettings({ force: true });
    } catch (e) {
      const text = formatActionError(e);
      if (setScopedError) {
        setScopedError(text);
      } else {
        setError(text);
        onError(text);
      }
    } finally {
      setWorking(false);
    }
  }

  function openCreateSkillEditor() {
    setSkillEditorMode("create");
    setSkillOriginalName("");
    setSkillFormName("");
    setSkillFormDescription("");
    setSkillFormContent("");
    setSkillEditorError(null);
    setSkillEditorOpen(true);
  }

  function openEditSkillEditor(skill: OpenclawAgentSkillSetting) {
    setSkillEditorMode("edit");
    setSkillOriginalName(skill.name);
    setSkillFormName(skill.name);
    setSkillFormDescription(skill.description || "");
    setSkillFormContent(skill.content || "");
    setSkillEditorError(null);
    setSkillEditorOpen(true);
  }

  async function submitSkillEditor() {
    const name = skillFormName.trim();
    const description = skillFormDescription.trim();
    const content = skillFormContent.trim();
    if (!name || !description || !content) {
      setSkillEditorError(t("chat.settings.skills.form.required"));
      return;
    }
    await runAction(async () => {
      if (skillEditorMode === "create") {
        await api.createOpenclawAgentSkill(agentId, { name, description, content });
      } else {
        await api.patchOpenclawAgentSkill(agentId, skillOriginalName, { name, description, content });
      }
      setSkillEditorOpen(false);
    }, { setScopedError: setSkillEditorError });
  }

  function openCreateCronEditor() {
    setCronEditorMode("create");
    setCronEditId("");
    setCronFormName("");
    setCronFormMode("weekly");
    setCronFormTime("03:00");
    setCronFormWeekday(1);
    setCronFormDayOfMonth(1);
    setCronFormMessage("");
    setCronFormEnabled(true);
    setCronEditorError(null);
    setCronEditorOpen(true);
  }

  function openEditCronEditor(item: OpenclawAgentCronSetting) {
    const parsed = parseSimpleCronSchedule(item.scheduleExpr || "");
    if (!parsed) {
      setError(t("chat.settings.cron.form.unsupportedExpr"));
      return;
    }
    setCronEditorMode("edit");
    setCronEditId(item.id);
    setCronFormName(item.name);
    setCronFormMode(parsed.mode);
    setCronFormTime(parsed.time);
    setCronFormWeekday(parsed.weekday);
    setCronFormDayOfMonth(parsed.dayOfMonth);
    setCronFormMessage(item.message || "");
    setCronFormEnabled(item.enabled);
    setCronEditorError(null);
    setCronEditorOpen(true);
  }

  async function submitCronEditor() {
    const name = cronFormName.trim();
    const message = cronFormMessage.trim();
    const scheduleTime = cronFormTime.trim();
    if (!name || !message || !scheduleTime) {
      setCronEditorError(t("chat.settings.cron.form.required"));
      return;
    }
    if (cronFormMode === "monthly" && (cronFormDayOfMonth < 1 || cronFormDayOfMonth > 31)) {
      setCronEditorError(t("chat.settings.cron.form.dayOfMonthInvalid"));
      return;
    }
    await runAction(async () => {
      const payload = {
        name,
        scheduleMode: cronFormMode,
        scheduleTime,
        scheduleWeekday: cronFormMode === "weekly" ? cronFormWeekday : undefined,
        scheduleDayOfMonth: cronFormMode === "monthly" ? cronFormDayOfMonth : undefined,
        message,
        enabled: cronFormEnabled,
      } as const;
      if (cronEditorMode === "create") {
        await api.createOpenclawAgentCron(agentId, payload);
      } else {
        await api.patchOpenclawAgentCron(agentId, cronEditId, payload);
      }
      setCronEditorOpen(false);
    }, { setScopedError: setCronEditorError });
  }

  function openCreateHookEditor() {
    const defaultName = "custom-hook";
    setHookEditorMode("create");
    setHookOriginalName("");
    setHookFormName(defaultName);
    setHookFormMd(defaultHookMarkdown(defaultName));
    setHookFormHandler(defaultHookHandler());
    setHookFormEnabled(true);
    setHookEditorError(null);
    setHookEditorOpen(true);
  }

  function openEditHookEditor(item: OpenclawAgentHookSetting) {
    setHookEditorMode("edit");
    setHookOriginalName(item.name);
    setHookFormName(item.name);
    setHookFormMd(item.hookMd?.trim() || defaultHookMarkdown(item.name));
    setHookFormHandler(item.handlerTs?.trim() || defaultHookHandler());
    setHookFormEnabled(item.enabled);
    setHookEditorError(null);
    setHookEditorOpen(true);
  }

  async function submitHookEditor() {
    const name = hookFormName.trim();
    const hookMd = hookFormMd.trim();
    const handlerTs = hookFormHandler.trim();
    if (!name || !hookMd || !handlerTs) {
      setHookEditorError(t("chat.settings.hooks.form.required"));
      return;
    }
    await runAction(async () => {
      if (hookEditorMode === "create") {
        await api.createOpenclawAgentHook(agentId, {
          name,
          hookMd,
          handlerTs,
          enabled: hookFormEnabled,
        });
      } else {
        await api.patchOpenclawAgentHook(agentId, hookOriginalName, {
          name,
          hookMd,
          handlerTs,
          enabled: hookFormEnabled,
        });
      }
      setHookEditorOpen(false);
    }, { setScopedError: setHookEditorError });
  }

  const skillItems = settings.skills;
  const cronItems = settings.cronJobs;
  const hookItems = settings.hooks;

  return (
    <>
      <Modal
        open={open}
        onClose={() => {
          if (working) return;
          onClose();
        }}
        title={t("chat.settings.title")}
        width="max-w-5xl"
      >
        <div className="space-y-4">
          <div className="rounded-xl border border-ink-100 bg-ink-50/60 p-1.5">
            <div className="grid grid-cols-4 gap-1">
              {(["skills", "cron", "hooks", "agents"] as SettingsTab[]).map((item) => (
                <button
                  key={item}
                  type="button"
                  className={cn(
                    "rounded-lg px-3 py-2 text-sm font-medium transition",
                    tab === item
                      ? "bg-white text-brand-700 shadow-sm ring-1 ring-brand-200"
                      : "text-ink-600 hover:text-ink-900",
                  )}
                  onClick={() => setTab(item)}
                  disabled={working}
                >
                  {t(`chat.settings.tabs.${item}`)}
                </button>
              ))}
            </div>
          </div>

          {error ? <ErrorBox>{error}</ErrorBox> : null}
          <div className="space-y-3">
              {tab === "skills" ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="text-sm text-ink-600">{t("chat.settings.skills.hint")}</div>
                    <button type="button" className="btn-primary" disabled={working} onClick={openCreateSkillEditor}>
                      {t("chat.settings.skills.add")}
                    </button>
                  </div>
                  {loadingByTab.skills && skillItems === null ? (
                    <Loading label={t("chat.settings.loading")} />
                  ) : skillItems === null || skillItems.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-ink-200 px-4 py-6 text-sm text-ink-500">
                      {t("chat.settings.skills.empty")}
                    </div>
                  ) : (
                    skillItems.map((item) => (
                      <div key={item.name} className="rounded-xl border border-ink-200 bg-white p-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate text-sm font-semibold text-ink-900">{item.name}</div>
                            <div className="truncate text-xs text-black">{item.description || t("chat.settings.skills.noDescription")}</div>
                            <div className="truncate text-xs text-ink-500">{item.path}</div>
                          </div>
                          <div className="inline-flex items-center gap-2">
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs"
                              disabled={working}
                              onClick={() =>
                                setExpandedSkillName((prev) => (prev === item.name ? null : item.name))
                              }
                            >
                              {expandedSkillName === item.name
                                ? t("chat.settings.common.hide")
                                : t("chat.settings.common.view")}
                            </button>
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs"
                              disabled={working}
                              onClick={() => openEditSkillEditor(item)}
                            >
                              {t("chat.settings.common.edit")}
                            </button>
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs !border-rose-300 !text-rose-600"
                              disabled={working}
                              onClick={() => {
                                if (!window.confirm(t("chat.settings.skills.deleteConfirm", { name: item.name }))) {
                                  return;
                                }
                                void runAction(async () => {
                                  await api.deleteOpenclawAgentSkill(agentId, item.name);
                                });
                              }}
                            >
                              {t("chat.settings.common.delete")}
                            </button>
                          </div>
                        </div>
                        {expandedSkillName === item.name ? (
                          <pre className="mt-3 max-h-64 overflow-auto rounded-lg border border-ink-200 bg-white p-3 text-xs text-black">
                            {item.content || t("chat.settings.skills.noContent")}
                          </pre>
                        ) : null}
                      </div>
                    ))
                  )}
                </div>
              ) : null}

              {tab === "cron" ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="text-sm text-ink-600">{t("chat.settings.cron.hint")}</div>
                    <button type="button" className="btn-primary" disabled={working} onClick={openCreateCronEditor}>
                      {t("chat.settings.cron.add")}
                    </button>
                  </div>
                  {loadingByTab.cron && cronItems === null ? (
                    <Loading label={t("chat.settings.loading")} />
                  ) : cronItems === null || cronItems.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-ink-200 px-4 py-6 text-sm text-ink-500">
                      {t("chat.settings.cron.empty")}
                    </div>
                  ) : (
                    cronItems.map((item) => (
                      <div key={item.id} className="rounded-xl border border-ink-200 bg-white p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-semibold text-ink-900">{item.name}</span>
                              <span
                                className={cn(
                                  "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                                  item.systemBuiltin
                                    ? "bg-amber-100 text-amber-700"
                                    : "bg-brand-100 text-brand-700",
                                )}
                              >
                                {item.systemBuiltin
                                  ? t("chat.settings.cron.builtin")
                                  : t("chat.settings.cron.custom")}
                              </span>
                              <span
                                className={cn(
                                  "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                                  item.enabled
                                    ? "bg-emerald-100 text-emerald-700"
                                    : "bg-ink-200 text-ink-600",
                                )}
                              >
                                {item.enabled
                                  ? t("chat.settings.common.enabled")
                                  : t("chat.settings.common.disabled")}
                              </span>
                            </div>
                            <div className="text-xs text-ink-600">
                              <code>{item.scheduleExpr || "-"}</code>
                              <span className="mx-2 text-ink-300">|</span>
                              <span>{item.scheduleTz || "UTC"}</span>
                            </div>
                            <div className="text-xs text-black">{item.message}</div>
                          </div>
                          <div className="inline-flex flex-wrap items-center justify-end gap-2">
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs"
                              disabled={working}
                              onClick={() =>
                                void runAction(async () => {
                                  if (item.enabled) {
                                    await api.disableOpenclawAgentCron(agentId, item.id);
                                  } else {
                                    await api.enableOpenclawAgentCron(agentId, item.id);
                                  }
                                })
                              }
                            >
                              {item.enabled
                                ? t("chat.settings.common.disable")
                                : t("chat.settings.common.enable")}
                            </button>
                            {item.canEdit ? (
                              <button
                                type="button"
                                className="btn-outline !px-2.5 !py-1 text-xs"
                                disabled={working}
                                onClick={() => openEditCronEditor(item)}
                              >
                                {t("chat.settings.common.edit")}
                              </button>
                            ) : null}
                            {item.canDelete ? (
                              <button
                                type="button"
                                className="btn-outline !px-2.5 !py-1 text-xs !border-rose-300 !text-rose-600"
                                disabled={working}
                                onClick={() => {
                                  if (!window.confirm(t("chat.settings.cron.deleteConfirm", { name: item.name }))) {
                                    return;
                                  }
                                  void runAction(async () => {
                                    await api.deleteOpenclawAgentCron(agentId, item.id);
                                  });
                                }}
                              >
                                {t("chat.settings.common.delete")}
                              </button>
                            ) : null}
                          </div>
                        </div>
                      </div>
                    ))
                  )}
                </div>
              ) : null}

              {tab === "hooks" ? (
                <div className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div className="text-sm text-ink-600">{t("chat.settings.hooks.hint")}</div>
                    <button type="button" className="btn-primary" disabled={working} onClick={openCreateHookEditor}>
                      {t("chat.settings.hooks.add")}
                    </button>
                  </div>
                  {loadingByTab.hooks && hookItems === null ? (
                    <Loading label={t("chat.settings.loading")} />
                  ) : hookItems === null || hookItems.length === 0 ? (
                    <div className="rounded-lg border border-dashed border-ink-200 px-4 py-6 text-sm text-ink-500">
                      {t("chat.settings.hooks.empty")}
                    </div>
                  ) : (
                    hookItems.map((item) => (
                      <div key={item.name} className="rounded-xl border border-ink-200 bg-white p-3">
                        <div className="flex items-start justify-between gap-3">
                          <div className="space-y-1">
                            <div className="flex items-center gap-2">
                              <span className="text-sm font-semibold text-ink-900">{item.name}</span>
                              <span
                                className={cn(
                                  "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                                  item.systemBuiltin
                                    ? "bg-amber-100 text-amber-700"
                                    : "bg-brand-100 text-brand-700",
                                )}
                              >
                                {item.systemBuiltin
                                  ? t("chat.settings.hooks.builtin")
                                  : t("chat.settings.hooks.custom")}
                              </span>
                              <span
                                className={cn(
                                  "rounded-full px-2 py-0.5 text-[10px] font-semibold",
                                  item.enabled
                                    ? "bg-emerald-100 text-emerald-700"
                                    : "bg-ink-200 text-ink-600",
                                )}
                              >
                                {item.enabled
                                  ? t("chat.settings.common.enabled")
                                  : t("chat.settings.common.disabled")}
                              </span>
                            </div>
                            <div className="text-xs text-black">{item.description || t("chat.settings.hooks.noDescription")}</div>
                            <div className="text-xs text-black">
                              {item.events.length > 0 ? item.events.join(", ") : t("chat.settings.hooks.noEvents")}
                            </div>
                          </div>
                          <div className="inline-flex flex-wrap items-center justify-end gap-2">
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs"
                              disabled={working}
                              onClick={() =>
                                void runAction(async () => {
                                  if (item.enabled) {
                                    await api.disableOpenclawAgentHook(agentId, item.name);
                                  } else {
                                    await api.enableOpenclawAgentHook(agentId, item.name);
                                  }
                                })
                              }
                            >
                              {item.enabled
                                ? t("chat.settings.common.disable")
                                : t("chat.settings.common.enable")}
                            </button>
                            <button
                              type="button"
                              className="btn-outline !px-2.5 !py-1 text-xs"
                              disabled={working}
                              onClick={() =>
                                setExpandedHookName((prev) => (prev === item.name ? null : item.name))
                              }
                            >
                              {expandedHookName === item.name
                                ? t("chat.settings.common.hide")
                                : t("chat.settings.common.view")}
                            </button>
                            {item.canEdit ? (
                              <button
                                type="button"
                                className="btn-outline !px-2.5 !py-1 text-xs"
                                disabled={working}
                                onClick={() => openEditHookEditor(item)}
                              >
                                {t("chat.settings.common.edit")}
                              </button>
                            ) : null}
                            {item.canDelete ? (
                              <button
                                type="button"
                                className="btn-outline !px-2.5 !py-1 text-xs !border-rose-300 !text-rose-600"
                                disabled={working}
                                onClick={() => {
                                  if (!window.confirm(t("chat.settings.hooks.deleteConfirm", { name: item.name }))) {
                                    return;
                                  }
                                  void runAction(async () => {
                                    await api.deleteOpenclawAgentHook(agentId, item.name);
                                  });
                                }}
                              >
                                {t("chat.settings.common.delete")}
                              </button>
                            ) : null}
                          </div>
                        </div>
                        {expandedHookName === item.name ? (
                          <div className="mt-3 grid gap-3 lg:grid-cols-2">
                            <pre className="max-h-56 overflow-auto rounded-lg border border-ink-200 bg-white p-3 text-xs text-black">
                              {item.hookMd || t("chat.settings.hooks.noHookMd")}
                            </pre>
                            <pre className="max-h-56 overflow-auto rounded-lg border border-ink-200 bg-white p-3 text-xs text-black">
                              {item.handlerTs || t("chat.settings.hooks.noHandler")}
                            </pre>
                          </div>
                        ) : null}
                      </div>
                    ))
                  )}
                </div>
              ) : null}

              {tab === "agents" ? (
                <div className="space-y-3">
                  {loadingByTab.agents && settings.agentsUserCustomSection === null ? (
                    <Loading label={t("chat.settings.loading")} />
                  ) : (
                    <>
                  <div className="flex items-center justify-between gap-3">
                    <div className="text-sm text-ink-600">{t("chat.settings.agents.hint")}</div>
                    {!agentsEditing ? (
                      <button
                        type="button"
                        className="btn-outline !px-2.5 !py-1 text-xs"
                        disabled={working}
                        onClick={() => setAgentsEditing(true)}
                      >
                        {t("chat.settings.common.edit")}
                      </button>
                    ) : null}
                  </div>
                  {agentsEditing ? (
                    <textarea
                      className="textarea h-72 w-full resize-y text-black"
                      value={customSectionDraft}
                      onChange={(e) => setCustomSectionDraft(e.target.value)}
                      disabled={working}
                      placeholder={t("chat.settings.agents.placeholder")}
                    />
                  ) : (
                    <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-ink-200 bg-white p-3 text-sm text-black">
                      {customSectionDraft.trim() || t("chat.settings.agents.placeholder")}
                    </pre>
                  )}
                  {agentsEditing ? (
                    <div className="flex justify-end gap-2">
                      <button
                        type="button"
                        className="btn-outline"
                        disabled={working}
                        onClick={() => {
                          setAgentsEditing(false);
                          setCustomSectionDraft(settings.agentsUserCustomSection || "");
                        }}
                      >
                        {t("common.cancel")}
                      </button>
                      <button
                        type="button"
                        className="btn-primary"
                        disabled={working}
                        onClick={() =>
                          void runAction(async () => {
                            await api.updateOpenclawAgentCustomSection(agentId, customSectionDraft);
                            setAgentsEditing(false);
                          })
                        }
                      >
                        {working ? t("chat.settings.common.saving") : t("chat.settings.common.save")}
                      </button>
                    </div>
                  ) : (
                    <div className="text-xs text-ink-500">
                      {t("chat.settings.agents.readonlyHint")}
                    </div>
                  )}
                    </>
                  )}
                </div>
              ) : null}
            </div>
        </div>
      </Modal>

      <Modal
        open={skillEditorOpen}
        onClose={() => {
          if (working) return;
          setSkillEditorError(null);
          setSkillEditorOpen(false);
        }}
        title={
          skillEditorMode === "create"
            ? t("chat.settings.skills.form.createTitle")
            : t("chat.settings.skills.form.editTitle")
        }
      >
        <div className="space-y-3">
          {skillEditorError ? <ErrorBox>{skillEditorError}</ErrorBox> : null}
          <div>
            <label className="label">{t("chat.settings.skills.form.name")}</label>
            <input
              className="input"
              value={skillFormName}
              onChange={(e) => {
                setSkillFormName(e.target.value);
                setSkillEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.skills.form.namePlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("chat.settings.skills.form.description")}</label>
            <input
              className="input"
              value={skillFormDescription}
              onChange={(e) => {
                setSkillFormDescription(e.target.value);
                setSkillEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.skills.form.descriptionPlaceholder")}
            />
          </div>
          <div>
            <label className="label">{t("chat.settings.skills.form.content")}</label>
            <textarea
              className="textarea h-56 w-full resize-y"
              value={skillFormContent}
              onChange={(e) => {
                setSkillFormContent(e.target.value);
                setSkillEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.skills.form.contentPlaceholder")}
            />
          </div>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setSkillEditorError(null);
                setSkillEditorOpen(false);
              }}
              disabled={working}
            >
              {t("common.cancel")}
            </button>
            <button type="button" className="btn-primary" onClick={() => void submitSkillEditor()} disabled={working}>
              {working ? t("chat.settings.common.saving") : t("chat.settings.common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={cronEditorOpen}
        onClose={() => {
          if (working) return;
          setCronEditorError(null);
          setCronEditorOpen(false);
        }}
        title={
          cronEditorMode === "create"
            ? t("chat.settings.cron.form.createTitle")
            : t("chat.settings.cron.form.editTitle")
        }
      >
        <div className="space-y-3">
          {cronEditorError ? <ErrorBox>{cronEditorError}</ErrorBox> : null}
          <div>
            <label className="label">{t("chat.settings.cron.form.name")}</label>
            <input
              className="input"
              value={cronFormName}
              onChange={(e) => {
                setCronFormName(e.target.value);
                setCronEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.cron.form.namePlaceholder")}
            />
          </div>
          <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
            <div>
              <label className="label">{t("chat.settings.cron.form.mode")}</label>
              <select
                className="select"
                value={cronFormMode}
                onChange={(e) => {
                  setCronFormMode(e.target.value as CronScheduleMode);
                  setCronEditorError(null);
                }}
                disabled={working}
              >
                <option value="daily">{t("chat.settings.cron.form.modeDaily")}</option>
                <option value="weekly">{t("chat.settings.cron.form.modeWeekly")}</option>
                <option value="monthly">{t("chat.settings.cron.form.modeMonthly")}</option>
              </select>
            </div>
            <div>
              <label className="label">{t("chat.settings.cron.form.time")}</label>
              <input
                className="input"
                type="time"
                value={cronFormTime}
                onChange={(e) => {
                  setCronFormTime(e.target.value);
                  setCronEditorError(null);
                }}
                disabled={working}
              />
            </div>
          </div>
          {cronFormMode === "weekly" ? (
            <div>
              <label className="label">{t("chat.settings.cron.form.weekday")}</label>
              <select
                className="select"
                value={cronFormWeekday}
                onChange={(e) => {
                  setCronFormWeekday(Number(e.target.value));
                  setCronEditorError(null);
                }}
                disabled={working}
              >
                <option value={1}>{t("chat.settings.cron.form.weekdayMon")}</option>
                <option value={2}>{t("chat.settings.cron.form.weekdayTue")}</option>
                <option value={3}>{t("chat.settings.cron.form.weekdayWed")}</option>
                <option value={4}>{t("chat.settings.cron.form.weekdayThu")}</option>
                <option value={5}>{t("chat.settings.cron.form.weekdayFri")}</option>
                <option value={6}>{t("chat.settings.cron.form.weekdaySat")}</option>
                <option value={0}>{t("chat.settings.cron.form.weekdaySun")}</option>
              </select>
            </div>
          ) : null}
          {cronFormMode === "monthly" ? (
            <div>
              <label className="label">{t("chat.settings.cron.form.dayOfMonth")}</label>
              <input
                className="input"
                type="number"
                min={1}
                max={31}
                value={cronFormDayOfMonth}
                onChange={(e) => {
                  setCronFormDayOfMonth(Number(e.target.value));
                  setCronEditorError(null);
                }}
                disabled={working}
              />
            </div>
          ) : null}
          <div>
            <label className="label">{t("chat.settings.cron.form.message")}</label>
            <textarea
              className="textarea h-28 w-full resize-y"
              value={cronFormMessage}
              onChange={(e) => {
                setCronFormMessage(e.target.value);
                setCronEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.cron.form.messagePlaceholder")}
            />
          </div>
          <label className="inline-flex items-center gap-2 text-sm text-ink-700">
            <input
              type="checkbox"
              checked={cronFormEnabled}
              onChange={(e) => {
                setCronFormEnabled(e.target.checked);
                setCronEditorError(null);
              }}
              disabled={working}
            />
            {t("chat.settings.cron.form.enabled")}
          </label>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setCronEditorError(null);
                setCronEditorOpen(false);
              }}
              disabled={working}
            >
              {t("common.cancel")}
            </button>
            <button type="button" className="btn-primary" onClick={() => void submitCronEditor()} disabled={working}>
              {working ? t("chat.settings.common.saving") : t("chat.settings.common.save")}
            </button>
          </div>
        </div>
      </Modal>

      <Modal
        open={hookEditorOpen}
        onClose={() => {
          if (working) return;
          setHookEditorError(null);
          setHookEditorOpen(false);
        }}
        title={
          hookEditorMode === "create"
            ? t("chat.settings.hooks.form.createTitle")
            : t("chat.settings.hooks.form.editTitle")
        }
        width="max-w-4xl"
      >
        <div className="space-y-3">
          {hookEditorError ? <ErrorBox>{hookEditorError}</ErrorBox> : null}
          <div>
            <label className="label">{t("chat.settings.hooks.form.name")}</label>
            <input
              className="input"
              value={hookFormName}
              onChange={(e) => {
                setHookFormName(e.target.value);
                setHookEditorError(null);
              }}
              disabled={working}
              placeholder={t("chat.settings.hooks.form.namePlaceholder")}
            />
          </div>
          <div className="grid grid-cols-1 gap-3 lg:grid-cols-2">
            <div>
              <label className="label">{t("chat.settings.hooks.form.hookMd")}</label>
              <textarea
                className="textarea h-64 w-full resize-y font-mono text-xs"
                value={hookFormMd}
                onChange={(e) => {
                  setHookFormMd(e.target.value);
                  setHookEditorError(null);
                }}
                disabled={working}
              />
            </div>
            <div>
              <label className="label">{t("chat.settings.hooks.form.handlerTs")}</label>
              <textarea
                className="textarea h-64 w-full resize-y font-mono text-xs"
                value={hookFormHandler}
                onChange={(e) => {
                  setHookFormHandler(e.target.value);
                  setHookEditorError(null);
                }}
                disabled={working}
              />
            </div>
          </div>
          <label className="inline-flex items-center gap-2 text-sm text-ink-700">
            <input
              type="checkbox"
              checked={hookFormEnabled}
              onChange={(e) => {
                setHookFormEnabled(e.target.checked);
                setHookEditorError(null);
              }}
              disabled={working}
            />
            {t("chat.settings.hooks.form.enabled")}
          </label>
          <div className="flex justify-end gap-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => {
                setHookEditorError(null);
                setHookEditorOpen(false);
              }}
              disabled={working}
            >
              {t("common.cancel")}
            </button>
            <button type="button" className="btn-primary" onClick={() => void submitHookEditor()} disabled={working}>
              {working ? t("chat.settings.common.saving") : t("chat.settings.common.save")}
            </button>
          </div>
        </div>
      </Modal>
    </>
  );
}

function buildOpenclawChatUrl(agentId: string, gatewayUrl?: string | null): string {
  const suffix = `/?agentId=${encodeURIComponent(agentId)}`;
  const normalizedGatewayUrl = (gatewayUrl || "").trim();
  if (normalizedGatewayUrl) {
    try {
      const parsed = new URL(normalizedGatewayUrl);
      return `${parsed.origin}${suffix}`;
    } catch {
      // ignore invalid runtime URL and fall back to legacy default
    }
  }
  if (typeof window === "undefined") return `http://127.0.0.1:18789${suffix}`;
  const protocol = window.location.protocol;
  const hostname = window.location.hostname;
  return `${protocol}//${hostname}:18789${suffix}`;
}

function isRemoteBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
}

function buildAgentMyDesktopPath(workspacePath: string): string {
  const base = (workspacePath || "").trim().replace(/[\\/]+$/, "");
  if (!base) return "my-desktop";
  const sep = base.includes("\\") && !base.includes("/") ? "\\" : "/";
  return `${base}${sep}my-desktop`;
}

function Bubble({
  msg,
  pending,
  noTextReply,
}: {
  msg: Message;
  pending?: boolean;
  noTextReply: string;
}) {
  const isUser = msg.role === "user";
  return (
    <div className={isUser ? "flex justify-end" : "flex justify-start"}>
      <div
        className={
          isUser
            ? "bg-brand-500 text-white rounded-2xl rounded-tr-sm px-4 py-2 max-w-[75%] whitespace-pre-wrap text-sm"
            : "bg-white border border-ink-200 text-ink-800 rounded-2xl rounded-tl-sm px-4 py-2 max-w-[75%] shadow-card"
        }
      >
        {msg.content ? (
          isUser ? (
            msg.content
          ) : (
            <ChatMarkdown content={msg.content} />
          )
        ) : pending ? (
          <TypingDots />
        ) : (
          <span className="text-ink-400">{noTextReply}</span>
        )}
      </div>
    </div>
  );
}

function TypingDots() {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = setInterval(() => {
      setTick((v) => (v + 1) % 3);
    }, 350);
    return () => clearInterval(timer);
  }, []);
  return <span className="text-ink-400">{".".repeat(tick + 1)}</span>;
}

/** Extract text delta from an OpenAI-style chat completion chunk.
 *
 * Handles both ``choices[0].delta.content`` (chat completions) and the
 * single-message variant ``choices[0].message.content``.
 */
function extractDelta(chunk: any): string {
  const c = chunk?.choices?.[0];
  if (!c) return "";
  if (c.delta?.content) return String(c.delta.content);
  if (c.message?.content) return String(c.message.content);
  return "";
}

