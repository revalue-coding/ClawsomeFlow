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

import {
  Fragment,
  FormEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
  type ReactNode,
} from "react";
import { useLocation, useNavigate, useParams } from "react-router-dom";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import {
  ApiError,
  ChatAttachmentMeta,
  isNetworkError,
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
import { useDialog } from "@/components/dialog";
import { ChatMarkdown } from "@/components/ChatMarkdown";
import { CopyButton, NewMessagesDivider, PendingReply } from "@/components/ChatBubble";
import { AgentCardAvatar } from "@/components/AgentCardAvatar";
import {
  AgentManagementHeader,
  AgentViewModeToggle,
} from "@/components/AgentPageToolbar";
import { DesktopIcon, EditIcon, PlusIcon, RefreshIcon, SettingsIcon, StoreIcon, TrashIcon } from "@/components/icons";
import { handleChatTextareaEnterKey } from "@/lib/chatInput";
import { cn } from "@/lib/cn";
import { resolveDroppedFolderPath } from "@/lib/chatDropFolder";
import { alertIfNativeDirectoryBlocked, getNativeDirectoryBlockedMessage, isRemoteBrowser } from "@/lib/remoteClient";
import {
  clearChatHistory,
  formatChatTime,
  loadChatHistory,
  loadLastSeenCount,
  normalizeAssistantContent,
  reconcileTranscript,
  saveChatHistory,
  saveLastSeenCount,
  scrollToNewMessagesDivider,
  settledCount,
  turnDividerIndex,
  displayChatMessages,
} from "@/lib/chatHistory";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";
import { useOpRecovery } from "@/lib/useOpRecovery";
import { useNavigationGuard } from "@/lib/useNavigationGuard";
import {
  isCreateCancelConverged,
  isOpenclawCancelArmed,
  waitForCancelArmed,
} from "@/lib/createCancelVerify";
import { useAutoGrowTextarea } from "@/lib/useAutoGrowTextarea";
import { useStickyScroll } from "@/lib/useStickyScroll";

interface Message {
  role: "user" | "assistant" | "system";
  content: string;
  attachments?: ChatAttachmentMeta[];
  ts?: number;
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
const CHAT_MAX_ATTACHMENTS = 8;
const CHAT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const OPENCLAW_PENDING_FILES_CACHE = new Map<string, File[]>();

type CreateCancelState = {
  agentId: string;
  cancelling: boolean;
  /**
   * Cancel is *safe* now — the backend has finished scaffolding and registered
   * the op, so cancelling can no longer leave residual data. The button stays
   * disabled until this flips true (see {@link isOpenclawCancelArmed}).
   */
  armed?: boolean;
  /** Cancel did not fully converge — user may force-close; cleanup banner persists. */
  failed?: boolean;
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
  const [runtimeNetDown, setRuntimeNetDown] = useState(false);
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
    setRuntimeNetDown(false);
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
      // Backend unreachable (service down) ≠ OpenClaw gateway down — say so.
      setRuntimeNetDown(isNetworkError(e));
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
            {runtimeNetDown
              ? t("common.serviceUnreachableTitle")
              : t("chat.runtimeUnavailableMessage")}
          </p>
          {runtimeNetDown && (
            <p className="mt-2 whitespace-pre-line text-sm text-ink-600">
              {t("common.serviceUnreachableHint")}
            </p>
          )}
          {!runtimeNetDown && runtimeCheckError && (
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

  return (
    <>
      <AgentQuickActions>
        {(actions) => (id ? null : <ChatPicker actions={actions} />)}
      </AgentQuickActions>
      {id ? <ChatRoom agentId={id} runtimeGatewayUrl={runtimeGatewayUrl} /> : null}
    </>
  );
}

type OpenclawPickerActions = {
  openCreate: () => void;
  openRestore: () => void;
  openImport: () => void;
  openRemoveFor: (agent: OpenclawAgentSummary) => void;
};

// ──────────────────────────────────────────────────────────────────────


function AgentQuickActions({
  children,
}: {
  children?: (actions: OpenclawPickerActions) => ReactNode;
}) {
  const { t } = useTranslation();
  const { confirm, alert } = useDialog();
  const location = useLocation();
  const navigate = useNavigate();
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [teamsLoading, setTeamsLoading] = useState(false);
  const [teamsError, setTeamsError] = useState<string | null>(null);
  const [createModalOpen, setCreateModalOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:create-modal-open",
  );
  const [createAgentId, setCreateAgentId] = useSessionBackedState("openclaw:create:agentId", "");
  const [createAgentName, setCreateAgentName] = useSessionBackedState("openclaw:create:agentName", "");
  const [createResponsibility, setCreateResponsibility] = useSessionBackedState("openclaw:create:responsibility", "");
  const [createExtra, setCreateExtra] = useSessionBackedState("openclaw:create:extra", "");
  const [createTeamChoice, setCreateTeamChoice] = useSessionBackedState("openclaw:create:teamChoice", "");
  const [createTeamName, setCreateTeamName] = useSessionBackedState("openclaw:create:teamName", "");
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
  // Session-backed (not transient) so the "running" status is stably maintained
  // across SPA-nav / refresh: useSessionBackedState's setter writes through to
  // storage synchronously even after unmount, so when the create finishes while
  // we're navigated away, the popup restores in its true terminal state instead
  // of flashing back to a stale "running" with a close button. isClosed clears
  // the key on the default (false) so a fresh visit starts clean.
  const [workPopupRunning, setWorkPopupRunning] = useSessionBackedState(
    "openclaw-chat:quick-actions:work-popup-running",
    false,
    { isClosed: (value) => value === false },
  );
  // Session-backed so a *failed* operation that finishes while we're unmounted
  // restores in red, not just as green text: useSessionBackedState's setter
  // writes to storage synchronously even after unmount, so the failure outcome
  // survives a tab switch. Only persist the failure (false); success is the
  // default and clears the key.
  const [workPopupSuccess, setWorkPopupSuccess] = useSessionBackedState(
    "openclaw-chat:quick-actions:work-popup-success",
    true,
    { isClosed: (value) => value === true },
  );
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
  const [createCancelCleanupNotice, setCreateCancelCleanupNotice] = useSessionBackedState<
    string | null
  >("openclaw-chat:cancel-cleanup-notice", null, { isClosed: (v) => v === null });
  // Import cancel mirrors create cancel (same shape) — distinct op so it has its
  // own state. `agentId` holds the batch id. Chosen semantics: cancel keeps
  // already-imported agents and stops the rest.
  const [importCancelState, setImportCancelState] = useSessionBackedState<CreateCancelState | null>(
    "openclaw-chat:quick-actions:import-cancel-state",
    null,
    { isClosed: (value) => value === null },
  );
  const importCancelRequestedRef = useRef(false);
  const importArmPollInFlightRef = useRef(false);
  const importVerifyInFlightRef = useRef(false);
  const createRequestAbortRef = useRef<AbortController | null>(null);
  const createCancelRequestedRef = useRef(false);
  // Guards against a double-submit firing two POST /openclaw/agents for the same
  // id — which on the backend would race into one shared workspace and the
  // loser's rollback could wipe the winner's files.
  const createRequestInFlightRef = useRef(false);
  const cancelVerifyInFlightRef = useRef(false);
  // Single-flights the "is it safe to cancel yet?" poll so a remount / retry
  // can't stack duplicate loops for the same create.
  const armPollInFlightRef = useRef(false);
  const [storeComingSoonOpen, setStoreComingSoonOpen] = useSessionBackedModalFlag(
    "openclaw-chat:quick-actions:store-coming-soon-open",
  );

  // Block leaving the page while an irreversible/in-flight operation runs, so a
  // remove / restore / create-cancellation / import-cancellation can't be
  // orphaned mid-flight. react-router allows a single blocker at a time, so every
  // condition is folded into one expression here.
  const navGuardActive =
    removeSubmitting ||
    restoreSubmitting ||
    createCancelState?.cancelling === true ||
    importCancelState?.cancelling === true;
  useNavigationGuard(navGuardActive, () => {
    void alert(t("common.navGuardBusy"));
  });

  // Durable recovery across refresh / tab close+reopen (OpenClaw create
  // previously had NO recovery — a remount mid-build left the popup stuck). A
  // localStorage pointer + on-mount status query (+ WS for the terminal frame).
  const { track: trackOp, clear: clearOp } = useOpRecovery("openclaw:create:op", {
    onRunning: (p) => {
      // A cancel may already be resuming on this same mount (it sets the ref
      // synchronously in its own effect); don't clobber its cancelling state.
      if (createCancelRequestedRef.current) return;
      setCreateCancelState({ agentId: p.agentId, cancelling: false, armed: false });
      openWorkPopup();
      // Recovered mid-create (refresh / reopen): re-derive whether cancel is safe
      // yet rather than trusting a stale armed flag.
      void armCancelWhenSafe(p.agentId);
    },
    onSucceeded: (p) => {
      if (createCancelRequestedRef.current) return;
      finishWorkPopup(true, t("assistant.workPopup.createdWithId", { id: p.agentId }));
      notifyOpenclawAgentsUpdated();
    },
    onFailed: (_p, detail) => {
      if (createCancelRequestedRef.current) return;
      finishWorkPopup(
        false,
        detail === "cancelled" || detail === "bootstrap_incomplete"
          ? t("assistant.workPopup.cancelled")
          : detail,
      );
    },
    onMissing: () => {
      // The op never registered within the grace window — tear the popup down
      // silently rather than leaving a stale "running" shell.
      if (createCancelRequestedRef.current) return;
      setWorkPopupOpen(false);
      resetWorkPopupDisplayState();
      resetCreateCancelState();
      createRequestInFlightRef.current = false;
    },
  });

  // Import is a batch op (one server op covering the whole import); recover its
  // popup across refresh / close+reopen via a client-generated batch id.
  const { track: trackImportOp, clear: clearImportOp } = useOpRecovery("openclaw:import:op", {
    onRunning: (p) => {
      if (importCancelRequestedRef.current) return;
      setImportCancelState({ agentId: p.agentId, cancelling: false, armed: false });
      openWorkPopup();
      // Recovered mid-import: re-derive whether cancel is safe yet.
      void armImportCancelWhenSafe(p.agentId);
    },
    onSucceeded: (_p, result) => {
      if (importCancelRequestedRef.current) return;
      resetImportCancelState();
      finishWorkPopup((Number(result.failedCount) || 0) === 0);
      void loadImportCandidates();
      notifyOpenclawAgentsUpdated();
    },
    onFailed: (_p, detail) => {
      if (importCancelRequestedRef.current) return;
      resetImportCancelState();
      finishWorkPopup(detail === "cancelled", detail === "cancelled" ? undefined : detail);
      void loadImportCandidates();
      notifyOpenclawAgentsUpdated();
    },
  });

  function openStoreComingSoon() {
    setStoreComingSoonOpen(true);
  }

  useEffect(() => {
    void loadTeams();
  }, []);

  // NB: there is intentionally no separate on-mount "reconcile popup vs op
  // status" effect here. Recovery is owned solely by useOpRecovery above
  // (durable localStorage pointer → WS subscribe + graceful status poll). The
  // old reconcile effect duplicated that work and — fatally — closed the popup
  // on a transient `not_found` during the pre-registration window, which is what
  // made the popup "直接不见了" after navigating away and back. The session-backed
  // popup fields (workPopupOpen/Text/Success/Running + createCancelState) already
  // restore the visible terminal/running state without a reconcile pass.

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
    // A user-initiated create/import cancel owns the popup until its verify
    // completes — recovery / the resolving request must not unlock early.
    if (createCancelRequestedRef.current || importCancelRequestedRef.current) return;
    setWorkPopupRunning(false);
    setWorkPopupSuccess(success);
    setWorkPopupText(
      success
        ? detail || t("assistant.workPopup.done")
        : detail || t("assistant.workPopup.failed"),
    );
    resetCreateCancelState();
  }

  async function verifyCreateCancelConverged(agentId: string): Promise<void> {
    const verifyDeadline = Date.now() + CREATE_CANCEL_VERIFY_TIMEOUT_MS;
    let consecutiveAbsent = 0;
    while (true) {
      const [listed, op] = await Promise.all([
        api.listOpenclawAgents(),
        api.getOperationStatus(`openclaw_create:${agentId}`),
      ]);
      const absent = !listed.items.some((item) => item.id === agentId);
      if (absent) consecutiveAbsent += 1;
      else consecutiveAbsent = 0;
      if (isCreateCancelConverged(absent, op, consecutiveAbsent)) return;
      if (Date.now() >= verifyDeadline) {
        if (!absent) {
          throw new Error(t("assistant.workPopup.cancelAgentStillVisible"));
        }
        throw new Error(t("assistant.workPopup.cancelOpStillRunning"));
      }
      await new Promise((resolve) => window.setTimeout(resolve, CREATE_CANCEL_VERIFY_POLL_MS));
    }
  }

  function closeWorkPopupAfterCancel(): void {
    clearOp();
    notifyOpenclawAgentsUpdated();
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetCreateCancelState();
    createRequestInFlightRef.current = false;
  }

  function dismissCancelFailureNotice(agentId: string): void {
    setCreateCancelCleanupNotice(agentId);
    clearOp();
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetCreateCancelState();
    createRequestInFlightRef.current = false;
    createCancelRequestedRef.current = false;
  }

  function closeWorkPopupFromModal(): void {
    if (createCancelState?.failed && createCancelState.agentId) {
      dismissCancelFailureNotice(createCancelState.agentId);
      return;
    }
    if (importCancelState?.failed) {
      dismissImportCancelFailureNotice();
      return;
    }
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetCreateCancelState();
    resetImportCancelState();
  }

  // Keep the cancel button disabled until the backend reaches a state where a
  // cancel cannot leave residual data (op registered + bootstrap task tracked).
  // This is the whole fix for "取消创建 总是出错": arming late is what guarantees
  // the cancel/cleanup runs against a stable create instead of racing it.
  async function armCancelWhenSafe(agentId: string): Promise<void> {
    if (armPollInFlightRef.current) return;
    armPollInFlightRef.current = true;
    try {
      const armed = await waitForCancelArmed(`openclaw_create:${agentId}`, isOpenclawCancelArmed, {
        getStatus: (opId) => api.getOperationStatus(opId),
        shouldStop: () => createCancelRequestedRef.current,
      });
      if (!armed) return;
      setCreateCancelState((prev) =>
        prev && prev.agentId === agentId && !prev.cancelling && !prev.failed
          ? { ...prev, armed: true }
          : prev,
      );
    } finally {
      armPollInFlightRef.current = false;
    }
  }

  async function runCreateCancelFlow(
    agentId: string,
    options: { postCancel: boolean },
  ): Promise<void> {
    if (cancelVerifyInFlightRef.current) return;
    cancelVerifyInFlightRef.current = true;
    createCancelRequestedRef.current = true;
    setWorkPopupOpen(true);
    setWorkPopupRunning(true);
    setCreateCancelState({ agentId, cancelling: true });
    try {
      if (options.postCancel) {
        createRequestAbortRef.current?.abort();
        setWorkPopupText(t("assistant.workPopup.cancelRunning"));
        await api.cancelOpenclawAgentCreate(agentId);
      }
      setWorkPopupText(t("assistant.workPopup.cancelVerifying"));
      await verifyCreateCancelConverged(agentId);
      closeWorkPopupAfterCancel();
    } catch (e) {
      const err = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      createCancelRequestedRef.current = false;
      setWorkPopupRunning(false);
      setWorkPopupSuccess(false);
      setWorkPopupText(t("assistant.workPopup.cancelFailed", { message: err }));
      setCreateCancelState({ agentId, cancelling: false, failed: true });
    } finally {
      cancelVerifyInFlightRef.current = false;
    }
  }

  async function onCancelCreate() {
    if (createCancelState === null || createCancelState.cancelling) return;
    await runCreateCancelFlow(createCancelState.agentId, { postCancel: true });
  }

  useEffect(() => {
    const state = createCancelState;
    if (state === null || !state.cancelling) return;
    void runCreateCancelFlow(state.agentId, { postCancel: false });
    // Resume verify-only after refresh / remount while a cancel was in flight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // ── Import cancel (mirrors create cancel; keeps already-imported agents) ──

  function resetImportCancelState() {
    setImportCancelState(null);
    importCancelRequestedRef.current = false;
  }

  // Arm the import-cancel button once the batch op is registered (delayed
  // release, like create) so a cancel always hits a live, cancellable run.
  async function armImportCancelWhenSafe(batchId: string): Promise<void> {
    if (importArmPollInFlightRef.current) return;
    importArmPollInFlightRef.current = true;
    try {
      const armed = await waitForCancelArmed(
        `openclaw_import_batch:${batchId}`,
        isOpenclawCancelArmed,
        {
          getStatus: (opId) => api.getOperationStatus(opId),
          shouldStop: () => importCancelRequestedRef.current,
        },
      );
      if (!armed) return;
      setImportCancelState((prev) =>
        prev && prev.agentId === batchId && !prev.cancelling && !prev.failed
          ? { ...prev, armed: true }
          : prev,
      );
    } finally {
      importArmPollInFlightRef.current = false;
    }
  }

  // Import cancel converges as soon as the batch op goes terminal: the backend
  // marks it cancelled synchronously, and a run that finished first is "done".
  async function verifyImportCancelConverged(batchId: string): Promise<void> {
    const verifyDeadline = Date.now() + CREATE_CANCEL_VERIFY_TIMEOUT_MS;
    while (true) {
      const op = await api.getOperationStatus(`openclaw_import_batch:${batchId}`);
      if (op.state === "succeeded" || op.state === "not_found") return;
      if (op.state === "failed" && op.detail === "cancelled") return;
      if (Date.now() >= verifyDeadline) {
        throw new Error(t("assistant.workPopup.cancelOpStillRunning"));
      }
      await new Promise((resolve) => window.setTimeout(resolve, CREATE_CANCEL_VERIFY_POLL_MS));
    }
  }

  function closeImportPopupAfterCancel(): void {
    clearImportOp();
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetImportCancelState();
    void loadImportCandidates();
    notifyOpenclawAgentsUpdated();
  }

  function dismissImportCancelFailureNotice(): void {
    clearImportOp();
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetImportCancelState();
    importCancelRequestedRef.current = false;
  }

  async function runImportCancelFlow(batchId: string): Promise<void> {
    if (importVerifyInFlightRef.current) return;
    importVerifyInFlightRef.current = true;
    importCancelRequestedRef.current = true;
    setWorkPopupOpen(true);
    setWorkPopupRunning(true);
    setImportCancelState({ agentId: batchId, cancelling: true });
    try {
      setWorkPopupText(t("assistant.workPopup.cancelImportRunning"));
      await api.cancelOpenclawImport(batchId);
      setWorkPopupText(t("assistant.workPopup.cancelVerifying"));
      await verifyImportCancelConverged(batchId);
      closeImportPopupAfterCancel();
    } catch (e) {
      const err = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      importCancelRequestedRef.current = false;
      setWorkPopupRunning(false);
      setWorkPopupSuccess(false);
      setWorkPopupText(t("assistant.workPopup.cancelFailed", { message: err }));
      setImportCancelState({ agentId: batchId, cancelling: false, failed: true });
    } finally {
      importVerifyInFlightRef.current = false;
    }
  }

  async function onCancelImport() {
    if (importCancelState === null || importCancelState.cancelling) return;
    await runImportCancelFlow(importCancelState.agentId);
  }

  useEffect(() => {
    const state = importCancelState;
    if (state === null || !state.cancelling) return;
    void runImportCancelFlow(state.agentId);
    // Resume verify after refresh / remount while an import cancel was in flight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

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
    const batchId = crypto.randomUUID();
    importCancelRequestedRef.current = false;
    setImportCancelState({ agentId: batchId, cancelling: false, armed: false });
    trackImportOp({ opId: `openclaw_import_batch:${batchId}`, agentId: batchId });
    openWorkPopup();
    void armImportCancelWhenSafe(batchId);
    try {
      const out = await api.importOpenclawAgents({ agentIds: selectedImportIds, teamId, batchId });
      // A cancel in flight owns the popup (runImportCancelFlow) — don't clobber.
      if (importCancelRequestedRef.current) return;
      clearImportOp();
      resetImportCancelState();
      setLastImportResult(out);
      setSelectedImportIds([]);
      await loadImportCandidates();
      if (out.imported.length > 0) notifyOpenclawAgentsUpdated();
      finishWorkPopup(!out.cancelled && out.failed.length === 0);
    } catch (e) {
      if (importCancelRequestedRef.current) return;
      clearImportOp();
      resetImportCancelState();
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
    const batchId = crypto.randomUUID();
    importCancelRequestedRef.current = false;
    setImportCancelState({ agentId: batchId, cancelling: false, armed: false });
    trackImportOp({ opId: `openclaw_import_batch:${batchId}`, agentId: batchId });
    openWorkPopup();
    void armImportCancelWhenSafe(batchId);
    try {
      const out = await api.importOpenclawAgents({ importAll: true, teamId, batchId });
      if (importCancelRequestedRef.current) return;
      clearImportOp();
      resetImportCancelState();
      setLastImportResult(out);
      setSelectedImportIds([]);
      await loadImportCandidates();
      if (out.imported.length > 0) notifyOpenclawAgentsUpdated();
      finishWorkPopup(!out.cancelled && out.failed.length === 0);
    } catch (e) {
      if (importCancelRequestedRef.current) return;
      clearImportOp();
      resetImportCancelState();
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
    if (createRequestInFlightRef.current || createCancelState?.cancelling) return;
    if (/\s/.test(agentId)) {
      setCreateError(t("assistant.createModal.invalidAgentIdNoSpaces"));
      return;
    }
    if (/[\u3400-\u9FFF]/.test(agentId)) {
      setCreateError(t("assistant.createModal.invalidAgentIdNoChinese"));
      return;
    }
    // Reject a duplicate id on the spot \u2014 keep the modal open and tell the user
    // why, instead of closing it, firing the long-running create, and only
    // surfacing the collision after the backend round-trip. (The backend's
    // AGENT_ALREADY_EXISTS check below stays as the authoritative guard against
    // a race.)
    try {
      const existing = await api.listOpenclawAgents();
      if (existing.items.some((a) => a.id === agentId)) {
        setCreateError(t("assistant.createModal.idDuplicate", { id: agentId }));
        return;
      }
      const opStatus = await api.getOperationStatus(`openclaw_create:${agentId}`);
      if (opStatus.state === "running" || opStatus.inFlight) {
        setCreateError(t("assistant.createModal.idInProgress", { id: agentId }));
        return;
      }
    } catch {
      /* list / op status unavailable — fall through; backend still rejects duplicates */
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
    // Single-flight from here on (no await before this point sets it): a second
    // click can't fire a duplicate create. Reset in the finally below.
    if (createRequestInFlightRef.current) return;
    createRequestInFlightRef.current = true;
    setCreateModalOpen(false);
    setCreateError(null);
    const abortController = new AbortController();
    createRequestAbortRef.current = abortController;
    createCancelRequestedRef.current = false;
    setCreateCancelState({
      agentId,
      cancelling: false,
      armed: false,
    });
    trackOp({ opId: `openclaw_create:${agentId}`, agentId });
    openWorkPopup();
    void armCancelWhenSafe(agentId);
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
      if (createCancelRequestedRef.current) return;
      clearOp();
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
      createRequestInFlightRef.current = false;
    } catch (e) {
      if (createCancelRequestedRef.current || isAbortError(e)) return;
      // A user-input rejection (id already taken / invalid) belongs back IN the
      // form, not buried in the progress popup — reopen it with the message.
      if (
        e instanceof ApiError &&
        (e.code === "AGENT_ALREADY_EXISTS" ||
          e.code === "AGENT_EXISTS" ||
          e.code === "INVALID_PAYLOAD")
      ) {
        clearOp();
        setWorkPopupOpen(false);
        resetWorkPopupDisplayState();
        resetCreateCancelState();
        const duplicate =
          e.code === "AGENT_ALREADY_EXISTS" ||
          e.code === "AGENT_EXISTS" ||
          /already in progress/i.test(e.message);
        setCreateError(
          duplicate
            ? t("assistant.createModal.idInProgress", { id: agentId })
            : `${e.code}: ${e.message}`,
        );
        setCreateModalOpen(true);
        return;
      }
      clearOp();
      const err = e instanceof Error ? e.message : String(e);
      finishWorkPopup(false, err);
    } finally {
      if (!createCancelRequestedRef.current) {
        createRequestInFlightRef.current = false;
      }
      if (createRequestAbortRef.current === abortController) {
        createRequestAbortRef.current = null;
      }
    }
  }

  async function openRemoveForAgent(agent: OpenclawAgentSummary) {
    setRemoveModalOpen(true);
    setRemoveError(null);
    setRemoveMode("unregister");
    setRemoveTargetId(agent.id);
    setRemoveTargets([agent]);
    setRemoveLoading(false);
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
    if (!(await confirm(confirmText))) return;
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
  // The work popup is shared by create and import (mutually exclusive). Pick
  // whichever cancellable op is active and drive the cancel button from it.
  const popupCancelKind: "create" | "import" | null = createCancelState
    ? "create"
    : importCancelState
      ? "import"
      : null;
  const popupCancelState = createCancelState ?? importCancelState;
  const workPopupBusy =
    popupCancelState?.cancelling === true ||
    (workPopupRunning && !popupCancelState?.failed);
  const showCreateCancelAction = popupCancelState !== null;
  // Only allow cancel once it is armed (safe) — or to retry a failed cancel.
  // While unarmed the button is shown but disabled ("正在创建/导入…") so the user
  // sees the op is still running rather than racing a half-built operation.
  const createCancelEnabled =
    showCreateCancelAction &&
    !(popupCancelState?.cancelling ?? false) &&
    ((popupCancelState?.armed ?? false) || (popupCancelState?.failed ?? false));
  const onPopupCancelClick = () => {
    if (popupCancelKind === "import") void onCancelImport();
    else void onCancelCreate();
  };
  const onPopupForceClose = () => {
    if (popupCancelKind === "import") dismissImportCancelFailureNotice();
    else if (createCancelState?.agentId) dismissCancelFailureNotice(createCancelState.agentId);
  };
  const popupCancelLabel = popupCancelState?.cancelling
    ? t("assistant.workPopup.cancellingCreate")
    : popupCancelState?.failed
      ? t("assistant.workPopup.cancelRetry")
      : popupCancelState?.armed
        ? popupCancelKind === "import"
          ? t("assistant.workPopup.cancelImport")
          : t("assistant.workPopup.cancelCreate")
        : popupCancelKind === "import"
          ? t("assistant.workPopup.cancelImportPreparing")
          : t("assistant.workPopup.cancelPreparing");
  const workPopupDisplayText =
    workPopupText ||
    (workPopupOpen && workPopupRunning ? t("assistant.workPopup.running") : "");
  const pickerActions: OpenclawPickerActions = {
    openCreate: onOpenCreateModal,
    openRestore: () => {
      void openRestoreModal();
    },
    openImport: () => {
      void openImportModal();
    },
    openRemoveFor: (agent) => {
      void openRemoveForAgent(agent);
    },
  };

  return (
    <>
      {createCancelCleanupNotice && (
        <div className="mb-4 flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
          <p className="flex-1">
            {t("assistant.workPopup.cancelCleanupHint", { id: createCancelCleanupNotice })}
          </p>
          <button
            type="button"
            className="shrink-0 text-amber-700 underline hover:text-amber-900 dark:text-amber-200"
            onClick={() => setCreateCancelCleanupNotice(null)}
          >
            {t("assistant.workPopup.cancelCleanupDismiss")}
          </button>
        </div>
      )}
      {children?.(pickerActions)}

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
            <label className="label">{t("assistant.createModal.agentNameLabel")}</label>
            <input
              className="input"
              value={createAgentName}
              onChange={(e) => setCreateAgentName(e.target.value)}
              placeholder={t("assistant.createModal.agentNamePlaceholder")}
            />
          </div>
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
        onClose={() => {
          // Once removal is in flight it cannot be cancelled — keep the modal up.
          if (removeSubmitting) return;
          setRemoveModalOpen(false);
        }}
        title={t("assistant.removeModal.title")}
        width="max-w-lg"
        dismissible={!removeSubmitting}
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
                  disabled={removeSubmitting}
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
        onClose={() => {
          // Once restore is in flight it cannot be cancelled — keep the modal up.
          if (restoreSubmitting) return;
          setRestoreModalOpen(false);
        }}
        title={t("assistant.restoreModal.title")}
        width="max-w-lg"
        dismissible={!restoreSubmitting}
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
                  disabled={restoreSubmitting}
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
          closeWorkPopupFromModal();
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
          {createCancelState?.failed && (
            <p className="text-sm text-amber-800 dark:text-amber-200">
              {t("assistant.workPopup.cancelCleanupHint", { id: createCancelState.agentId })}
            </p>
          )}
          {workPopupBusy && <Loading />}
          {showCreateCancelAction && (
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn-outline"
                onClick={onPopupCancelClick}
                disabled={!createCancelEnabled}
              >
                {popupCancelLabel}
              </button>
              {popupCancelState?.failed && (
                <button
                  type="button"
                  className="btn-primary"
                  onClick={onPopupForceClose}
                >
                  {t("assistant.workPopup.cancelForceClose")}
                </button>
              )}
            </div>
          )}
          {!workPopupBusy && !popupCancelState?.failed && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-primary"
                onClick={closeWorkPopupFromModal}
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


function agentCardShowsIdLine(agent: { id: string; name: string }): boolean {
  const name = agent.name.trim();
  return name.length > 0 && name !== agent.id;
}

function agentCardTitle(agent: { id: string; name: string }): string {
  const name = agent.name.trim();
  return agentCardShowsIdLine(agent) ? name : agent.id;
}

function ChatPicker({ actions }: { actions: OpenclawPickerActions }) {
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
      <AgentManagementHeader
        title={t("chat.title")}
        description={t("chat.pageNote")}
        leading={
          <AgentViewModeToggle
            viewMode={viewMode}
            onChange={setViewMode}
            cardLabel={t("chat.viewCard")}
            listLabel={t("chat.viewList")}
          />
        }
        actions={
          <>
            <button type="button" className="btn-outline h-9 px-3 text-sm" onClick={actions.openRestore}>
              {t("assistant.askRestore")}
            </button>
            <button type="button" className="btn-outline h-9 px-3 text-sm" onClick={actions.openImport}>
              {t("assistant.askImport")}
            </button>
            <button
              type="button"
              className="btn-primary inline-flex h-9 items-center gap-1.5 px-4"
              onClick={actions.openCreate}
            >
              <PlusIcon className="h-4 w-4" />
              {t("assistant.askCreate")}
            </button>
          </>
        }
      />
      {error && <ErrorBox>{error}</ErrorBox>}
      {!items && !error && <Loading />}
      {items && items.length === 0 && (
        <EmptyState
          icon={<AgentCardAvatar size="empty" className="mb-0" platform="openclaw" />}
          action={
            <div className="flex flex-col items-center gap-2">
              <div className="flex items-center gap-2">
                <SilentLink to="/chat?createAgent=1" className="btn-primary">
                  {t("chat.pickerEmptyActionCreate")}
                </SilentLink>
                <button
                  type="button"
                  className="btn-outline"
                  onClick={() => setStoreComingSoonOpen(true)}
                >
                  {t("chat.pickerEmptyActionLoadStore")}
                </button>
              </div>
              <SilentLink to="/chat?importAgent=1" className="btn-outline">
                {t("assistant.askImport")}
              </SilentLink>
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
                  <SilentLink
                    key={a.id}
                    as="div"
                    to={`/chat/${a.id}`}
                    className="group card block p-5 transition-all hover:border-brand-300 hover:shadow-[0_0_24px_-6px_rgb(var(--brand-300))]"
                  >
                    <div className="flex items-start justify-between">
                      <AgentCardAvatar platform="openclaw" />
                      <button
                        type="button"
                        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-rose-500 hover:bg-rose-50 hover:text-rose-700"
                        title={t("assistant.askRemove")}
                        aria-label={t("assistant.askRemove")}
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          actions.openRemoveFor(a);
                        }}
                      >
                        <TrashIcon className="h-5 w-5" />
                      </button>
                    </div>
                    <div className="font-semibold text-ink-900">{agentCardTitle(a)}</div>
                    {agentCardShowsIdLine(a) && (
                      <div className="mt-0.5 font-mono text-xs text-ink-500">{a.id}</div>
                    )}
                    {a.description && (
                      <div className="text-xs text-ink-500 mt-2 line-clamp-3">
                        {a.description}
                      </div>
                    )}
                  </SilentLink>
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
                    <div className="inline-flex items-center justify-end gap-2">
                      <button
                        type="button"
                        className="inline-flex h-8 w-8 items-center justify-center rounded-md text-rose-500 hover:bg-rose-50 hover:text-rose-700"
                        title={t("assistant.askRemove")}
                        aria-label={t("assistant.askRemove")}
                        onClick={() => actions.openRemoveFor(a)}
                      >
                        <TrashIcon className="h-5 w-5" />
                      </button>
                      <SilentLink className="btn-primary" to={`/chat/${a.id}`}>
                        {t("agents.chatLink")}
                      </SilentLink>
                    </div>
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
      <div className="relative overflow-hidden rounded-2xl border border-brand-100 bg-gradient-to-br from-indigo-50 via-surface to-fuchsia-50 px-6 py-7">
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
  const { alert } = useDialog();
  const [agent, setAgent] = useState<OpenclawAgentDetail | null>(null);
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [messages, setMessages] = useState<Message[]>([]);
  // Persist the unsent composer text so switching tabs doesn't discard it.
  const [input, setInput] = useSessionBackedState(
    `openclaw-chat:room:${agentId}:input`,
    "",
    { isClosed: (v) => v.trim() === "" },
  );
  const inputRef = useAutoGrowTextarea(input, { minHeightPx: 80, maxHeightPx: 240 });
  const [streaming, setStreaming] = useState(false);
  // True while polling for a reply whose stream was detached by a tab switch
  // (the answer is still landing in server history). Drives a pending bubble
  // without persisting an empty placeholder to the cache.
  const [recovering, setRecovering] = useState(false);
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
  const {
    ref: scrollRef,
    atBottom,
    scrollToBottom,
    handleScroll,
    stickIfAtBottom,
    suppressNextStickyScroll,
  } = useStickyScroll<HTMLDivElement>();
  // AbortController for the in-flight SSE fetch, so "Stop" can cut the stream.
  const abortRef = useRef<AbortController | null>(null);
  // Index (into the displayed message list) before which a "new messages"
  // divider is drawn on re-entry; -1 = none. Computed once at load.
  const [newDividerAt, setNewDividerAt] = useState(-1);
  // Divider anchor used to jump to the first unseen message block when the user
  // returns to this chat and new messages arrived while away.
  const newDividerRef = useRef<HTMLDivElement | null>(null);
  const didJumpToNewDividerRef = useRef(false);
  /** Divider index for the in-flight turn (armed at send/regenerate). */
  const turnDividerAtRef = useRef(-1);
  // Latest transcript, so the unmount cleanup can persist how many messages the
  // user had seen when they navigated away.
  const messagesRef = useRef<Message[]>([]);
  messagesRef.current = messages;
  const displayMessages = useMemo(() => {
    const raw =
      recovering &&
      messages.length > 0 &&
      messages[messages.length - 1].role === "user"
        ? [...messages, { role: "assistant" as const, content: "" }]
        : messages;
    return displayChatMessages(raw);
  }, [messages, recovering]);
  const [pendingFiles, setPendingFiles] = useState<File[]>([]);
  const pendingFilesRef = useRef<File[]>([]);
  pendingFilesRef.current = pendingFiles;
  const [draggingFiles, setDraggingFiles] = useState(false);
  const [uploadingAttachments, setUploadingAttachments] = useState(false);
  const fileInputRef = useRef<HTMLInputElement | null>(null);

  const addPendingFiles = useCallback((incoming: File[]) => {
    if (incoming.length === 0) return;
    setActionError(null);
    const next = [...pendingFilesRef.current];
    let errorText: string | null = null;
    for (const file of incoming) {
      const duplicate = next.some(
        (existing) =>
          existing.name === file.name &&
          existing.size === file.size &&
          existing.lastModified === file.lastModified,
      );
      if (duplicate) continue;
      if (file.size <= 0) {
        errorText ??= t("chat.attachments.emptyFile", { name: file.name });
        continue;
      }
      if (file.size > CHAT_MAX_ATTACHMENT_BYTES) {
        errorText ??= t("chat.attachments.tooLarge", {
          name: file.name,
          maxMb: Math.round(CHAT_MAX_ATTACHMENT_BYTES / (1024 * 1024)),
        });
        continue;
      }
      if (next.length >= CHAT_MAX_ATTACHMENTS) {
        errorText ??= t("chat.attachments.tooMany", { max: CHAT_MAX_ATTACHMENTS });
        break;
      }
      next.push(file);
    }
    setPendingFiles(next);
    if (errorText) setActionError(errorText);
  }, [t]);

  const insertTextAtCursor = useCallback((text: string) => {
    if (!text) return;
    const textarea = inputRef.current;
    if (!textarea) {
      setInput((prev) => `${prev}${text}`);
      return;
    }
    const start = textarea.selectionStart ?? textarea.value.length;
    const end = textarea.selectionEnd ?? start;
    const next = `${textarea.value.slice(0, start)}${text}${textarea.value.slice(end)}`;
    setInput(next);
    window.requestAnimationFrame(() => {
      const node = inputRef.current;
      if (!node) return;
      const caret = start + text.length;
      node.focus();
      node.setSelectionRange(caret, caret);
    });
  }, [inputRef, setInput]);

  const appendFolderPathToInput = useCallback((folderPath: string) => {
    const text = folderPath.trim();
    if (!text) return;
    insertTextAtCursor(text);
  }, [insertTextAtCursor]);

  const onDropAttachmentArea = useCallback((event: DragEvent<HTMLDivElement>) => {
    event.preventDefault();
    setDraggingFiles(false);
    if (streaming || resetting || uploadingAttachments) return;
    void (async () => {
      const folder = resolveDroppedFolderPath(event.dataTransfer);
      if (folder?.hasFolder) {
        const blocked = await getNativeDirectoryBlockedMessage(t, "pick");
        if (blocked) {
          setActionError(blocked);
          return;
        }
        if (folder.absolutePath) {
          appendFolderPathToInput(folder.absolutePath);
          setActionError(null);
        } else {
          setActionError(t("chat.attachments.folderAbsolutePathUnavailable"));
        }
        return;
      }
      addPendingFiles(Array.from(event.dataTransfer.files ?? []));
    })();
  }, [
    addPendingFiles,
    appendFolderPathToInput,
    resetting,
    streaming,
    t,
    uploadingAttachments,
  ]);

  const onSelectAttachmentFiles = useCallback((event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files ?? []);
    addPendingFiles(files);
    event.target.value = "";
  }, [addPendingFiles]);

  const removePendingFile = useCallback((idx: number) => {
    setPendingFiles((prev) => prev.filter((_, i) => i !== idx));
  }, []);

  const uploadPendingFiles = useCallback(async () => {
    if (pendingFilesRef.current.length === 0) return [] as ChatAttachmentMeta[];
    setUploadingAttachments(true);
    try {
      const out: ChatAttachmentMeta[] = [];
      for (const file of pendingFilesRef.current) {
        const uploaded = await api.uploadOpenclawChatAttachment(agentId, file);
        out.push(uploaded.attachment);
      }
      return out;
    } finally {
      setUploadingAttachments(false);
    }
  }, [agentId]);

  useEffect(() => {
    didJumpToNewDividerRef.current = false;
  }, [agentId]);

  useEffect(() => {
    const cached = OPENCLAW_PENDING_FILES_CACHE.get(agentId);
    setPendingFiles(cached ? [...cached] : []);
    setDraggingFiles(false);
  }, [agentId]);

  useEffect(() => {
    if (pendingFiles.length > 0) {
      OPENCLAW_PENDING_FILES_CACHE.set(agentId, pendingFiles);
    } else {
      OPENCLAW_PENDING_FILES_CACHE.delete(agentId);
    }
  }, [agentId, pendingFiles]);

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
  // agents (incl. on initial mount / page refresh), then reconcile against
  // server history. A tab switch can leave the cache holding a stale empty
  // assistant bubble (the false "no reply") while the real answer sits on the
  // server; the server is authoritative. Only the explicit "Reset" button (or
  // its localStorage entry being cleared) wipes the transcript.
  useEffect(() => {
    let cancelled = false;
    // How many messages the user had already seen before this visit — read once,
    // up front, so the persist-on-change effect can't clobber it first.
    const seenAtEntry = loadLastSeenCount(agentId);
    const cached = loadChatHistory(agentId);
    setMessages(cached);
    setActionError(null);
    void (async () => {
      try {
        const hist = await api.getOpenclawAgentChatHistory(agentId);
        if (cancelled) return;
        const server: Message[] = hist.messages.map((m) => ({
          role: m.role,
          content:
            m.role === "assistant"
              ? normalizeAssistantContent(m.content)
              : m.content,
          attachments: m.attachments ?? [],
        }));
        // Server history has no timestamps; backfill from the local cache (which
        // persists ts) by matching position + content so times survive a refresh.
        const merged = reconcileTranscript(cached, server).map((m, i) => {
          const c = cached[i];
          return c && c.role === m.role && c.content === m.content && c.ts
            ? { ...m, ts: c.ts }
            : m;
        });
        setMessages(merged);
        if (merged.length > 0) saveChatHistory(agentId, merged);
        // Draw the "new messages" divider above anything that arrived while the
        // user was away (settled count grew beyond what they'd last seen).
        const shown = settledCount(merged);
        const dividerAt = seenAtEntry > 0 && shown > seenAtEntry ? seenAtEntry : -1;
        setNewDividerAt(dividerAt);
        if (dividerAt >= 0) {
          didJumpToNewDividerRef.current = false;
          suppressNextStickyScroll();
        }
        // Reconnect: server turn registry is authoritative for in-flight work.
        // History alone can miss a running turn (e.g. cache holds a partial
        // assistant bubble while the server still says running).
        try {
          const st = await api.getOpenclawChatStatus(agentId);
          if (cancelled) return;
          let nextMessages = merged;
          if (st.status === "running") {
            setRecovering(true);
            const last = nextMessages[nextMessages.length - 1];
            if (!last || last.role === "user") {
              nextMessages = [
                ...nextMessages,
                { role: "assistant" as const, content: "", ts: Date.now() },
              ];
            }
          } else {
            const last = nextMessages[nextMessages.length - 1];
            const needsFinal =
              st.status === "done" &&
              st.final.trim() &&
              (!last || last.role !== "assistant" || !last.content.trim());
            if (needsFinal) {
              const text = normalizeAssistantContent(st.final);
              nextMessages =
                last && last.role === "assistant"
                  ? nextMessages.map((m, i) =>
                      i === nextMessages.length - 1 ? { ...m, content: text, ts: Date.now() } : m,
                    )
                  : [...nextMessages, { role: "assistant" as const, content: text, ts: Date.now() }];
            } else if (last && last.role === "user") {
              setRecovering(true);
              nextMessages = [
                ...nextMessages,
                { role: "assistant" as const, content: "", ts: Date.now() },
              ];
            }
          }
          setMessages(nextMessages);
          if (nextMessages.length > 0) saveChatHistory(agentId, nextMessages);
        } catch {
          const last = merged[merged.length - 1];
          if (last && last.role === "user") {
            setRecovering(true);
            const nextMessages = [
              ...merged,
              { role: "assistant" as const, content: "", ts: Date.now() },
            ];
            setMessages(nextMessages);
            saveChatHistory(agentId, nextMessages);
          }
        }
      } catch {
        /* keep the cached view if history fetch fails */
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [agentId]);

  const revealCompletedTurn = useCallback(() => {
    const at = turnDividerAtRef.current;
    if (at < 0) return;
    suppressNextStickyScroll();
    setNewDividerAt(at);
    didJumpToNewDividerRef.current = false;
  }, [suppressNextStickyScroll]);

  // Reconnect to a turn whose SSE stream was detached (tab switch / refresh):
  // poll GET /chat/status and keep going *as long as the server says running*
  // (no fixed cap — a turn can run many minutes), surfacing the live step trail.
  // On done/error/idle, adopt the final answer (from history, which the backend
  // persists even after a disconnect) and stop.
  useEffect(() => {
    if (!recovering) return;
    let cancelled = false;
    let timer: number | undefined;
    const adoptFinalFromHistory = async () => {
      try {
        const hist = await api.getOpenclawAgentChatHistory(agentId);
        if (cancelled) return;
        const server: Message[] = hist.messages.map((m) => ({
          role: m.role,
          content:
            m.role === "assistant"
              ? normalizeAssistantContent(m.content)
              : m.content,
          attachments: m.attachments ?? [],
        }));
        const last = server[server.length - 1];
        if (last && last.role === "assistant" && last.content.trim() !== "") {
          setMessages(server);
          saveChatHistory(agentId, server);
          revealCompletedTurn();
        }
      } catch {
        /* leave the transcript as-is */
      }
    };
    const adoptFinal = (text: string) => {
      const normalized = normalizeAssistantContent(text);
      setMessages((prev) => {
        const next = [...prev];
        const ts = Date.now();
        if (next.length > 0 && next[next.length - 1].role === "assistant") {
          next[next.length - 1] = { role: "assistant", content: normalized, ts };
        } else {
          next.push({ role: "assistant", content: normalized, ts });
        }
        saveChatHistory(agentId, next);
        return next;
      });
      revealCompletedTurn();
    };
    const tick = async () => {
      try {
        const st = await api.getOpenclawChatStatus(agentId);
        if (cancelled) return;
        if (st.status === "running") {
          // Keep recovering — PendingReply shows in the assistant bubble.
        } else {
          if (st.status === "done" && st.final.trim()) {
            adoptFinal(st.final);
          } else {
            await adoptFinalFromHistory();
          }
          if (st.status === "error" && st.error && st.error !== "cancelled") {
            setActionError(st.error);
          }
          setRecovering(false);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (cancelled) return;
      timer = window.setTimeout(() => void tick(), 2000);
    };
    timer = window.setTimeout(() => void tick(), 1200);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [recovering, agentId, revealCompletedTurn]);

  // Persist on every transcript change. ``saveChatHistory`` keeps only
  // the trailing ``HISTORY_LIMIT`` entries so the storage stays bounded.
  useEffect(() => {
    if (messages.length === 0) return;
    saveChatHistory(agentId, messages);
  }, [agentId, messages]);

  // On leaving the page (or switching agents), remember how many messages the
  // user had seen so the next visit can mark what's new.
  useEffect(() => {
    return () => saveLastSeenCount(agentId, settledCount(messagesRef.current));
  }, [agentId]);

  // Auto-scroll on new content and once the chat panel is actually mounted.
  // When we restore history before agent detail finishes loading, the first
  // ``messages`` update can happen while the scroll container is still absent.
  // Including ``agent`` ensures we scroll to latest as soon as the panel appears.
  // Skip while a divider jump is pending — ``revealCompletedTurn`` / re-entry
  // will scroll to the divider instead of pinning the latest tail.
  useEffect(() => {
    if (newDividerAt >= 0 && !didJumpToNewDividerRef.current) return;
    stickIfAtBottom();
  }, [messages, agent, stickIfAtBottom, newDividerAt]);

  // On re-entry (or when a turn completes in-page), scroll to the "new messages"
  // divider instead of pinning the latest tail.
  useEffect(() => {
    if (newDividerAt < 0 || didJumpToNewDividerRef.current) return;
    const divider = newDividerRef.current;
    const container = scrollRef.current;
    if (!divider || !container) return;
    const raf = window.requestAnimationFrame(() => {
      scrollToNewMessagesDivider(container, divider);
      handleScroll();
      didJumpToNewDividerRef.current = true;
    });
    return () => window.cancelAnimationFrame(raf);
  }, [newDividerAt, messages.length, recovering, streaming, agent, handleScroll, scrollRef]);

  // Core streaming turn. ``appendUser`` is false on regenerate (the user message
  // is already in the transcript — we only replace the assistant reply).
  async function runTurn(
    text: string,
    opts: { appendUser: boolean; attachments?: ChatAttachmentMeta[] },
  ) {
    setActionError(null);
    setRecovering(false);
    setNewDividerAt(-1);
    didJumpToNewDividerRef.current = true;
    turnDividerAtRef.current = turnDividerIndex(messagesRef.current, opts.appendUser);
    setStreaming(true);
    const turnAttachments = opts.attachments ?? [];
    setMessages((m) => {
      const base = opts.appendUser
        ? [
            ...m,
            {
              role: "user" as const,
              content: text,
              attachments: turnAttachments,
              ts: Date.now(),
            },
          ]
        : m;
      // Add an empty assistant message to stream into (timestamped once it lands).
      return [...base, { role: "assistant" as const, content: "" }];
    });
    scrollToBottom(); // the user just acted — jump to the latest
    const controller = new AbortController();
    abortRef.current = controller;
    let aborted = false;
    try {
      const resp = await api.chatWithOpenclawAgent(
        agentId,
        {
          // Session context is maintained by backend session_key.
          // Send only this turn's message to avoid client-side context replay.
          messages: [{ role: "user", content: text }],
          attachments: turnAttachments,
          stream: true,
        },
        { signal: controller.signal },
      );
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
            if (chunk.error) {
              throw new Error(String(chunk.error));
            }
            if (delta) {
              setMessages((m) => {
                const out = m.slice();
                out[out.length - 1] = {
                  role: "assistant",
                  content: out[out.length - 1].content + delta,
                  ts: Date.now(),
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
      if (controller.signal.aborted) {
        aborted = true;
      } else {
        setMessages((m) => {
          const out = m.slice();
          out[out.length - 1] = {
            role: "assistant",
            content: `(error) ${e instanceof Error ? e.message : String(e)}`,
            ts: Date.now(),
          };
          return out;
        });
      }
    } finally {
      abortRef.current = null;
      setStreaming(false);
      if (aborted) {
        // Mark the (still-empty) pending reply as stopped; keep any partial text.
        setMessages((m) => {
          const out = m.slice();
          const last = out[out.length - 1];
          if (last && last.role === "assistant" && !last.content) {
            out[out.length - 1] = {
              role: "assistant",
              content: t("chat.stopped"),
              ts: Date.now(),
            };
          }
          return out;
        });
      } else {
        // SSE may end without emitting the final delta (NO_TEXT_REPLY / proxy
        // buffering). Pull authoritative history once the turn finishes.
        try {
          const hist = await api.getOpenclawAgentChatHistory(agentId);
          const server: Message[] = hist.messages.map((m) => ({
            role: m.role,
            content:
              m.role === "assistant"
                ? normalizeAssistantContent(m.content)
                : m.content,
            attachments: m.attachments ?? [],
          }));
          const last = server[server.length - 1];
          if (last && last.role === "assistant") {
            setMessages((prev) => {
              const merged = reconcileTranscript(prev, server);
              saveChatHistory(agentId, merged);
              return merged;
            });
          }
          revealCompletedTurn();
        } catch {
          /* keep streamed partial */
          revealCompletedTurn();
        }
      }
    }
  }

  async function onSend(e: FormEvent) {
    e.preventDefault();
    if (streaming || recovering || uploadingAttachments) return;
    const text = input.trim();
    if (!text && pendingFiles.length === 0) return;
    let uploadedAttachments: ChatAttachmentMeta[] = [];
    if (pendingFiles.length > 0) {
      try {
        uploadedAttachments = await uploadPendingFiles();
      } catch (e) {
        const message = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
        setActionError(t("chat.attachments.uploadFailed", { message }));
        return;
      }
    }
    setInput("");
    setPendingFiles([]);
    await runTurn(text, { appendUser: true, attachments: uploadedAttachments });
  }

  async function stopTurn() {
    abortRef.current?.abort();
    try {
      await api.stopOpenclawAgentChat(agentId);
    } catch {
      /* best-effort — the client stream is already cut */
    }
    setRecovering(false);
  }

  async function regenerate() {
    if (streaming || recovering || resetting || uploadingAttachments) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    // Drop the trailing assistant reply; runTurn appends a fresh one.
    setMessages((m) => {
      const out = m.slice();
      if (out.length && out[out.length - 1].role === "assistant") out.pop();
      return out;
    });
    await runTurn(lastUser.content, {
      appendUser: false,
      attachments: lastUser.attachments ?? [],
    });
  }

  async function onResetConversation() {
    if (streaming || resetting || uploadingAttachments) return;
    setActionError(null);
    setResetting(true);
    try {
      await api.resetOpenclawAgentChat(agentId);
      setRecovering(false);
      setMessages([]);
      setNewDividerAt(-1);
      setInput("");
      setPendingFiles([]);
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
    if (await alertIfNativeDirectoryBlocked(t, "open")) return;
    const targetPath = buildAgentMyDesktopPath(agent.workspacePath);
    setActionError(null);
    setOpeningMyDesktop(true);
    try {
      await api.openDirectory({ path: targetPath });
    } catch (e) {
      const message = e instanceof ApiError ? `${e.code}: ${e.message}` : String(e);
      if (typeof window !== "undefined") {
        void alert(t("chat.myDesktop.openFailed", { message }));
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
            <AgentCardAvatar size="header" platform="openclaw" />
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
              {t("chat.myDesktop.action")}
            </button>
          </h1>
          <div className="mt-2 inline-flex items-center gap-2 text-xs text-ink-600">
            <span>{t("chat.teamLabel")}:</span>
            <span className="rounded-full border border-ink-200 bg-ink-50 px-2 py-0.5">
              {agent.teamName || t("chat.ungroupedTeam")}
            </span>
            <button
              type="button"
              className="btn-primary !px-2.5 !py-1 text-[11px] shadow-[0_0_12px_-6px_rgb(var(--brand-400))]"
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
          <button
            type="button"
            onClick={() => {
              if (isRemoteBrowser()) {
                void alert(t("chat.toOpenclawRemoteUnavailable"));
                return;
              }
              if (!openclawUrl) {
                void alert(t("chat.toOpenclawGatewayUnavailable"));
                return;
              }
              window.open(openclawUrl, "_blank", "noopener,noreferrer");
            }}
            className="inline-flex h-10 items-center justify-center rounded-full
                     bg-gradient-to-r from-brand-500 via-brand-400 to-orange-500
                     px-5 py-0 text-sm font-semibold tracking-wide text-white
                     shadow-[0_0_24px_-4px_rgb(var(--brand-400))]
                     ring-1 ring-brand-300/60
                     hover:from-brand-600 hover:to-orange-600
                     hover:shadow-[0_0_32px_-2px_rgb(var(--brand-400))]
                     hover:-translate-y-0.5
                     transition-all"
          >
            {t("chat.toOpenclaw")}
          </button>
        </div>
      </div>

      {actionError && <ErrorBox>{t("chat.error", { message: actionError })}</ErrorBox>}

      <Card className="flex min-h-0 flex-1 flex-col p-0 overflow-hidden">
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="min-h-[280px] flex-1 overflow-auto px-5 py-4 space-y-4 bg-ink-50/40"
          >
            {displayMessages.map((m, i, list) => (
              <Fragment key={i}>
                {i === newDividerAt && (
                  <div ref={newDividerRef}>
                    <NewMessagesDivider label={t("chat.newMessages")} />
                  </div>
                )}
                <Bubble
                  msg={m}
                  pending={
                    (streaming || recovering) &&
                    i === list.length - 1 &&
                    m.role === "assistant" &&
                    !m.content
                  }
                  noTextReply={t("chat.noTextReply")}
                />
              </Fragment>
            ))}
            {!streaming &&
              !recovering &&
              messages.length > 0 &&
              messages[messages.length - 1].role === "assistant" &&
              !!messages[messages.length - 1].content && (
                <div className="flex justify-start">
                  <button
                    type="button"
                    onClick={() => void regenerate()}
                    disabled={resetting}
                    className="inline-flex items-center gap-1 rounded-full border border-ink-200 bg-surface px-3 py-1 text-xs text-ink-500 hover:bg-ink-50 hover:text-ink-700 disabled:opacity-50"
                  >
                    <RefreshIcon className="h-3.5 w-3.5" />
                    {t("chat.regenerate")}
                  </button>
                </div>
              )}
          </div>
          {!atBottom && (
            <button
              type="button"
              onClick={scrollToBottom}
              className="absolute bottom-3 left-1/2 -translate-x-1/2 inline-flex items-center gap-1 rounded-full border border-ink-200 bg-surface/95 px-3 py-1 text-xs text-ink-600 shadow-card backdrop-blur hover:bg-ink-50"
            >
              {t("chat.scrollToBottom")}
              <span aria-hidden>↓</span>
            </button>
          )}
        </div>
        <form
          onSubmit={onSend}
          className="space-y-2 border-t border-ink-100 p-3"
        >
          <div
            className={cn(
              "space-y-2 rounded-lg border border-dashed px-3 py-2 transition",
              draggingFiles ? "border-brand-400 bg-brand-50/40" : "border-ink-200 bg-ink-50/40",
            )}
            onDragOver={(event: DragEvent<HTMLDivElement>) => {
              event.preventDefault();
              if (!draggingFiles) setDraggingFiles(true);
            }}
            onDragLeave={(event: DragEvent<HTMLDivElement>) => {
              if (event.currentTarget.contains(event.relatedTarget as Node)) return;
              setDraggingFiles(false);
            }}
            onDrop={onDropAttachmentArea}
          >
            <input
              ref={fileInputRef}
              type="file"
              multiple
              className="hidden"
              onChange={onSelectAttachmentFiles}
            />
            <div className="flex flex-wrap items-center gap-2 text-xs text-ink-500">
              <button
                type="button"
                className="btn-outline !px-2 !py-1 text-xs"
                disabled={streaming || resetting || uploadingAttachments}
                onClick={() => fileInputRef.current?.click()}
              >
                {t("chat.attachments.add")}
              </button>
              <span>{t("chat.attachments.dropHint")}</span>
              {uploadingAttachments && (
                <span className="text-brand-600">{t("chat.attachments.uploading")}</span>
              )}
            </div>
            {pendingFiles.length > 0 && (
              <div className="space-y-1">
                {pendingFiles.map((file, idx) => (
                  <div
                    key={`${file.name}-${file.size}-${file.lastModified}-${idx}`}
                    className="flex items-center justify-between rounded-md border border-ink-200 bg-surface px-2 py-1 text-xs text-ink-600"
                  >
                    <span className="truncate" title={file.name}>
                      {file.name} · {formatLocalFileSize(file.size)}
                    </span>
                    <button
                      type="button"
                      className="ml-2 shrink-0 text-ink-400 hover:text-ink-700"
                      onClick={() => removePendingFile(idx)}
                      disabled={streaming || resetting || uploadingAttachments}
                      title={t("chat.attachments.remove")}
                    >
                      {t("chat.attachments.remove")}
                    </button>
                  </div>
                ))}
              </div>
            )}
            <div className="flex items-end gap-2">
              <textarea
                ref={inputRef}
                className="textarea h-20 flex-1 resize-none"
                placeholder={t("chat.inputPlaceholder")}
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  handleChatTextareaEnterKey(e, () => {
                    if (!streaming && !recovering && !uploadingAttachments) {
                      (e.currentTarget.form as HTMLFormElement).requestSubmit();
                    }
                  });
                }}
                disabled={streaming || recovering || uploadingAttachments}
              />
              <button
                type="button"
                className="btn-outline"
                disabled={streaming || resetting || uploadingAttachments}
                onClick={onResetConversation}
              >
                {resetting ? t("chat.resetting") : t("chat.reset")}
              </button>
              {streaming || recovering ? (
                <button
                  type="button"
                  className="btn-outline border-rose-300 text-rose-600 hover:bg-rose-50"
                  onClick={() => void stopTurn()}
                >
                  {t("chat.stop")}
                </button>
              ) : (
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={
                    uploadingAttachments ||
                    (!input.trim() && pendingFiles.length === 0)
                  }
                >
                  {t("chat.send")}
                </button>
              )}
            </div>
          </div>
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
  const { confirm } = useDialog();
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
                      ? "bg-surface text-brand-700 shadow-sm ring-1 ring-brand-200"
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
                      <div key={item.name} className="rounded-xl border border-ink-200 bg-surface p-3">
                        <div className="flex items-center justify-between gap-3">
                          <div className="min-w-0">
                            <div className="truncate text-sm font-semibold text-ink-900">{item.name}</div>
                            <div className="truncate text-xs text-ink-900">{item.description || t("chat.settings.skills.noDescription")}</div>
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
                              onClick={async () => {
                                if (
                                  !(await confirm(
                                    t("chat.settings.skills.deleteConfirm", { name: item.name }),
                                    { danger: true, okText: t("common.delete") },
                                  ))
                                ) {
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
                          <pre className="mt-3 max-h-64 overflow-auto rounded-lg border border-ink-200 bg-surface p-3 text-xs text-ink-900">
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
                      <div key={item.id} className="rounded-xl border border-ink-200 bg-surface p-3">
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
                            <div className="text-xs text-ink-900">{item.message}</div>
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
                                onClick={async () => {
                                  if (
                                    !(await confirm(
                                      t("chat.settings.cron.deleteConfirm", { name: item.name }),
                                      { danger: true, okText: t("common.delete") },
                                    ))
                                  ) {
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
                      <div key={item.name} className="rounded-xl border border-ink-200 bg-surface p-3">
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
                            <div className="text-xs text-ink-900">{item.description || t("chat.settings.hooks.noDescription")}</div>
                            <div className="text-xs text-ink-900">
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
                                onClick={async () => {
                                  if (
                                    !(await confirm(
                                      t("chat.settings.hooks.deleteConfirm", { name: item.name }),
                                      { danger: true, okText: t("common.delete") },
                                    ))
                                  ) {
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
                            <pre className="max-h-56 overflow-auto rounded-lg border border-ink-200 bg-surface p-3 text-xs text-ink-900">
                              {item.hookMd || t("chat.settings.hooks.noHookMd")}
                            </pre>
                            <pre className="max-h-56 overflow-auto rounded-lg border border-ink-200 bg-surface p-3 text-xs text-ink-900">
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
                      className="textarea h-72 w-full resize-y text-ink-900"
                      value={customSectionDraft}
                      onChange={(e) => setCustomSectionDraft(e.target.value)}
                      disabled={working}
                      placeholder={t("chat.settings.agents.placeholder")}
                    />
                  ) : (
                    <pre className="max-h-72 overflow-auto whitespace-pre-wrap rounded-lg border border-ink-200 bg-surface p-3 text-sm text-ink-900">
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

function buildOpenclawChatUrl(agentId: string, gatewayUrl?: string | null): string | null {
  const suffix = `/?agentId=${encodeURIComponent(agentId)}`;
  const normalizedGatewayUrl = (gatewayUrl || "").trim();
  if (!normalizedGatewayUrl) return null;
  try {
    const parsed = new URL(normalizedGatewayUrl);
    return `${parsed.origin}${suffix}`;
  } catch {
    return null;
  }
}

function buildAgentMyDesktopPath(workspacePath: string): string {
  const base = (workspacePath || "").trim().replace(/[\\/]+$/, "");
  if (!base) return "my-desktop";
  const sep = base.includes("\\") && !base.includes("/") ? "\\" : "/";
  return `${base}${sep}my-desktop`;
}

function formatLocalFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${Math.round(bytes)} B`;
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
  const time = !pending ? formatChatTime(msg.ts) : "";
  const showFooter = !pending && (!!time || !!msg.content);
  return (
    <div className={`group flex flex-col ${isUser ? "items-end" : "items-start"}`}>
      <div
        className={
          isUser
            ? "bg-brand-500 text-white rounded-2xl rounded-tr-sm px-4 py-2 max-w-[75%] whitespace-pre-wrap text-sm"
            : "bg-surface border border-ink-200 text-ink-800 rounded-2xl rounded-tl-sm px-4 py-2 max-w-[75%] shadow-card"
        }
      >
        {msg.content ? (
          isUser ? (
            msg.content
          ) : (
            <ChatMarkdown content={msg.content} />
          )
        ) : pending ? (
          <PendingReply />
        ) : isUser && msg.attachments && msg.attachments.length > 0 ? null : (
          <span className="text-ink-400">{noTextReply}</span>
        )}
        {msg.attachments && msg.attachments.length > 0 && (
          <div className="mt-2 space-y-1">
            {msg.attachments.map((item) => (
              <div
                key={item.id}
                className={
                  isUser
                    ? "rounded-md bg-white/15 px-2 py-1 text-[11px] text-white/90"
                    : "rounded-md bg-ink-50 px-2 py-1 text-[11px] text-ink-600"
                }
                title={item.relativePath}
              >
                <span className="font-medium">{item.name}</span>
                <span className={isUser ? "ml-2 text-white/70" : "ml-2 text-ink-400"}>
                  {formatLocalFileSize(item.sizeBytes)}
                </span>
              </div>
            ))}
          </div>
        )}
      </div>
      {showFooter && (
        <div
          className={`mt-1 flex items-center gap-2 px-1 text-[11px] text-ink-400 ${
            isUser ? "flex-row-reverse" : ""
          }`}
        >
          {time && <span>{time}</span>}
          {msg.content && (
            <span className="opacity-0 transition-opacity group-hover:opacity-100">
              <CopyButton text={msg.content} />
            </span>
          )}
        </div>
      )}
    </div>
  );
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

