/** Hermes Agent management + direct chat ("My Team" → Hermes Agent).
 *
 * Mirrors the OpenClaw chat page but for managed Hermes agents (= Hermes
 * profiles). Differences: permanent delete only, a "Claim existing" action,
 * a per-chat working-directory picker, and a "my-profile" button (opens the
 * profile root). No Import&Optimize / Agent Store / "to Hermes" button.
 */
import {
  Fragment,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ChangeEvent,
  type DragEvent,
} from "react";
import { useNavigate, useParams } from "react-router-dom";
import { SilentLink } from "@/components/SilentLink";
import { useTranslation } from "react-i18next";

import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  Modal,
} from "@/components/ui";
import { useDialog } from "@/components/dialog";
import { AgentCardAvatar } from "@/components/AgentCardAvatar";
import {
  AgentManagementHeader,
  AgentPageToolbar,
  AgentToolbarIconButton,
  AgentViewModeToggle,
} from "@/components/AgentPageToolbar";
import { ChatBubble, NewMessagesDivider } from "@/components/ChatBubble";
import { DesktopIcon, EditIcon, ExternalLinkIcon, RefreshIcon, SettingsIcon, TrashIcon } from "@/components/icons";
import {
  clearChatHistory,
  loadChatHistory,
  loadLastSeenCount,
  normalizeAssistantContent,
  reconcileTranscript,
  saveChatHistory,
  saveLastSeenCount,
  scrollToNewMessagesDivider,
  settledCount,
  reentryDividerIndex,
  turnDividerIndex,
  displayChatMessages,
} from "@/lib/chatHistory";
import { handleChatTextareaEnterKey } from "@/lib/chatInput";
import { resolveDroppedFolderPath } from "@/lib/chatDropFolder";
import { alertIfNativeDirectoryBlocked, ensureUiCapabilities, getNativeDirectoryBlockedMessage, isRemoteBrowser } from "@/lib/remoteClient";
import { cn } from "@/lib/cn";
import { useAutoGrowTextarea } from "@/lib/useAutoGrowTextarea";
import { useStickyScroll } from "@/lib/useStickyScroll";
import {
  api,
  ApiError,
  type ChatAttachmentMeta,
  isNetworkError,
  type ChatHistoryMessage,
  type HermesAgentSummary,
  type HermesCronJob,
  type HermesCronDeliveryTarget,
  type HermesModelSetting,
  type HermesMcpServer,
  type HermesSkillSetting,
  type OpenclawTeam,
} from "@/lib/api";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";
import { useOpRecovery } from "@/lib/useOpRecovery";
import { useNavigationGuard } from "@/lib/useNavigationGuard";
import {
  isCreateCancelConverged,
  isHermesCancelArmed,
  waitForCancelArmed,
} from "@/lib/createCancelVerify";

const CREATE_TEAM_SENTINEL = "__create_team__";
// Cancel only unlocks once the backend confirms rollback; mirror OpenClaw's
// verify-by-list loop so the popup doesn't close before the agent is gone.
const CREATE_CANCEL_VERIFY_TIMEOUT_MS = 30 * 1000;
const CREATE_CANCEL_VERIFY_POLL_MS = 800;
const CHAT_MAX_ATTACHMENTS = 8;
const CHAT_MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024;
const HERMES_PENDING_FILES_CACHE = new Map<string, File[]>();

type CreateCancelState = {
  agentId: string;
  cancelling: boolean;
  /**
   * Cancel is *safe* now — the create has published itself in-flight, so a
   * cancel can no longer be discarded by the "fresh attempt" reset and rollback
   * always applies. The button stays disabled until this flips true (see
   * {@link isHermesCancelArmed}).
   */
  armed?: boolean;
  failed?: boolean;
};

function isAbortError(value: unknown): boolean {
  return value instanceof Error && value.name === "AbortError";
}

// Mirror of the backend Hermes id rules (services/hermes_agents.py):
// lowercase letters/digits/underscore/hyphen; first character must be
// letter/digit; max 64 chars; "default" reserved.
const HERMES_ID_RE = /^[a-z0-9][a-z0-9_-]*$/;

/** Returns an i18n error key for the first failing create-form rule, or null
 *  when the input is valid. Duplicate check is against the loaded agent list. */
function createFieldError(opts: {
  name: string;
  profileId: string;
  modelInheritFrom: string;
  cloneFrom: string;
  teamChoice: string;
  newTeamName: string;
  existingIds: string[];
}): string | null {
  const name = opts.name.trim();
  const id = opts.profileId.trim();
  if (!name) return "hermes.create.errors.nameRequired";
  if (!id) return "hermes.create.errors.idRequired";
  if (!HERMES_ID_RE.test(id)) return "hermes.create.errors.idFormat";
  if (id.length > 64) return "hermes.create.errors.idLength";
  if (id === "default") return "hermes.create.errors.idReserved";
  if (opts.teamChoice === CREATE_TEAM_SENTINEL && !opts.newTeamName.trim()) {
    return "hermes.create.errors.teamRequired";
  }
  // Clone source is optional ("" = no clone); "default" = active profile.
  if (
    opts.cloneFrom &&
    opts.cloneFrom !== "default" &&
    !opts.existingIds.includes(opts.cloneFrom)
  ) {
    return "hermes.create.errors.cloneFromMissing";
  }
  // Model inheritance is optional ("" = none); "default" = active profile.
  if (
    opts.modelInheritFrom &&
    opts.modelInheritFrom !== "default" &&
    !opts.existingIds.includes(opts.modelInheritFrom)
  ) {
    return "hermes.create.errors.modelInheritMissing";
  }
  if (opts.existingIds.includes(id)) return "hermes.create.errors.idDuplicate";
  return null;
}

function agentCardShowsIdLine(agent: { id: string; name: string }): boolean {
  const name = agent.name.trim();
  return name.length > 0 && name !== agent.id;
}

function agentCardTitle(agent: { id: string; name: string }): string {
  const name = agent.name.trim();
  return name || agent.id;
}

function errText(e: unknown): string {
  if (e instanceof ApiError) {
    const flows = e.details?.flow_names;
    if (Array.isArray(flows) && flows.length > 0) {
      return `${e.message}: ${flows.join(", ")}`;
    }
    return e.message;
  }
  if (e instanceof Error) return e.message;
  return String(e);
}

function workdirValidationError(
  e: unknown,
  t: (key: string) => string,
): string {
  if (e instanceof ApiError) {
    switch (e.code) {
      case "PATH_NOT_FOUND":
        return t("hermes.workdirErrors.notFound");
      case "PATH_NOT_A_DIRECTORY":
        return t("hermes.workdirErrors.notDirectory");
      case "PATH_NOT_ACCESSIBLE":
      case "PATH_INVALID":
        return t("hermes.workdirErrors.notAccessible");
      case "INVALID_PAYLOAD":
        return t("hermes.workdirErrors.required");
      default:
        if (e.message && !/^HTTP \d+$/.test(e.message.trim())) {
          return e.message;
        }
        return t("hermes.workdirErrors.invalid");
    }
  }
  return errText(e);
}

function formatLocalFileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes <= 0) return "0 B";
  if (bytes >= 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  if (bytes >= 1024) return `${Math.round(bytes / 1024)} KB`;
  return `${Math.round(bytes)} B`;
}

/** Compare agent lists for the background full-reconcile refresh gate. */
function hermesAgentListsEqual(
  a: HermesAgentSummary[],
  b: HermesAgentSummary[],
): boolean {
  if (a.length !== b.length) return false;
  const key = (x: HermesAgentSummary) =>
    `${x.id}\0${x.name}\0${x.teamId}\0${x.description}`;
  const sa = [...a].sort((x, y) => x.id.localeCompare(y.id)).map(key).join("\n");
  const sb = [...b].sort((x, y) => x.id.localeCompare(y.id)).map(key).join("\n");
  return sa === sb;
}

// ──────────────────────────────────────────────────────────────────────
// Top-level: runtime gate → picker | chat room
// ──────────────────────────────────────────────────────────────────────

export function HermesChat() {
  const { id } = useParams();
  const { t } = useTranslation();
  const [running, setRunning] = useState<boolean | null>(null);
  const [reason, setReason] = useState("");
  const [netDown, setNetDown] = useState(false);

  const checkRuntime = useCallback(() => {
    let alive = true;
    setRunning(null);
    setNetDown(false);
    // Stage 1 — fastest path (binary presence on PATH): render the UI right
    // away instead of blocking on the slow `hermes --version` update-check.
    api
      .getHermesRuntimeStatus("fast")
      .then((fast) => {
        if (!alive) return;
        setRunning(fast.running);
        setReason(fast.reason);
        if (!fast.running) return;
        // Stage 2 — deeper verification in the background. Only re-block the
        // screen if the CLI genuinely can't run; never on a slow probe.
        api
          .getHermesRuntimeStatus("full")
          .then((full) => {
            if (!alive) return;
            if (!full.running) {
              setRunning(false);
              setReason(full.reason);
            }
          })
          .catch(() => {
            /* background verify failed to reach the server — keep showing UI */
          });
      })
      .catch((e) => {
        if (!alive) return;
        // Backend unreachable (service down) ≠ Hermes CLI missing. Say so.
        setNetDown(isNetworkError(e));
        setRunning(false);
        setReason("");
      });
    return () => {
      alive = false;
    };
  }, []);

  useEffect(() => checkRuntime(), [checkRuntime]);

  if (running === null) return <Loading label={t("common.loading")} />;
  if (!running) {
    return (
      <div className="mx-auto max-w-2xl py-16">
        <EmptyState
          title={netDown ? t("common.serviceUnreachableTitle") : t("hermes.notInstalledTitle")}
          hint={
            netDown
              ? t("common.serviceUnreachableHint")
              : `${t("hermes.notInstalled")}${reason ? `\n\n${reason}` : ""}`
          }
          action={
            <button type="button" className="btn-primary" onClick={() => checkRuntime()}>
              {t("common.refresh")}
            </button>
          }
        />
      </div>
    );
  }
  return id ? <ChatRoom agentId={id} /> : <Picker />;
}

// ──────────────────────────────────────────────────────────────────────
// Picker
// ──────────────────────────────────────────────────────────────────────

function Picker() {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const navigate = useNavigate();
  // Lifted from RemoveModal so the page-level navigation guard can see an
  // in-flight removal (the modal is a child component, mounted only while open).
  const [removeBusy, setRemoveBusy] = useState(false);
  const [agents, setAgents] = useState<HermesAgentSummary[]>([]);
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useSessionBackedModalFlag("hermes:picker:create");
  const [removeTarget, setRemoveTarget] = useSessionBackedState<HermesAgentSummary | null>(
    "hermes:picker:removeTarget", null, { isClosed: (v) => v === null },
  );
  const [viewMode, setViewMode] = useState<"card" | "list">("card");

  // ── Create form (lifted out of CreateModal so the build keeps running after
  // the form modal closes, and so all state survives a tab switch via session
  // storage). The actual create request is awaited here in the Picker, which
  // stays mounted while the form modal comes and goes.
  const [createName, setCreateName] = useSessionBackedState("hermes:create:name", "");
  const [createProfileId, setCreateProfileId] = useSessionBackedState("hermes:create:profileId", "");
  const [createResponsibility, setCreateResponsibility] = useSessionBackedState(
    "hermes:create:responsibility", "",
  );
  const [createModelInheritFrom, setCreateModelInheritFrom] = useSessionBackedState(
    "hermes:create:modelInheritFrom",
    "",
  );
  // Optional "clone config from another agent" (default: none). When set, the
  // full-clone checkbox is enabled; cleared back to none disables/unchecks it.
  const [createCloneFrom, setCreateCloneFrom] = useSessionBackedState(
    "hermes:create:cloneFrom",
    "",
  );
  const [createCloneAll, setCreateCloneAll] = useSessionBackedState(
    "hermes:create:cloneAll", false, { isClosed: (v) => v === false },
  );
  const [createTeamChoice, setCreateTeamChoice] = useSessionBackedState("hermes:create:teamChoice", "");
  const [createNewTeamName, setCreateNewTeamName] = useSessionBackedState("hermes:create:newTeamName", "");
  const [createError, setCreateError] = useState("");

  // ── Build-progress popup (mirrors OpenClaw's work popup). `workPopupText`,
  // `workPopupRunning`, `workPopupSuccess` and `createCancelState` are all
  // session-backed so the popup restores in its true state on return — the
  // setters write through to storage synchronously even after unmount, so a
  // create that finishes while we're navigated away restores as done/failed
  // rather than flashing back to a stale "running".
  const [workPopupOpen, setWorkPopupOpen] = useSessionBackedModalFlag("hermes:create:workPopupOpen");
  const [workPopupRunning, setWorkPopupRunning] = useSessionBackedState(
    "hermes:create:workPopupRunning", false, { isClosed: (v) => v === false },
  );
  // Session-backed (unlike OpenClaw's transient flag) so a *failed* build that
  // finishes while we're unmounted restores in red, not just as green text:
  // useSessionBackedState's setter writes to storage synchronously even after
  // unmount, so the failure outcome survives a tab switch. Only persist the
  // failure (false); success is the default and clears the key.
  const [workPopupSuccess, setWorkPopupSuccess] = useSessionBackedState(
    "hermes:create:workPopupSuccess", true, { isClosed: (v) => v === true },
  );
  const [workPopupText, setWorkPopupText] = useSessionBackedState(
    "hermes:create:workPopupText", "", { isClosed: (v) => v.trim() === "" },
  );
  const [createCancelState, setCreateCancelState] = useSessionBackedState<CreateCancelState | null>(
    "hermes:create:cancelState", null, { isClosed: (v) => v === null },
  );
  const [createCancelCleanupNotice, setCreateCancelCleanupNotice] = useSessionBackedState<
    string | null
  >("hermes:create:cancel-cleanup-notice", null, { isClosed: (v) => v === null });
  const createAbortRef = useRef<AbortController | null>(null);
  const createCancelRequestedRef = useRef(false);
  // Guards against a double-submit (e.g. a fast double-click in the window
  // before the modal closes) firing two POST /hermes/agents for the same id —
  // which on the backend would race and could clobber the winner's profile.
  const createInFlightRef = useRef(false);
  const cancelVerifyInFlightRef = useRef(false);
  // Single-flights the "is it safe to cancel yet?" poll (see armCancelWhenSafe).
  const armPollInFlightRef = useRef(false);
  const pickerMountedRef = useRef(true);

  // Block leaving the page while a removal or a create-cancellation is in flight,
  // so neither can be orphaned mid-operation. (One blocker at a time, so both
  // conditions are folded together.)
  useNavigationGuard(removeBusy || createCancelState?.cancelling === true, () => {
    void alert(t("common.navGuardBusy"));
  });

  useEffect(() => {
    pickerMountedRef.current = true;
    return () => {
      pickerMountedRef.current = false;
    };
  }, []);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [a, tm] = await Promise.all([
        api.listHermesAgents("fast"),
        api.listOpenclawTeams(),
      ]);
      if (!pickerMountedRef.current) return;
      setAgents(a.items);
      setTeams(tm.items);
    } catch (e) {
      if (!pickerMountedRef.current) return;
      setError(errText(e));
    } finally {
      if (pickerMountedRef.current) setLoading(false);
    }
    // Background full reconcile — authoritative but slow; refresh only on diff.
    void api
      .listHermesAgents("full")
      .then((full) => {
        if (!pickerMountedRef.current) return;
        setAgents((prev) =>
          hermesAgentListsEqual(prev, full.items) ? prev : full.items,
        );
      })
      .catch(() => {
        /* fast list already shown; next manual reload retries */
      });
  }, []);

  // Quiet refetch (no loading spinner) used by the build-progress poll.
  const refreshAgents = useCallback(async () => {
    try {
      const a = await api.listHermesAgents("fast");
      if (!pickerMountedRef.current) return;
      setAgents(a.items);
    } catch {
      /* ignore poll errors — the next tick retries */
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const workPopupBusy =
    createCancelState?.cancelling === true ||
    (workPopupRunning && !createCancelState?.failed);
  const showCreateCancelAction = createCancelState !== null;
  // Only allow cancel once armed (safe) — or to retry a failed cancel. Until
  // armed the button is shown but disabled ("准备取消…").
  const createCancelEnabled =
    showCreateCancelAction &&
    !(createCancelState?.cancelling ?? false) &&
    ((createCancelState?.armed ?? false) || (createCancelState?.failed ?? false));
  const workPopupDisplayText =
    workPopupText ||
    (workPopupOpen && workPopupRunning ? t("hermes.create.workPopup.running") : "");

  const resetCreateForm = useCallback(() => {
    setCreateName("");
    setCreateProfileId("");
    setCreateResponsibility("");
    setCreateModelInheritFrom("");
    setCreateCloneFrom("");
    setCreateCloneAll(false);
    setCreateTeamChoice("");
    setCreateNewTeamName("");
  }, [
    setCreateName,
    setCreateProfileId,
    setCreateResponsibility,
    setCreateModelInheritFrom,
    setCreateCloneFrom,
    setCreateCloneAll,
    setCreateTeamChoice,
    setCreateNewTeamName,
  ]);

  const resetCreateCancelState = useCallback(() => {
    setCreateCancelState(null);
    createAbortRef.current = null;
    createCancelRequestedRef.current = false;
  }, [setCreateCancelState]);

  const resetWorkPopupDisplayState = useCallback(() => {
    setWorkPopupRunning(false);
    setWorkPopupSuccess(true);
    setWorkPopupText("");
  }, [setWorkPopupText]);

  const openWorkPopup = useCallback(() => {
    setWorkPopupOpen(true);
    setWorkPopupRunning(true);
    setWorkPopupSuccess(true);
    setWorkPopupText(t("hermes.create.workPopup.running"));
  }, [setWorkPopupOpen, setWorkPopupText, t]);

  const finishWorkPopup = useCallback(
    (success: boolean, detail?: string) => {
      if (createCancelRequestedRef.current) return;
      setWorkPopupRunning(false);
      setWorkPopupSuccess(success);
      setWorkPopupText(
        success
          ? detail || t("hermes.create.workPopup.created", { id: "" })
          : detail || t("hermes.create.workPopup.failed"),
      );
      resetCreateCancelState();
    },
    [setWorkPopupText, resetCreateCancelState, t],
  );

  // Keep the cancel button disabled until the create publishes itself in-flight,
  // i.e. until a cancel can no longer be discarded by the commit's "fresh
  // attempt" reset. Arming late is what makes cancel reliable instead of racing
  // a half-built create.
  const armCancelWhenSafe = useCallback(
    async (agentId: string) => {
      if (armPollInFlightRef.current) return;
      armPollInFlightRef.current = true;
      try {
        const armed = await waitForCancelArmed(`hermes_create:${agentId}`, isHermesCancelArmed, {
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
    },
    [setCreateCancelState],
  );

  // Durable recovery across refresh / tab close+reopen: a localStorage pointer
  // to the in-flight create op + on-mount status query (+ WS for the terminal
  // transition). The in-page awaited POST still drives the happy path; this only
  // fires when the awaiting closure is gone (the page was reloaded/reopened).
  const { track: trackOp, clear: clearOp } = useOpRecovery("hermes:create:op", {
    onRunning: (p) => {
      // A cancel may already be resuming on this same mount (it sets the ref
      // synchronously in its own effect); don't clobber its cancelling state.
      if (createCancelRequestedRef.current) return;
      setCreateCancelState({ agentId: p.agentId, cancelling: false, armed: false });
      openWorkPopup();
      // Recovered mid-create: re-derive whether cancel is safe yet.
      void armCancelWhenSafe(p.agentId);
    },
    onSucceeded: (p) => {
      if (createCancelRequestedRef.current) return;
      finishWorkPopup(true, t("hermes.create.workPopup.created", { id: p.agentId }));
      resetCreateForm();
      void reload();
    },
    onFailed: (_p, detail) => {
      if (createCancelRequestedRef.current) return;
      finishWorkPopup(
        false,
        detail === "cancelled" ? t("hermes.create.workPopup.cancelled") : detail,
      );
    },
    onMissing: () => {
      // The op never registered within the grace window — tear the popup down
      // silently rather than leaving a stale "running" shell.
      if (createCancelRequestedRef.current) return;
      setWorkPopupOpen(false);
      resetWorkPopupDisplayState();
      resetCreateCancelState();
      createInFlightRef.current = false;
    },
  });

  const verifyHermesCancelConverged = useCallback(async (agentId: string): Promise<void> => {
    const verifyDeadline = Date.now() + CREATE_CANCEL_VERIFY_TIMEOUT_MS;
    let consecutiveAbsent = 0;
    while (true) {
      const [listed, op] = await Promise.all([
        api.listHermesAgents("fast"),
        api.getOperationStatus(`hermes_create:${agentId}`),
      ]);
      const absent = !listed.items.some((item) => item.id === agentId);
      if (absent) consecutiveAbsent += 1;
      else consecutiveAbsent = 0;
      if (isCreateCancelConverged(absent, op, consecutiveAbsent)) return;
      if (Date.now() >= verifyDeadline) {
        if (!absent) {
          throw new Error(t("hermes.create.workPopup.cancelAgentStillVisible"));
        }
        throw new Error(t("hermes.create.workPopup.cancelOpStillRunning"));
      }
      await new Promise((resolve) => window.setTimeout(resolve, CREATE_CANCEL_VERIFY_POLL_MS));
    }
  }, [t]);

  const closeWorkPopupAfterCancel = useCallback(() => {
    clearOp();
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetCreateCancelState();
    createInFlightRef.current = false;
    void refreshAgents();
  }, [
    clearOp,
    refreshAgents,
    resetCreateCancelState,
    resetWorkPopupDisplayState,
    setWorkPopupOpen,
  ]);

  const dismissCancelFailureNotice = useCallback(
    (agentId: string) => {
      setCreateCancelCleanupNotice(agentId);
      clearOp();
      setWorkPopupOpen(false);
      resetWorkPopupDisplayState();
      resetCreateCancelState();
      createInFlightRef.current = false;
      createCancelRequestedRef.current = false;
    },
    [
      clearOp,
      resetCreateCancelState,
      resetWorkPopupDisplayState,
      setCreateCancelCleanupNotice,
      setWorkPopupOpen,
    ],
  );

  const closeWorkPopupFromModal = useCallback(() => {
    if (createCancelState?.failed && createCancelState.agentId) {
      dismissCancelFailureNotice(createCancelState.agentId);
      return;
    }
    setWorkPopupOpen(false);
    resetWorkPopupDisplayState();
    resetCreateCancelState();
  }, [
    createCancelState,
    dismissCancelFailureNotice,
    resetCreateCancelState,
    resetWorkPopupDisplayState,
    setWorkPopupOpen,
  ]);

  const runCreateCancelFlow = useCallback(
    async (agentId: string, options: { postCancel: boolean }) => {
      if (cancelVerifyInFlightRef.current) return;
      cancelVerifyInFlightRef.current = true;
      createCancelRequestedRef.current = true;
      setWorkPopupOpen(true);
      setWorkPopupRunning(true);
      setCreateCancelState({ agentId, cancelling: true });
      try {
        if (options.postCancel) {
          createAbortRef.current?.abort();
          setWorkPopupText(t("hermes.create.workPopup.cancelRunning"));
          await api.cancelHermesAgentCreate(agentId);
        }
        setWorkPopupText(t("hermes.create.workPopup.cancelVerifying"));
        await verifyHermesCancelConverged(agentId);
        closeWorkPopupAfterCancel();
      } catch (e) {
        createCancelRequestedRef.current = false;
        setWorkPopupRunning(false);
        setWorkPopupSuccess(false);
        setWorkPopupText(
          t("hermes.create.workPopup.cancelFailed", { message: errText(e) }),
        );
        setCreateCancelState({ agentId, cancelling: false, failed: true });
      } finally {
        cancelVerifyInFlightRef.current = false;
      }
    },
    [
      closeWorkPopupAfterCancel,
      setCreateCancelState,
      setWorkPopupOpen,
      setWorkPopupText,
      verifyHermesCancelConverged,
      t,
    ],
  );

  const cancelCreate = useCallback(async () => {
    if (createCancelState === null || createCancelState.cancelling) return;
    await runCreateCancelFlow(createCancelState.agentId, { postCancel: true });
  }, [createCancelState, runCreateCancelFlow]);

  useEffect(() => {
    const state = createCancelState;
    if (state === null || !state.cancelling) return;
    void runCreateCancelFlow(state.agentId, { postCancel: false });
    // Resume verify-only after refresh / remount while a cancel was in flight.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const submitCreate = useCallback(async () => {
    if (createInFlightRef.current) return;
    const profileId = createProfileId.trim();
    const name = createName.trim();
    const responsibility = createResponsibility.trim();
    // Validate on the spot: show the problem IN the form and keep it open —
    // never close the modal or fire a request for invalid input or a duplicate
    // id. (The flag is set only after this passes, so a corrected resubmit is
    // not locked out.)
    const errKey = createFieldError({
      name,
      profileId,
      modelInheritFrom: createModelInheritFrom,
      cloneFrom: createCloneFrom,
      teamChoice: createTeamChoice,
      newTeamName: createNewTeamName,
      existingIds: agents.map((a) => a.id),
    });
    if (errKey) {
      setCreateError(t(errKey, { id: profileId }));
      return;
    }

    createInFlightRef.current = true;
    try {
      let teamId: string | undefined;
      try {
        teamId = await resolveTeamId(createTeamChoice, createNewTeamName);
      } catch (e) {
        setCreateError(errText(e)); // keep modal open
        return;
      }
      setShowCreate(false);
      setCreateError("");
      const ac = new AbortController();
      createAbortRef.current = ac;
      createCancelRequestedRef.current = false;
      setCreateCancelState({ agentId: profileId, cancelling: false, armed: false });
      trackOp({ opId: `hermes_create:${profileId}`, agentId: profileId });
      openWorkPopup();
      void armCancelWhenSafe(profileId);
      try {
        const created = await api.createHermesAgent(
          {
            id: profileId,
            name,
            responsibility,
            teamId,
            modelInheritFrom: createModelInheritFrom,
            cloneFrom: createCloneFrom,
            // Full-clone only applies when a clone source is chosen.
            cloneAll: createCloneFrom ? createCloneAll : false,
          },
          { signal: ac.signal },
        );
        if (createCancelRequestedRef.current) return;
        clearOp();
        finishWorkPopup(true, t("hermes.create.workPopup.created", { id: created.id }));
        resetCreateForm();
        await reload();
      } catch (e) {
        // Cancelled (or unmounted mid-flight) → handled by cancelCreate / op recovery.
        if (createCancelRequestedRef.current || isAbortError(e)) return;
        // A user-input rejection (duplicate id lost a race vs a stale list, or
        // invalid payload) belongs back IN the form, not buried in the progress
        // popup — reopen it with the message.
        if (
          e instanceof ApiError &&
          (e.code === "AGENT_ALREADY_EXISTS" || e.code === "INVALID_PAYLOAD")
        ) {
          clearOp();
          setWorkPopupOpen(false);
          resetWorkPopupDisplayState();
          resetCreateCancelState();
          setCreateError(
            e.code === "AGENT_ALREADY_EXISTS"
              ? t("hermes.create.errors.idDuplicate", { id: profileId })
              : errText(e),
          );
          setShowCreate(true);
          return;
        }
        clearOp();
        finishWorkPopup(false, errText(e));
      } finally {
        if (createAbortRef.current === ac) createAbortRef.current = null;
      }
    } finally {
      if (!createCancelRequestedRef.current) {
        createInFlightRef.current = false;
      }
    }
  }, [
    createProfileId, createName, createResponsibility, createModelInheritFrom,
    createCloneFrom, createCloneAll,
    createTeamChoice, createNewTeamName,
    agents, setShowCreate, setCreateCancelState, openWorkPopup, finishWorkPopup,
    resetCreateForm, resetWorkPopupDisplayState, resetCreateCancelState, setWorkPopupOpen,
    reload, trackOp, clearOp, armCancelWhenSafe, t,
  ]);

  // Recovery across refresh / close+reopen is owned solely by useOpRecovery
  // above (durable localStorage pointer → WS subscribe + graceful status poll).
  // There is intentionally no separate on-mount "reconcile popup vs op status"
  // effect: it duplicated that work and closed the popup on a transient
  // `not_found` during the create's pre-registration window — the popup-vanishes
  // bug. The session-backed popup fields restore the visible state on their own.

  const grouped = useMemo(() => {
    const UNGROUPED = "__ungrouped__";
    const m = new Map<string, { label: string; list: HermesAgentSummary[] }>();
    for (const a of agents) {
      const key = a.teamId || UNGROUPED;
      const label = a.teamName || t("hermes.ungrouped");
      const entry = m.get(key) ?? { label, list: [] };
      entry.list.push(a);
      m.set(key, entry);
    }
    // Mirror OpenClaw: "ungrouped" first, then teams alphabetically by name,
    // agents within a group alphabetically by name.
    return Array.from(m.entries())
      .sort(([ka, va], [kb, vb]) => {
        if (ka === UNGROUPED && kb !== UNGROUPED) return -1;
        if (ka !== UNGROUPED && kb === UNGROUPED) return 1;
        return va.label.localeCompare(vb.label);
      })
      .map(([key, entry]) => ({
        key,
        label: entry.label,
        list: entry.list.slice().sort((a, b) => a.name.localeCompare(b.name)),
      }));
  }, [agents, t]);

  return (
    <div className="space-y-5">
      <AgentManagementHeader
        title={t("hermes.title")}
        description={t("hermes.modelNote")}
        leading={
          <AgentViewModeToggle
            viewMode={viewMode}
            onChange={setViewMode}
            cardLabel={t("chat.viewCard")}
            listLabel={t("chat.viewList")}
          />
        }
        actions={
          <button
            type="button"
            className="btn-primary inline-flex h-9 items-center gap-1.5 px-4"
            onClick={() => {
              setCreateError("");
              setShowCreate(true);
            }}
            disabled={workPopupBusy}
          >
            {t("hermes.createAgent")}
          </button>
        }
      />

      {createCancelCleanupNotice && (
        <div className="flex items-start gap-3 rounded-lg border border-amber-200 bg-amber-50 px-4 py-3 text-sm text-amber-900 dark:border-amber-800 dark:bg-amber-950/40 dark:text-amber-100">
          <p className="flex-1">
            {t("hermes.create.workPopup.cancelCleanupHint", { id: createCancelCleanupNotice })}
          </p>
          <button
            type="button"
            className="shrink-0 text-amber-700 underline hover:text-amber-900 dark:text-amber-200"
            onClick={() => setCreateCancelCleanupNotice(null)}
          >
            {t("hermes.create.workPopup.cancelCleanupDismiss")}
          </button>
        </div>
      )}

      {error && <ErrorBox>{error}</ErrorBox>}
      {loading ? (
        <Loading label={t("common.loading")} />
      ) : agents.length === 0 ? (
        <EmptyState title={t("hermes.title")} hint={t("hermes.listEmpty")} />
      ) : viewMode === "card" ? (
        <div className="space-y-5">
          {grouped.map(({ key, label, list }) => (
            <div key={key} className="space-y-3">
              <div className="inline-flex items-center gap-1 rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-semibold text-brand-700">
                {t("hermes.team")}: {label}
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {list.map((a) => (
                  <SilentLink
                    key={a.id}
                    as="div"
                    to={`/hermes/${a.id}`}
                    className="group card block p-5 transition-all hover:border-brand-300 hover:shadow-[0_0_24px_-6px_rgb(var(--brand-300))]"
                  >
                    <div className="flex items-start justify-between">
                      <AgentCardAvatar platform="hermes" />
                      <button
                        type="button"
                        className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-md text-rose-500 hover:bg-rose-50 hover:text-rose-700"
                        title={t("hermes.removeAgent")}
                        aria-label={t("hermes.removeAgent")}
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setRemoveTarget(a);
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
                      <p className="mt-2 line-clamp-3 text-xs text-ink-500">{a.description}</p>
                    )}
                  </SilentLink>
                ))}
              </div>
            </div>
          ))}
        </div>
      ) : (
        <Card className="overflow-hidden p-0">
          <table className="w-full text-sm">
            <thead className="bg-ink-50 text-ink-500">
              <tr>
                <th className="px-4 py-2 text-left font-medium">{t("chat.agentLabel")}</th>
                <th className="px-4 py-2 text-left font-medium">{t("chat.columnId")}</th>
                <th className="px-4 py-2 text-left font-medium">{t("chat.teamLabel")}</th>
                <th className="px-4 py-2 text-left font-medium">{t("common.description")}</th>
                <th className="px-4 py-2 text-right font-medium">{t("common.actions")}</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <tr key={a.id} className="table-row">
                  <td className="px-4 py-3 font-medium text-ink-900">{a.name}</td>
                  <td className="px-4 py-3 font-mono text-xs text-ink-500">{a.id}</td>
                  <td className="px-4 py-3 text-ink-600">{a.teamName || t("hermes.ungrouped")}</td>
                  <td className="px-4 py-3 text-ink-600">{a.description || t("common.none")}</td>
                  <td className="px-4 py-3 text-right">
                    <button
                      type="button"
                      className="btn-primary"
                      onClick={() => navigate(`/hermes/${a.id}`)}
                    >
                      {t("hermes.open")}
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Card>
      )}

      {showCreate && (
        <CreateModal
          teams={teams}
          name={createName}
          onNameChange={setCreateName}
          profileId={createProfileId}
          onProfileIdChange={setCreateProfileId}
          responsibility={createResponsibility}
          onResponsibilityChange={setCreateResponsibility}
          modelInheritFrom={createModelInheritFrom}
          onModelInheritFromChange={setCreateModelInheritFrom}
          cloneFrom={createCloneFrom}
          onCloneFromChange={setCreateCloneFrom}
          cloneAll={createCloneAll}
          onCloneAllChange={setCreateCloneAll}
          existingProfiles={agents}
          teamChoice={createTeamChoice}
          onTeamChoiceChange={setCreateTeamChoice}
          newTeamName={createNewTeamName}
          onNewTeamNameChange={setCreateNewTeamName}
          error={createError}
          onClose={() => setShowCreate(false)}
          onSubmit={() => void submitCreate()}
        />
      )}

      <Modal
        open={workPopupOpen}
        onClose={() => {
          if (workPopupBusy) return;
          closeWorkPopupFromModal();
        }}
        title={t("hermes.create.workPopup.title")}
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
              {t("hermes.create.workPopup.cancelCleanupHint", { id: createCancelState.agentId })}
            </p>
          )}
          {workPopupBusy && <Loading />}
          {showCreateCancelAction && (
            <div className="flex justify-end gap-2">
              <button
                type="button"
                className="btn-outline"
                onClick={() => void cancelCreate()}
                disabled={!createCancelEnabled}
              >
                {createCancelState?.cancelling
                  ? t("hermes.create.cancelling")
                  : createCancelState?.failed
                    ? t("hermes.create.workPopup.cancelRetry")
                    : createCancelState?.armed
                      ? t("hermes.create.cancelCreate")
                      : t("hermes.create.workPopup.cancelPreparing")}
              </button>
              {createCancelState?.failed && (
                <button
                  type="button"
                  className="btn-primary"
                  onClick={() => dismissCancelFailureNotice(createCancelState.agentId)}
                >
                  {t("hermes.create.workPopup.cancelForceClose")}
                </button>
              )}
            </div>
          )}
          {!workPopupBusy && !createCancelState?.failed && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-primary"
                onClick={closeWorkPopupFromModal}
              >
                {t("hermes.create.workPopup.close")}
              </button>
            </div>
          )}
        </div>
      </Modal>

      {removeTarget && (
        <RemoveModal
          agent={removeTarget}
          onBusyChange={setRemoveBusy}
          onClose={() => setRemoveTarget(null)}
          onDone={() => {
            setRemoveBusy(false);
            setRemoveTarget(null);
            void reload();
          }}
        />
      )}
    </div>
  );
}

/** Team picker with an inline "create new team" option (mirrors OpenClaw). */
function TeamSelect({
  teams,
  value,
  onChange,
  newTeamName,
  onNewTeamNameChange,
}: {
  teams: OpenclawTeam[];
  value: string;
  onChange: (v: string) => void;
  newTeamName: string;
  onNewTeamNameChange: (v: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="space-y-2">
      <select
        className="select"
        value={value}
        onChange={(e) => onChange(e.target.value)}
      >
        <option value="">{t("hermes.ungrouped")}</option>
        {teams.map((tm) => (
          <option key={tm.id} value={tm.id}>
            {tm.name}
          </option>
        ))}
        <option value={CREATE_TEAM_SENTINEL}>{t("assistant.teamSelect.createNew")}</option>
      </select>
      {value === CREATE_TEAM_SENTINEL && (
        <input
          className="input"
          value={newTeamName}
          placeholder={t("assistant.teamSelect.newTeamPlaceholder")}
          onChange={(e) => onNewTeamNameChange(e.target.value)}
        />
      )}
    </div>
  );
}

/** Resolve the team selection to a concrete team id (creating one if the
 *  "new team" sentinel is chosen). Returns undefined for "ungrouped". */
async function resolveTeamId(
  choice: string,
  newTeamName: string,
): Promise<string | undefined> {
  if (choice === CREATE_TEAM_SENTINEL) {
    const name = newTeamName.trim();
    if (!name) throw new Error("new team name required");
    const created = await api.createOpenclawTeam({ name });
    return created.id;
  }
  return choice || undefined;
}

/** Presentational create form. The build lifecycle (request, work popup,
 *  cancel) is owned by the Picker so it survives this modal closing on submit
 *  and a tab switch — see Picker's submitCreate/cancelCreate. */
function CreateModal({
  teams,
  name,
  onNameChange,
  profileId,
  onProfileIdChange,
  responsibility,
  onResponsibilityChange,
  modelInheritFrom,
  onModelInheritFromChange,
  cloneFrom,
  onCloneFromChange,
  cloneAll,
  onCloneAllChange,
  existingProfiles,
  teamChoice,
  onTeamChoiceChange,
  newTeamName,
  onNewTeamNameChange,
  error,
  onClose,
  onSubmit,
}: {
  teams: OpenclawTeam[];
  name: string;
  onNameChange: (v: string) => void;
  profileId: string;
  onProfileIdChange: (v: string) => void;
  responsibility: string;
  onResponsibilityChange: (v: string) => void;
  modelInheritFrom: string;
  onModelInheritFromChange: (v: string) => void;
  cloneFrom: string;
  onCloneFromChange: (v: string) => void;
  cloneAll: boolean;
  onCloneAllChange: (v: boolean) => void;
  existingProfiles: HermesAgentSummary[];
  teamChoice: string;
  onTeamChoiceChange: (v: string) => void;
  newTeamName: string;
  onNewTeamNameChange: (v: string) => void;
  error: string;
  onClose: () => void;
  onSubmit: () => void;
}) {
  const { t } = useTranslation();
  const teamReady = teamChoice !== CREATE_TEAM_SENTINEL || newTeamName.trim().length > 0;

  return (
    <Modal open onClose={onClose} title={t("hermes.create.title")} width="max-w-2xl">
      <div className="space-y-3">
        {error && <ErrorBox>{error}</ErrorBox>}
        <div>
          <label className="label">{t("hermes.create.nameLabel")}</label>
          <input
            className="input"
            value={name}
            placeholder={t("hermes.create.namePlaceholder")}
            onChange={(e) => onNameChange(e.target.value)}
          />
        </div>
        <div>
          <label className="label">{t("hermes.create.idLabel")}</label>
          <input
            className="input font-mono"
            value={profileId}
            placeholder={t("hermes.create.idPlaceholder")}
            onChange={(e) => onProfileIdChange(e.target.value)}
          />
          <div className="mt-1 text-xs text-ink-400">{t("hermes.create.idHint")}</div>
        </div>
        <div>
          <label className="label">{t("hermes.create.responsibility")}</label>
          <textarea
            className="textarea h-24"
            value={responsibility}
            placeholder={t("hermes.create.responsibilityPlaceholder")}
            onChange={(e) => onResponsibilityChange(e.target.value)}
          />
        </div>
        <div>
          <label className="label">{t("hermes.create.cloneFromLabel")}</label>
          <select
            className="select"
            value={cloneFrom}
            onChange={(e) => {
              const v = e.target.value;
              onCloneFromChange(v);
              // Full clone only makes sense with a source — reset it otherwise.
              if (!v) onCloneAllChange(false);
            }}
          >
            <option value="">{t("hermes.create.cloneFromNone")}</option>
            <option value="default">{t("hermes.create.cloneFromDefault")}</option>
            {existingProfiles.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {`${agent.name || agent.id} (${agent.id})`}
              </option>
            ))}
          </select>
          <div className="mt-1 text-xs text-ink-400">{t("hermes.create.cloneFromHint")}</div>
          <label
            className={`mt-2 flex items-center gap-2 text-sm ${
              cloneFrom ? "text-ink-700" : "text-ink-300"
            }`}
          >
            <input
              type="checkbox"
              checked={cloneFrom ? cloneAll : false}
              disabled={!cloneFrom}
              onChange={(e) => onCloneAllChange(e.target.checked)}
            />
            {t("hermes.create.cloneAllLabel")}
          </label>
        </div>
        <div>
          <label className="label">{t("hermes.create.modelInheritLabel")}</label>
          <select
            className="select"
            value={modelInheritFrom}
            onChange={(e) => onModelInheritFromChange(e.target.value)}
          >
            <option value="">{t("hermes.create.modelInheritNone")}</option>
            <option value="default">{t("hermes.create.modelInheritDefault")}</option>
            {existingProfiles.map((agent) => (
              <option key={agent.id} value={agent.id}>
                {`${agent.name || agent.id} (${agent.id})`}
              </option>
            ))}
          </select>
          <div className="mt-1 text-xs text-ink-400">{t("hermes.create.modelInheritHint")}</div>
        </div>
        <div>
          <label className="label">{t("hermes.create.teamLabel")}</label>
          <TeamSelect
            teams={teams}
            value={teamChoice}
            onChange={onTeamChoiceChange}
            newTeamName={newTeamName}
            onNewTeamNameChange={onNewTeamNameChange}
          />
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="btn-outline" onClick={onClose}>
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={onSubmit}
            disabled={!name.trim() || !profileId.trim() || !teamReady}
          >
            {t("hermes.create.submit")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function RemoveModal({
  agent,
  onClose,
  onDone,
  onBusyChange,
}: {
  agent: HermesAgentSummary;
  onClose: () => void;
  onDone: () => void;
  /** Lift busy state so the page-level navigation guard can observe it. */
  onBusyChange?: (busy: boolean) => void;
}) {
  const { t } = useTranslation();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const setBusyBoth = (b: boolean) => {
    setBusy(b);
    onBusyChange?.(b);
  };

  // Make sure the lifted flag is released if the modal unmounts mid-busy.
  useEffect(() => () => onBusyChange?.(false), [onBusyChange]);

  const remove = async () => {
    setBusyBoth(true);
    setError("");
    try {
      await api.deleteHermesAgent(agent.id);
      onDone();
    } catch (e) {
      setError(errText(e));
      setBusyBoth(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={t("hermes.remove.title")} dismissible={!busy}>
      <div className="space-y-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        <p className="text-sm text-rose-600">{t("hermes.remove.warning")}</p>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.remove.confirmLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono"
            value={confirm}
            placeholder={agent.id}
            onChange={(e) => setConfirm(e.target.value)}
          />
        </label>
        <div className="flex justify-end gap-2">
          <button
            type="button"
            className="rounded border border-ink-200 px-3 py-2 text-sm"
            onClick={onClose}
            disabled={busy}
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="rounded bg-rose-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => void remove()}
            disabled={busy || confirm.trim() !== agent.id}
          >
            {busy ? t("hermes.remove.deleting") : t("hermes.remove.submit")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Chat room
// ──────────────────────────────────────────────────────────────────────

interface ChatMsg {
  role: "user" | "assistant" | "system";
  content: string;
  attachments?: ChatAttachmentMeta[];
  ts?: number;
}

type GatewayNotice = {
  kind: "success" | "error";
  text: string;
};

function ChatRoom({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const navigate = useNavigate();
  const [name, setName] = useState(agentId);
  const [teamName, setTeamName] = useState("");
  const [teamId, setTeamId] = useState("");
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [teamEditOpen, setTeamEditOpen] = useSessionBackedModalFlag(`hermes:${agentId}:teamEdit:open`);
  const [teamEditChoice, setTeamEditChoice] = useSessionBackedState(`hermes:${agentId}:teamEdit:choice`, "");
  const [teamEditNewTeamName, setTeamEditNewTeamName] = useSessionBackedState(`hermes:${agentId}:teamEdit:newTeam`, "");
  const [teamEditError, setTeamEditError] = useState("");
  const [teamEditSaving, setTeamEditSaving] = useState(false);
  const [profileRoot, setProfileRoot] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  // Persist the unsent composer text so switching tabs doesn't discard it.
  const [input, setInput] = useSessionBackedState(`hermes:${agentId}:input`, "", {
    isClosed: (v) => v.trim() === "",
  });
  const inputRef = useAutoGrowTextarea(input, { minHeightPx: 80, maxHeightPx: 240 });
  const [workdir, setWorkdir] = useState(
    () => localStorage.getItem(`hermes-workdir-${agentId}`) || "",
  );
  const [workdirEditing, setWorkdirEditing] = useState(false);
  const [workdirDraft, setWorkdirDraft] = useState("");
  const [workdirError, setWorkdirError] = useState("");
  const [workdirSaving, setWorkdirSaving] = useState(false);
  const updateWorkdir = useCallback((next: string) => {
    setWorkdir(next);
    if (next.trim()) localStorage.setItem(`hermes-workdir-${agentId}`, next);
    else localStorage.removeItem(`hermes-workdir-${agentId}`);
  }, [agentId]);

  useEffect(() => {
    if (workdir.trim()) return;
    let cancelled = false;
    void ensureUiCapabilities()
      .then((caps) => {
        if (cancelled || !caps.userHomeDir) return;
        const stored = localStorage.getItem(`hermes-workdir-${agentId}`);
        if (stored && stored !== "~") {
          updateWorkdir(stored);
          return;
        }
        updateWorkdir(caps.userHomeDir);
      })
      .catch(() => {
        /* leave empty until user sets manually */
      });
    return () => {
      cancelled = true;
    };
  }, [agentId, updateWorkdir, workdir]);
  const [sending, setSending] = useState(false);
  // True while reconnecting to a turn whose SSE stream was detached by a tab
  // switch / refresh: we poll GET /chat/status (unbounded while the server says
  // "running") to drive the pending bubble + step trail, then adopt the final
  // answer — instead of giving up after a fixed window.
  const [recovering, setRecovering] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useSessionBackedModalFlag(`hermes:${agentId}:settings:open`);
  const [opening, setOpening] = useState(false);
  const [dashboardBusy, setDashboardBusy] = useState(false);
  const [gatewayBusy, setGatewayBusy] = useState(false);
  const [gatewayNotice, setGatewayNotice] = useState<GatewayNotice | null>(null);
  const gatewayNoticeTimerRef = useRef<number | null>(null);
  // Synchronous click lock: prevents double-trigger before state rerender.
  const gatewayStartLockRef = useRef(false);
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
  // Index (into the displayed, non-system message list) before which a "new
  // messages" divider is drawn on re-entry; -1 = none. Computed once at load.
  const [newDividerAt, setNewDividerAt] = useState(-1);
  // Divider anchor used to jump to the first unseen message block when the user
  // returns to this chat and new messages arrived while away.
  const newDividerRef = useRef<HTMLDivElement | null>(null);
  const didJumpToNewDividerRef = useRef(false);
  /** Divider index for the in-flight turn (armed at send/regenerate). */
  const turnDividerAtRef = useRef(-1);
  // Latest transcript, so the unmount cleanup can persist how many messages the
  // user had seen when they navigated away.
  const messagesRef = useRef<ChatMsg[]>([]);
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
    setError("");
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
    if (errorText) setError(errorText);
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
    if (sending || resetting || uploadingAttachments) return;
    void (async () => {
      const folder = resolveDroppedFolderPath(event.dataTransfer);
      if (folder?.hasFolder) {
        const blocked = await getNativeDirectoryBlockedMessage(t, "pick");
        if (blocked) {
          setError(blocked);
          return;
        }
        if (folder.absolutePath) {
          appendFolderPathToInput(folder.absolutePath);
          setError("");
        } else {
          setError(t("chat.attachments.folderAbsolutePathUnavailable"));
        }
        return;
      }
      addPendingFiles(Array.from(event.dataTransfer.files ?? []));
    })();
  }, [
    addPendingFiles,
    appendFolderPathToInput,
    resetting,
    sending,
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
    if (!workdir.trim()) throw new Error(t("hermes.workdirNeeded"));
    setUploadingAttachments(true);
    try {
      const out: ChatAttachmentMeta[] = [];
      for (const file of pendingFilesRef.current) {
        const uploaded = await api.uploadHermesChatAttachment(agentId, workdir, file);
        out.push(uploaded.attachment);
      }
      return out;
    } finally {
      setUploadingAttachments(false);
    }
  }, [agentId, workdir, t]);

  // localStorage scope for the transcript cache. Namespaced under `hermes:` so
  // it can't collide with an OpenClaw agent's cache for the same id.
  const chatScope = `hermes:${agentId}`;

  useEffect(() => {
    didJumpToNewDividerRef.current = false;
  }, [chatScope]);

  useEffect(() => {
    const cached = HERMES_PENDING_FILES_CACHE.get(agentId);
    setPendingFiles(cached ? [...cached] : []);
    setDraggingFiles(false);
  }, [agentId, chatScope]);

  useEffect(() => {
    if (pendingFiles.length > 0) {
      HERMES_PENDING_FILES_CACHE.set(agentId, pendingFiles);
    } else {
      HERMES_PENDING_FILES_CACHE.delete(agentId);
    }
  }, [agentId, pendingFiles]);

  useEffect(() => {
    // How many messages the user had already seen before this visit — read once,
    // up front, so the persist-on-change effect can't clobber it first.
    const seenAtEntry = loadLastSeenCount(chatScope);
    void (async () => {
      // Show the cache immediately (it may hold an in-flight partial), then
      // reconcile against server history once it loads.
      const cached: ChatMsg[] = loadChatHistory(chatScope).map((m) => ({
        role: m.role,
        content: m.content,
        attachments: m.attachments ?? [],
        ts: m.ts,
      }));
      if (cached.length > 0) setMessages(cached);
      try {
        const [detail, hist, teamList] = await Promise.all([
          api.getHermesAgent(agentId),
          api.getHermesAgentChatHistory(agentId),
          api.listOpenclawTeams(),
        ]);
        setName(detail.name);
        setTeamName(detail.teamName);
        setTeamId(detail.teamId);
        setTeams(teamList.items);
        setProfileRoot(detail.profileRoot);
        // Server is authoritative: a tab switch can leave the cache holding a
        // stale empty assistant bubble while the real answer sits on the server.
        const server: ChatMsg[] = hist.messages.map((m: ChatHistoryMessage) => ({
          role: m.role,
          content:
            m.role === "assistant"
              ? normalizeAssistantContent(m.content)
              : m.content,
          attachments: m.attachments ?? [],
          ts: m.ts,
        }));
        // Prefer the server-recorded timestamp; only fall back to the local cache
        // (matched by position + content) for rows the server predates.
        const merged = reconcileTranscript(cached, server).map((m, i) => {
          if (typeof m.ts === "number") return m;
          const c = cached[i];
          return c && c.role === m.role && c.content === m.content && c.ts
            ? { ...m, ts: c.ts }
            : m;
        });
        setMessages(merged);
        if (merged.length > 0) saveChatHistory(chatScope, merged);
        // Draw the "new messages" divider above anything that arrived while the
        // user was away (settled count grew beyond what they'd last seen).
        const dividerAt = reentryDividerIndex(merged, seenAtEntry);
        setNewDividerAt(dividerAt);
        if (dividerAt >= 0) {
          didJumpToNewDividerRef.current = false;
          suppressNextStickyScroll();
        }
        try {
          const st = await api.getHermesChatStatus(agentId);
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
          if (nextMessages.length > 0) saveChatHistory(chatScope, nextMessages);
        } catch {
          const last = merged[merged.length - 1];
          if (last && last.role === "user") {
            setRecovering(true);
            const nextMessages = [
              ...merged,
              { role: "assistant" as const, content: "", ts: Date.now() },
            ];
            setMessages(nextMessages);
            saveChatHistory(chatScope, nextMessages);
          }
        }
      } catch (e) {
        setError(errText(e));
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId]);

  // Persist the transcript (incl. partial streaming text) on every change so a
  // refresh / tab close+reopen doesn't lose an in-flight assistant turn.
  useEffect(() => {
    if (messages.length === 0) return;
    saveChatHistory(chatScope, messages);
  }, [chatScope, messages]);

  // On leaving the page (or switching agents), remember how many messages the
  // user had seen so the next visit can mark what's new.
  useEffect(() => {
    return () => saveLastSeenCount(chatScope, settledCount(messagesRef.current));
  }, [chatScope]);

  const revealCompletedTurn = useCallback(() => {
    const at = turnDividerAtRef.current;
    if (at < 0) return;
    suppressNextStickyScroll();
    setNewDividerAt(at);
    didJumpToNewDividerRef.current = false;
  }, [suppressNextStickyScroll]);

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
  }, [newDividerAt, messages.length, recovering, sending, handleScroll, scrollRef]);

  useEffect(() => {
    if (newDividerAt >= 0 && !didJumpToNewDividerRef.current) return;
    stickIfAtBottom();
  }, [messages, recovering, stickIfAtBottom, newDividerAt]);

  // Reconnect to a turn whose SSE stream was detached (tab switch / refresh):
  // poll GET /chat/status and keep going *as long as the server says running*
  // (no fixed cap — the math case can run many minutes), surfacing the live step
  // trail. On done/error/idle, adopt the final answer (from the job, falling
  // back to persisted history) and stop.
  useEffect(() => {
    if (!recovering) return;
    let cancelled = false;
    let timer: number | undefined;
    const adoptFinalFromHistory = async () => {
      try {
        const hist = await api.getHermesAgentChatHistory(agentId);
        if (cancelled) return;
        const server: ChatMsg[] = hist.messages.map((m: ChatHistoryMessage) => ({
          role: m.role,
          content:
            m.role === "assistant"
              ? normalizeAssistantContent(m.content)
              : m.content,
          attachments: m.attachments ?? [],
          ts: m.ts,
        }));
        const last = server[server.length - 1];
        if (last && last.role === "assistant" && last.content.trim() !== "") {
          setMessages(server);
          saveChatHistory(chatScope, server);
          revealCompletedTurn();
        }
      } catch {
        /* ignore — leave the transcript as-is */
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
        saveChatHistory(chatScope, next);
        return next;
      });
      revealCompletedTurn();
    };
    const tick = async () => {
      try {
        const st = await api.getHermesChatStatus(agentId);
        if (cancelled) return;
        if (st.status === "running") {
          // Keep recovering — PendingReply shows in the assistant bubble.
        } else {
          if (st.status === "done" && st.final.trim()) {
            adoptFinal(st.final);
          } else {
            await adoptFinalFromHistory();
            if (st.status === "error" && st.error && st.error !== "cancelled") {
              setError(t("hermes.chatError", { message: st.error }));
            }
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
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recovering, agentId, chatScope]);

  const pickWorkdir = async () => {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    try {
      const out = await api.pickDirectory({
        title: t("hermes.workdir"),
        initialPath: (workdirEditing ? workdirDraft : workdir) || undefined,
      });
      if (out.path) {
        if (workdirEditing) setWorkdirDraft(out.path);
        else updateWorkdir(out.path);
      }
    } catch (e) {
      if (workdirEditing) setWorkdirError(errText(e));
      else setError(errText(e));
    }
  };

  const startEditWorkdir = () => {
    setWorkdirDraft(workdir);
    setWorkdirError("");
    setWorkdirEditing(true);
  };

  const cancelEditWorkdir = () => {
    setWorkdirEditing(false);
    setWorkdirDraft("");
    setWorkdirError("");
  };

  const saveWorkdir = async () => {
    const raw = workdirDraft.trim();
    if (!raw || workdirSaving) return;
    setWorkdirSaving(true);
    setWorkdirError("");
    try {
      const out = await api.validateDirectory({ path: raw });
      updateWorkdir(out.path);
      setWorkdirEditing(false);
      setWorkdirDraft("");
    } catch (e) {
      setWorkdirError(workdirValidationError(e, t));
    } finally {
      setWorkdirSaving(false);
    }
  };

  const openProfile = async () => {
    if (await alertIfNativeDirectoryBlocked(t, "open")) return;
    setOpening(true);
    try {
      await api.openDirectory({ path: profileRoot });
    } catch (e) {
      void alert(t("hermes.openFailed", { message: errText(e) }));
    } finally {
      setOpening(false);
    }
  };

  const openTeamEdit = () => {
    setTeamEditChoice(teamId || "");
    setTeamEditNewTeamName("");
    setTeamEditError("");
    setTeamEditOpen(true);
  };

  const saveTeam = async () => {
    if (teamEditSaving) return;
    if (teamEditChoice === CREATE_TEAM_SENTINEL && !teamEditNewTeamName.trim()) {
      setTeamEditError(t("chat.teamEdit.newTeamRequired"));
      return;
    }
    setTeamEditSaving(true);
    setTeamEditError("");
    try {
      const resolved = await resolveTeamId(teamEditChoice, teamEditNewTeamName);
      const updated = await api.patchHermesAgent(agentId, { teamId: resolved ?? "" });
      setTeamName(updated.teamName);
      setTeamId(updated.teamId);
      const teamList = await api.listOpenclawTeams();
      setTeams(teamList.items);
      setTeamEditOpen(false);
    } catch (e) {
      setTeamEditError(errText(e));
    } finally {
      setTeamEditSaving(false);
    }
  };

  // Core streaming turn. ``appendUser`` is false on regenerate (the user message
  // is already in the transcript — we only replace the assistant reply).
  const runTurn = async (
    message: string,
    opts: { appendUser: boolean; attachments?: ChatAttachmentMeta[] },
  ) => {
    if (!workdir) {
      setError(t("hermes.workdirNeeded"));
      return;
    }
    setError("");
    setRecovering(false);
    setNewDividerAt(-1);
    didJumpToNewDividerRef.current = true;
    turnDividerAtRef.current = turnDividerIndex(messagesRef.current, opts.appendUser);
    setSending(true);
    const turnAttachments = opts.attachments ?? [];
    setMessages((prev) => {
      const base = opts.appendUser
        ? [
            ...prev,
            {
              role: "user" as const,
              content: message,
              attachments: turnAttachments,
              ts: Date.now(),
            },
          ]
        : prev;
      return [...base, { role: "assistant" as const, content: "" }];
    });
    scrollToBottom(); // the user just acted — jump to the latest
    const controller = new AbortController();
    abortRef.current = controller;
    let streamErr = "";
    let aborted = false;
    try {
      const res = await api.chatWithHermesAgent(
        agentId,
        { message, workdir, attachments: turnAttachments },
        { signal: controller.signal },
      );
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx);
          buf = buf.slice(idx + 2);
          const line = block.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          try {
            const obj = JSON.parse(data) as {
              delta?: string;
              error?: string;
            };
            if (obj.error) {
              streamErr = obj.error;
            } else if (typeof obj.delta === "string") {
              setMessages((prev) => {
                const next = [...prev];
                next[next.length - 1] = {
                  role: "assistant",
                  content: next[next.length - 1].content + obj.delta,
                  ts: Date.now(),
                };
                return next;
              });
            }
          } catch {
            /* ignore non-JSON keepalive lines */
          }
        }
      }
      if (streamErr) setError(t("hermes.chatError", { message: streamErr }));
    } catch (e) {
      if (controller.signal.aborted) {
        aborted = true;
      } else {
        setError(t("hermes.chatError", { message: errText(e) }));
      }
    } finally {
      abortRef.current = null;
      setSending(false);
      if (aborted) {
        // Mark the (still-empty) pending reply as stopped; keep any partial text.
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant" && !last.content) {
            next[next.length - 1] = {
              role: "assistant",
              content: t("chat.stopped"),
              ts: Date.now(),
            };
          }
          return next;
        });
      } else {
        try {
          const hist = await api.getHermesAgentChatHistory(agentId);
          const server: ChatMsg[] = hist.messages.map((m: ChatHistoryMessage) => ({
            role: m.role,
            content:
              m.role === "assistant"
                ? normalizeAssistantContent(m.content)
                : m.content,
            attachments: m.attachments ?? [],
            ts: m.ts,
          }));
          const last = server[server.length - 1];
          if (last && last.role === "assistant") {
            setMessages((prev) => {
              const merged = reconcileTranscript(prev, server);
              saveChatHistory(chatScope, merged);
              return merged;
            });
          }
          if (!streamErr) revealCompletedTurn();
        } catch {
          /* keep streamed partial */
          if (!streamErr) revealCompletedTurn();
        }
      }
    }
  };

  const send = async () => {
    if (sending || recovering || resetting || uploadingAttachments) return;
    const message = input.trim();
    if (!message && pendingFiles.length === 0) return;
    let uploadedAttachments: ChatAttachmentMeta[] = [];
    if (pendingFiles.length > 0) {
      try {
        uploadedAttachments = await uploadPendingFiles();
      } catch (e) {
        const msg = e instanceof ApiError ? `${e.code}: ${e.message}` : errText(e);
        setError(t("chat.attachments.uploadFailed", { message: msg }));
        return;
      }
    }
    setInput("");
    setPendingFiles([]);
    await runTurn(message, { appendUser: true, attachments: uploadedAttachments });
  };

  const stop = async () => {
    abortRef.current?.abort();
    try {
      await api.stopHermesAgentChat(agentId);
    } catch {
      /* best-effort — the client stream is already cut */
    }
    setRecovering(false);
  };

  const regenerate = async () => {
    if (sending || recovering || resetting || uploadingAttachments) return;
    const lastUser = [...messages].reverse().find((m) => m.role === "user");
    if (!lastUser) return;
    // Drop the trailing assistant reply; runTurn appends a fresh one.
    setMessages((prev) => {
      const next = [...prev];
      if (next.length && next[next.length - 1].role === "assistant") next.pop();
      return next;
    });
    await runTurn(lastUser.content, {
      appendUser: false,
      attachments: lastUser.attachments ?? [],
    });
  };

  const reset = async () => {
    if (sending || resetting || uploadingAttachments) return;
    setResetting(true);
    setError("");
    try {
      await api.resetHermesAgentChat(agentId);
      clearChatHistory(chatScope);
      setRecovering(false);
      setMessages([]);
      setNewDividerAt(-1);
    } catch (e) {
      setError(errText(e));
    } finally {
      setResetting(false);
    }
  };

  const showGatewayNotice = useCallback((kind: GatewayNotice["kind"], text: string) => {
    setGatewayNotice({ kind, text });
    if (gatewayNoticeTimerRef.current !== null) {
      window.clearTimeout(gatewayNoticeTimerRef.current);
    }
    gatewayNoticeTimerRef.current = window.setTimeout(() => {
      setGatewayNotice(null);
      gatewayNoticeTimerRef.current = null;
    }, 2000);
  }, []);

  useEffect(() => {
    return () => {
      if (gatewayNoticeTimerRef.current !== null) {
        window.clearTimeout(gatewayNoticeTimerRef.current);
      }
    };
  }, []);

  const startGateway = async () => {
    if (gatewayStartLockRef.current) return;
    gatewayStartLockRef.current = true;
    setGatewayBusy(true);
    try {
      const out = await api.startHermesAgentGateway(agentId);
      showGatewayNotice("success", out.message || t("hermes.gateway.started"));
    } catch (e) {
      showGatewayNotice(
        "error",
        t("hermes.gateway.startFailed", { message: errText(e) }),
      );
    } finally {
      gatewayStartLockRef.current = false;
      setGatewayBusy(false);
    }
  };

  return (
    <div className="flex h-[calc(100vh-6rem)] min-h-0 flex-col gap-5 overflow-hidden">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <h1 className="flex items-center gap-3 text-xl font-semibold text-ink-900">
            <AgentCardAvatar size="header" platform="hermes" />
            <span className="truncate">{name}</span>
            <button
              type="button"
              className="btn-outline inline-flex h-8 items-center justify-center gap-1.5 px-2.5 py-0 text-xs font-medium"
              onClick={() => void openProfile()}
              disabled={opening || !profileRoot}
              title={t("hermes.myProfile")}
            >
              <DesktopIcon className="h-3.5 w-3.5" />
              {t("hermes.myProfile")}
            </button>
          </h1>
          <div className="mt-2 inline-flex items-center gap-2 text-xs text-ink-600">
            <span>{t("chat.teamLabel")}:</span>
            <span className="rounded-full border border-ink-200 bg-ink-50 px-2 py-0.5">
              {teamName || t("chat.ungroupedTeam")}
            </span>
            <button
              type="button"
              className="text-ink-500 underline-offset-2 hover:text-ink-800 hover:underline"
              onClick={openTeamEdit}
            >
              {t("chat.teamEdit.action")}
            </button>
          </div>
        </div>
        <div className="inline-flex shrink-0 items-center gap-2">
          <div className="relative">
            <button
              type="button"
              className="inline-flex h-10 items-center justify-center gap-2 rounded-full
                       bg-gradient-to-r from-amber-500 to-orange-500
                       px-4 py-0 text-sm font-semibold text-white
                       shadow-[0_0_24px_-6px_rgba(245,158,11,0.95)]
                       ring-1 ring-amber-300/70
                       hover:from-amber-600 hover:to-orange-600
                       transition-all disabled:cursor-not-allowed disabled:opacity-70"
              disabled={gatewayBusy}
              onClick={() => void startGateway()}
            >
              {gatewayBusy ? t("hermes.gateway.starting") : t("hermes.gateway.startButton")}
            </button>
            {gatewayNotice && (
              <span
                className={cn(
                  "pointer-events-none absolute right-0 top-[calc(100%+0.35rem)] z-20 whitespace-nowrap rounded-full border px-2.5 py-1 text-xs shadow-card",
                  gatewayNotice.kind === "success"
                    ? "border-emerald-200 bg-emerald-50 text-emerald-700"
                    : "border-rose-200 bg-rose-50 text-rose-700",
                )}
              >
                {gatewayNotice.text}
              </span>
            )}
          </div>
          <button
            type="button"
            className="btn-outline inline-flex h-10 items-center justify-center px-4 py-0 text-sm font-medium"
            onClick={() => navigate("/hermes")}
          >
            {t("common.back")}
          </button>
          <AgentPageToolbar
            primary={
              <button
                type="button"
                className="inline-flex h-10 items-center justify-center gap-2 rounded-full
                         bg-gradient-to-r from-brand-500 via-brand-400 to-orange-500
                         px-5 py-0 text-sm font-semibold tracking-wide text-white
                         shadow-[0_0_24px_-4px_rgb(var(--brand-400))]
                         ring-1 ring-brand-300/60
                         hover:from-brand-600 hover:to-orange-600
                         hover:shadow-[0_0_32px_-2px_rgb(var(--brand-400))]
                         hover:-translate-y-0.5
                         transition-all"
                disabled={dashboardBusy}
                onClick={() => void openHermesDashboard(t, () => void alert(t("hermes.dashboardRemoteUnavailable")), (msg) => void alert(msg), setDashboardBusy, agentId)}
              >
                <ExternalLinkIcon className="h-4 w-4" />
                {dashboardBusy ? t("hermes.openingDashboard") : t("hermes.toHermes")}
              </button>
            }
          >
            <AgentToolbarIconButton
              label={t("hermes.settings")}
              icon={<SettingsIcon className="h-4 w-4" />}
              onClick={() => setShowSettings(true)}
            />
          </AgentPageToolbar>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden p-0">
        <div className="relative flex min-h-0 flex-1 flex-col">
          <div
            ref={scrollRef}
            onScroll={handleScroll}
            className="min-h-[280px] flex-1 space-y-4 overflow-auto bg-ink-50/40 px-5 py-4"
          >
            {displayMessages.map((m, i, list) => (
              <Fragment key={i}>
                {i === newDividerAt && (
                  <div ref={newDividerRef}>
                    <NewMessagesDivider label={t("chat.newMessages")} />
                  </div>
                )}
                <ChatBubble
                  msg={m}
                  pending={
                    (sending || recovering) &&
                    i === list.length - 1 &&
                    m.role === "assistant" &&
                    !m.content
                  }
                  noTextReply={t("chat.noTextReply")}
                />
              </Fragment>
            ))}
            {!sending &&
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
          onSubmit={(e) => {
            e.preventDefault();
            if (!sending && !resetting && !uploadingAttachments) void send();
          }}
          className="space-y-2 border-t border-ink-100 p-3"
        >
          <div className="space-y-1">
            <div className="flex items-center gap-2 text-xs text-ink-500">
              <span className="shrink-0 font-medium text-ink-600">{t("hermes.workdir")}</span>
              <input
                className={cn(
                  "input flex-1 font-mono text-xs",
                  !workdirEditing &&
                    "cursor-default bg-ink-50/80 focus:border-ink-200 focus:ring-0",
                )}
                value={workdirEditing ? workdirDraft : workdir}
                placeholder={t("hermes.workdirNeeded")}
                readOnly={!workdirEditing}
                tabIndex={workdirEditing ? 0 : -1}
                aria-readonly={!workdirEditing}
                disabled={workdirSaving}
                onMouseDown={(e) => {
                  if (!workdirEditing) e.preventDefault();
                }}
                onFocus={(e) => {
                  if (!workdirEditing) e.currentTarget.blur();
                }}
                onChange={(e) => {
                  if (!workdirEditing) return;
                  setWorkdirDraft(e.target.value);
                  setWorkdirError("");
                }}
              />
              {workdirEditing ? (
                <>
                  <button
                    type="button"
                    className="btn-outline shrink-0 !px-2 !py-1 text-xs"
                    onClick={() => void pickWorkdir()}
                    disabled={workdirSaving}
                  >
                    {t("hermes.chooseWorkdir")}
                  </button>
                  <button
                    type="button"
                    className="btn-primary shrink-0 !px-2 !py-1 text-xs"
                    onClick={() => void saveWorkdir()}
                    disabled={workdirSaving || !workdirDraft.trim()}
                  >
                    {workdirSaving ? t("hermes.workdirSaving") : t("hermes.workdirSave")}
                  </button>
                  <button
                    type="button"
                    className="btn-outline shrink-0 !px-2 !py-1 text-xs"
                    onClick={cancelEditWorkdir}
                    disabled={workdirSaving}
                  >
                    {t("common.cancel")}
                  </button>
                </>
              ) : (
                <button
                  type="button"
                  className="inline-flex h-8 w-8 shrink-0 items-center justify-center rounded-lg border border-ink-200 text-ink-500 hover:bg-ink-50 hover:text-ink-800"
                  title={t("hermes.workdirEdit")}
                  aria-label={t("hermes.workdirEdit")}
                  onClick={startEditWorkdir}
                >
                  <EditIcon className="h-4 w-4" />
                </button>
              )}
            </div>
            {workdirEditing && workdirError && (
              <p className="text-xs text-rose-600">{workdirError}</p>
            )}
          </div>
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
                className="btn-outline shrink-0 !px-2 !py-1 text-xs"
                onClick={() => fileInputRef.current?.click()}
                disabled={sending || resetting || uploadingAttachments}
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
                      disabled={sending || resetting || uploadingAttachments}
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
                    if (!sending && !recovering && !resetting && !uploadingAttachments) {
                      (e.currentTarget.form as HTMLFormElement).requestSubmit();
                    }
                  });
                }}
                disabled={sending || recovering || resetting || uploadingAttachments}
              />
              <button
                type="button"
                className="btn-outline"
                disabled={sending || recovering || resetting || uploadingAttachments}
                onClick={() => void reset()}
              >
                {resetting ? t("chat.resetting") : t("chat.reset")}
              </button>
              {sending || recovering ? (
                <button
                  type="button"
                  className="btn-outline border-rose-300 text-rose-600 hover:bg-rose-50"
                  onClick={() => void stop()}
                >
                  {t("chat.stop")}
                </button>
              ) : (
                <button
                  type="submit"
                  className="btn-primary"
                  disabled={resetting || uploadingAttachments || (!input.trim() && pendingFiles.length === 0)}
                >
                  {t("chat.send")}
                </button>
              )}
            </div>
          </div>
        </form>
      </Card>

      {showSettings && (
        <SettingsModal agentId={agentId} onClose={() => setShowSettings(false)} />
      )}

      <Modal
        open={teamEditOpen}
        onClose={() => {
          if (teamEditSaving) return;
          setTeamEditOpen(false);
        }}
        title={t("chat.teamEdit.title")}
      >
        <div className="space-y-3">
          {teamEditError && <ErrorBox>{teamEditError}</ErrorBox>}
          <div>
            <label className="label">{t("chat.teamEdit.label")}</label>
            <select
              className="select"
              value={teamEditChoice}
              onChange={(e) => setTeamEditChoice(e.target.value)}
              disabled={teamEditSaving}
            >
              <option value="">{t("chat.teamEdit.noneOptional")}</option>
              {teams.map((tm) => (
                <option key={tm.id} value={tm.id}>
                  {tm.name}
                </option>
              ))}
              <option value={CREATE_TEAM_SENTINEL}>{t("chat.teamEdit.createNew")}</option>
            </select>
          </div>
          {teamEditChoice === CREATE_TEAM_SENTINEL && (
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
          )}
          <div className="flex justify-end gap-2 pt-2">
            <button
              type="button"
              className="btn-outline"
              onClick={() => setTeamEditOpen(false)}
              disabled={teamEditSaving}
            >
              {t("common.cancel")}
            </button>
            <button
              type="button"
              className="btn-primary"
              onClick={() => void saveTeam()}
              disabled={teamEditSaving}
            >
              {teamEditSaving ? t("chat.teamEdit.saving") : t("common.save")}
            </button>
          </div>
        </div>
      </Modal>
    </div>
  );
}

// ──────────────────────────────────────────────────────────────────────
// Settings modal
// ──────────────────────────────────────────────────────────────────────

type SettingsTab = "soul" | "model" | "gateway" | "mcp" | "skills" | "cron";

function SettingsModal({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<SettingsTab>("soul");

  return (
    <Modal open onClose={onClose} title={t("hermes.settingsModal.title")} width="max-w-3xl">
      {/* Segmented tab control — mirrors the OpenClaw settings modal so the
          selected tab reads in both themes (a frosted surface chip with brand
          text + ring), instead of the old white-on-near-white in dark mode. */}
      <div className="mb-4 rounded-xl border border-ink-100 bg-ink-50/60 p-1.5">
        <div className="grid grid-cols-3 gap-1 sm:grid-cols-6">
          {(["soul", "model", "gateway", "mcp", "skills", "cron"] as SettingsTab[]).map((k) => (
            <button
              key={k}
              type="button"
              className={cn(
                "rounded-lg px-3 py-2 text-sm font-medium transition",
                tab === k
                  ? "bg-surface text-brand-700 shadow-sm ring-1 ring-brand-200"
                  : "text-ink-600 hover:text-ink-900",
              )}
              onClick={() => setTab(k)}
            >
              {t(`hermes.settingsModal.tabs.${k}`)}
            </button>
          ))}
        </div>
      </div>
      {tab === "soul" && <SoulTab agentId={agentId} />}
      {tab === "model" && <ModelTab agentId={agentId} />}
      {tab === "gateway" && <GatewayTab agentId={agentId} />}
      {tab === "mcp" && <McpTab agentId={agentId} />}
      {tab === "skills" && <SkillsTab agentId={agentId} />}
      {tab === "cron" && <CronTab agentId={agentId} />}
    </Modal>
  );
}

function SoulTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .getHermesSoul(agentId)
      .then((r) => setContent(r.content))
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  }, [agentId]);

  const save = async () => {
    setBusy(true);
    setSaved(false);
    setError("");
    try {
      await api.putHermesSoul(agentId, content);
      setSaved(true);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-3">
      {error && <ErrorBox>{error}</ErrorBox>}
      <p className="text-xs text-ink-400">{t("hermes.settingsModal.soulHint")}</p>
      <textarea
        className="h-72 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs"
        value={content}
        onChange={(e) => {
          setContent(e.target.value);
          setSaved(false);
        }}
      />
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void save()}
          disabled={busy}
        >
          {t("hermes.settingsModal.save")}
        </button>
        {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
      </div>
    </div>
  );
}

function ModelTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [model, setModel] = useState<HermesModelSetting>({ default: "", provider: "", baseUrl: "" });
  const [sources, setSources] = useState<HermesAgentSummary[]>([]);
  const [inheritFrom, setInheritFrom] = useState("default");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    Promise.all([api.getHermesModel(agentId), api.listHermesAgents()])
      .then(([m, a]) => {
        setModel(m);
        setSources(a.items);
      })
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  }, [agentId]);

  const isManualImport = inheritFrom === "__manual__";

  const applyModelFromSource = async () => {
    if (!isManualImport && !inheritFrom.trim()) return;
    setBusy(true);
    setSaved(false);
    setError("");
    try {
      if (isManualImport) {
        setModel(await api.putHermesModel(agentId, model));
      } else {
        setModel(await api.importHermesModel(agentId, { inheritFrom }));
      }
      setSaved(true);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}
      <div className="space-y-2 rounded border border-ink-100 bg-ink-50/40 p-3">
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.model.importFromLabel")}</span>
          <select
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={inheritFrom}
            onChange={(e) => {
              setInheritFrom(e.target.value);
              setSaved(false);
            }}
            disabled={busy}
          >
            <option value="default">{t("hermes.settingsModal.model.importDefault")}</option>
            {sources.map((a) => (
              <option key={a.id} value={a.id}>
                {`${a.name || a.id} (${a.id})`}
              </option>
            ))}
            <option value="__manual__">{t("hermes.settingsModal.model.importManual")}</option>
          </select>
        </label>
        {isManualImport ? (
          <div className="space-y-2 border-t border-ink-200 pt-3">
            <p className="text-xs text-ink-400">{t("hermes.settingsModal.model.hint")}</p>
            <label className="block text-sm">
              <span className="text-ink-600">{t("hermes.settingsModal.model.modelLabel")}</span>
              <input
                className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
                value={model.default}
                placeholder={t("hermes.settingsModal.model.modelPlaceholder")}
                onChange={(e) => {
                  setModel({ ...model, default: e.target.value });
                  setSaved(false);
                }}
              />
            </label>
            <label className="block text-sm">
              <span className="text-ink-600">{t("hermes.settingsModal.model.providerLabel")}</span>
              <input
                className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
                value={model.provider}
                placeholder={t("hermes.settingsModal.model.providerPlaceholder")}
                onChange={(e) => {
                  setModel({ ...model, provider: e.target.value });
                  setSaved(false);
                }}
              />
            </label>
            <label className="block text-sm">
              <span className="text-ink-600">{t("hermes.settingsModal.model.baseUrlLabel")}</span>
              <input
                className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
                value={model.baseUrl}
                placeholder={t("hermes.settingsModal.model.baseUrlPlaceholder")}
                onChange={(e) => {
                  setModel({ ...model, baseUrl: e.target.value });
                  setSaved(false);
                }}
              />
            </label>
            <div className="flex items-center gap-2">
              <button
                type="button"
                className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                onClick={() => void applyModelFromSource()}
                disabled={busy}
              >
                {t("hermes.settingsModal.model.importButton")}
              </button>
              {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
            </div>
          </div>
        ) : (
          <div className="flex items-center gap-2">
            <button
              type="button"
              className="rounded border border-ink-200 px-3 py-2 text-sm hover:bg-ink-50 disabled:opacity-50"
              onClick={() => void applyModelFromSource()}
              disabled={busy}
            >
              {t("hermes.settingsModal.model.importButton")}
            </button>
            {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
          </div>
        )}
      </div>
    </div>
  );
}

function GatewayTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const [cwd, setCwd] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [picking, setPicking] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .getHermesGateway(agentId)
      .then((r) => setCwd(r.cwd))
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  }, [agentId]);

  const pickWorkdir = async () => {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    setPicking(true);
    try {
      const out = await api.pickDirectory({
        title: t("hermes.settingsModal.gateway.workdirLabel"),
        initialPath: cwd || undefined,
      });
      if (out.path) {
        setCwd(out.path);
        setSaved(false);
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      setPicking(false);
    }
  };

  const save = async () => {
    if (!cwd.trim() || busy) return;
    setBusy(true);
    setSaved(false);
    setError("");
    try {
      const r = await api.putHermesGateway(agentId, { cwd: cwd.trim() });
      setCwd(r.cwd);
      setSaved(true);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-3">
      {error && <ErrorBox>{error}</ErrorBox>}
      <label className="block text-sm">
        <span className="font-medium text-ink-700">{t("hermes.settingsModal.gateway.workdirLabel")}</span>
        <p className="mt-1 text-xs text-ink-400">{t("hermes.settingsModal.gateway.hint")}</p>
        <div className="mt-2 flex gap-2">
          <input
            className="input flex-1 font-mono text-xs"
            value={cwd}
            placeholder={t("hermes.settingsModal.gateway.workdirPlaceholder")}
            onChange={(e) => {
              setCwd(e.target.value);
              setSaved(false);
            }}
            disabled={busy}
          />
          <button
            type="button"
            className="btn-outline whitespace-nowrap"
            onClick={() => void pickWorkdir()}
            disabled={busy || picking}
          >
            {t("hermes.settingsModal.gateway.pickWorkdir")}
          </button>
        </div>
      </label>
      <div className="flex items-center gap-2">
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void save()}
          disabled={busy || !cwd.trim()}
        >
          {busy ? t("hermes.settingsModal.gateway.saving") : t("hermes.settingsModal.gateway.save")}
        </button>
        {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
      </div>
    </div>
  );
}

type McpTransport = "http_sse" | "sse" | "local";

function McpTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<HermesMcpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [removingName, setRemovingName] = useState<string | null>(null);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<McpTransport>("http_sse");
  const [url, setUrl] = useState("");
  const [command, setCommand] = useState("");
  const [argsText, setArgsText] = useState("");
  const [environment, setEnvironment] = useState("");
  // null = creating a new server; a name = editing that existing server.
  const [editingName, setEditingName] = useState<string | null>(null);
  const isEditing = editingName !== null;
  const isLocal = transport === "local";
  const args = argsText.split("\n").map((line) => line.trim()).filter(Boolean);
  const canSave = Boolean(name.trim() && (isLocal ? command.trim() : url.trim()));

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setServers(await api.getHermesMcpServers(agentId));
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const resetForm = () => {
    setEditingName(null);
    setName("");
    setTransport("http_sse");
    setUrl("");
    setCommand("");
    setArgsText("");
    setEnvironment("");
  };

  const startEdit = (server: HermesMcpServer) => {
    setError("");
    setEditingName(server.name);
    setName(server.name);
    setTransport(server.transport);
    setUrl(server.url);
    setCommand(server.command);
    setArgsText(server.args.join("\n"));
    setEnvironment(""); // blank = keep existing env (values are masked)
  };

  const save = async () => {
    if (!canSave) return;
    setSaving(true);
    setError("");
    try {
      await api.putHermesMcpServer(agentId, {
        name: name.trim(),
        transport,
        url: isLocal ? "" : url.trim(),
        command: isLocal ? command.trim() : "",
        args: isLocal ? args : [],
        // On edit a blank env means "keep existing" → send null (preserve).
        environment: isEditing ? (environment.trim() ? environment : null) : environment,
      });
      resetForm();
      setServers(await api.getHermesMcpServers(agentId));
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (serverName: string) => {
    if (saving || removingName) return;
    setRemovingName(serverName);
    setError("");
    try {
      await api.deleteHermesMcpServer(agentId, serverName);
      if (editingName === serverName) resetForm();
      setServers(await api.getHermesMcpServers(agentId));
    } catch (e) {
      setError(errText(e));
    } finally {
      setRemovingName(null);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}
      <p className="text-xs text-ink-400">{t("hermes.settingsModal.mcp.hint")}</p>
      {servers.length === 0 ? (
        <p className="text-sm text-ink-500">{t("hermes.settingsModal.mcp.empty")}</p>
      ) : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {servers.map((server) => (
            <li key={server.name} className="flex items-center justify-between px-3 py-2 text-sm">
              <div className="min-w-0">
                <div className="flex items-center gap-2">
                  <code>{server.name}</code>
                  <span className="rounded border border-ink-200 bg-ink-50 px-1.5 py-0.5 text-[11px] text-ink-500">
                    {server.transport === "local"
                      ? t("hermes.settingsModal.mcp.transportLocal")
                      : server.transport === "sse"
                        ? t("hermes.settingsModal.mcp.transportSse")
                        : t("hermes.settingsModal.mcp.transportHttp")}
                  </span>
                </div>
                <p className="truncate font-mono text-xs text-ink-500">
                  {server.transport === "local"
                    ? [server.command, ...server.args].filter(Boolean).join(" ")
                    : server.url}
                </p>
                {server.envKeys.length > 0 && (
                  <p className="truncate text-xs text-ink-400">
                    {t("hermes.settingsModal.mcp.envKeys", { keys: server.envKeys.join(", ") })}
                  </p>
                )}
              </div>
              <div className="flex shrink-0 items-center gap-3">
                <button
                  type="button"
                  className="text-xs text-ink-500 hover:text-ink-800 disabled:opacity-50"
                  onClick={() => (editingName === server.name ? resetForm() : startEdit(server))}
                  disabled={saving || removingName !== null}
                >
                  {editingName === server.name
                    ? t("common.cancel")
                    : t("hermes.settingsModal.mcp.edit")}
                </button>
                <button
                  type="button"
                  className="text-xs text-rose-500 hover:text-rose-700 disabled:opacity-50"
                  onClick={() => void remove(server.name)}
                  disabled={saving || removingName === server.name}
                >
                  {t("hermes.settingsModal.mcp.remove")}
                </button>
              </div>
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-2 border-t border-ink-100 pt-3">
        <div className="text-sm font-medium text-ink-700">
          {isEditing
            ? t("hermes.settingsModal.mcp.editTitle", { name: editingName ?? "" })
            : t("hermes.settingsModal.mcp.add")}
        </div>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.nameLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm disabled:bg-ink-50 disabled:text-ink-400"
            value={name}
            placeholder={t("hermes.settingsModal.mcp.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
            disabled={isEditing}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.transportLabel")}</span>
          <select
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={transport}
            onChange={(e) => setTransport(e.target.value as McpTransport)}
          >
            <option value="http_sse">{t("hermes.settingsModal.mcp.transportHttp")}</option>
            <option value="sse">{t("hermes.settingsModal.mcp.transportSse")}</option>
            <option value="local">{t("hermes.settingsModal.mcp.transportLocal")}</option>
          </select>
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.urlLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs disabled:bg-ink-50 disabled:text-ink-400"
            value={url}
            placeholder={t("hermes.settingsModal.mcp.urlPlaceholder")}
            onChange={(e) => setUrl(e.target.value)}
            disabled={isLocal}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.commandLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs disabled:bg-ink-50 disabled:text-ink-400"
            value={command}
            placeholder={t("hermes.settingsModal.mcp.commandPlaceholder")}
            onChange={(e) => setCommand(e.target.value)}
            disabled={!isLocal}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.argsLabel")}</span>
          <textarea
            className="mt-1 h-20 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs disabled:bg-ink-50 disabled:text-ink-400"
            value={argsText}
            placeholder={t("hermes.settingsModal.mcp.argsPlaceholder")}
            onChange={(e) => setArgsText(e.target.value)}
            disabled={!isLocal}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.envLabel")}</span>
          <textarea
            className="mt-1 h-24 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs"
            value={environment}
            placeholder={t("hermes.settingsModal.mcp.envPlaceholder")}
            onChange={(e) => setEnvironment(e.target.value)}
          />
          {isEditing && (
            <span className="mt-1 block text-xs text-ink-400">
              {t("hermes.settingsModal.mcp.envKeepHint")}
            </span>
          )}
        </label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => void save()}
            disabled={saving || !canSave}
          >
            {saving
              ? t("common.saving")
              : isEditing
                ? t("common.save")
                : t("hermes.settingsModal.mcp.add")}
          </button>
          {isEditing && (
            <button type="button" className="btn-outline" onClick={resetForm} disabled={saving}>
              {t("common.cancel")}
            </button>
          )}
        </div>
      </div>
    </div>
  );
}

// SKILL.md is `---\nname: …\ndescription: "…"\n---\n\n<body>`; for editing we
// only want the body — the frontmatter is rebuilt from name+description on save.
function stripSkillFrontMatter(md: string): string {
  const m = md.match(/^---\n[\s\S]*?\n---\n+([\s\S]*)$/);
  return (m ? m[1] : md).trimEnd();
}

function SkillsTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<HermesSkillSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [content, setContent] = useState("");
  const [newName, setNewName] = useState("");
  const [newDesc, setNewDesc] = useState("");
  const [newContent, setNewContent] = useState("");
  const [creating, setCreating] = useState(false);
  // Inline edit state (one skill at a time).
  const [editingName, setEditingName] = useState<string | null>(null);
  const [editDesc, setEditDesc] = useState("");
  const [editBody, setEditBody] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [deletingName, setDeletingName] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      setSkills(await api.getHermesSkills(agentId));
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const view = async (nameKey: string) => {
    if (expanded === nameKey) {
      setExpanded(null);
      return;
    }
    setError("");
    try {
      const r = await api.getHermesSkill(agentId, nameKey);
      setContent(r.content ?? "");
      setExpanded(nameKey);
    } catch (e) {
      setError(errText(e));
    }
  };

  const startEdit = async (s: HermesSkillSetting) => {
    setError("");
    setExpanded(null);
    try {
      const r = await api.getHermesSkill(agentId, s.name);
      setEditDesc(r.description ?? s.description ?? "");
      setEditBody(stripSkillFrontMatter(r.content ?? ""));
      setEditingName(s.name);
    } catch (e) {
      setError(errText(e));
    }
  };

  const cancelEdit = () => {
    setEditingName(null);
    setEditSaving(false);
  };

  const saveEdit = async () => {
    if (!editingName || !editBody.trim()) return;
    setEditSaving(true);
    setError("");
    try {
      await api.updateHermesSkill(agentId, editingName, {
        description: editDesc.trim(),
        content: editBody,
      });
      cancelEdit();
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setEditSaving(false);
    }
  };

  const del = async (nameKey: string) => {
    if (creating || editSaving || deletingName) return;
    setDeletingName(nameKey);
    setError("");
    try {
      await api.deleteHermesSkill(agentId, nameKey);
      if (editingName === nameKey) cancelEdit();
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setDeletingName(null);
    }
  };

  const create = async () => {
    if (!newName.trim() || !newContent.trim()) return;
    setCreating(true);
    setError("");
    try {
      await api.createHermesSkill(agentId, {
        name: newName.trim(),
        description: newDesc.trim(),
        content: newContent,
      });
      setNewName("");
      setNewDesc("");
      setNewContent("");
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setCreating(false);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-2">
      {error && <ErrorBox>{error}</ErrorBox>}
      {skills.length === 0 ? (
        <p className="text-sm text-ink-500">{t("hermes.settingsModal.skills.empty")}</p>
      ) : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {skills.map((s) => (
            <li key={s.name} className="px-3 py-2">
              <div className="flex items-center justify-between">
                <div>
                  <code className="text-sm">{s.name}</code>
                  {s.description && (
                    <p className="text-xs text-ink-400 line-clamp-1">{s.description}</p>
                  )}
                </div>
                <div className="flex items-center gap-3">
                  <button
                    type="button"
                    className="text-xs text-ink-500 hover:text-ink-800 disabled:opacity-50"
                    onClick={() => void view(s.name)}
                    disabled={deletingName !== null || editSaving || creating}
                  >
                    {expanded === s.name
                      ? t("hermes.settingsModal.skills.hide")
                      : t("hermes.settingsModal.skills.view")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-ink-500 hover:text-ink-800 disabled:opacity-50"
                    onClick={() => (editingName === s.name ? cancelEdit() : void startEdit(s))}
                    disabled={deletingName !== null || creating}
                  >
                    {editingName === s.name
                      ? t("common.cancel")
                      : t("hermes.settingsModal.skills.edit")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-rose-500 hover:text-rose-700 disabled:opacity-50"
                    onClick={() => void del(s.name)}
                    disabled={deletingName === s.name || editSaving || creating}
                  >
                    {t("hermes.settingsModal.skills.delete")}
                  </button>
                </div>
              </div>
              {expanded === s.name && (
                <pre className="mt-2 max-h-60 overflow-auto rounded bg-ink-50 p-2 text-xs">
                  {content}
                </pre>
              )}
              {editingName === s.name && (
                <div className="mt-3 space-y-2 rounded border border-ink-100 bg-ink-50/40 p-3">
                  <input
                    className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
                    value={editDesc}
                    placeholder={t("hermes.settingsModal.skills.descPlaceholder")}
                    onChange={(e) => setEditDesc(e.target.value)}
                  />
                  <textarea
                    className="w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs"
                    rows={6}
                    value={editBody}
                    placeholder={t("hermes.settingsModal.skills.contentPlaceholder")}
                    onChange={(e) => setEditBody(e.target.value)}
                  />
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                      onClick={() => void saveEdit()}
                      disabled={editSaving || !editBody.trim()}
                    >
                      {editSaving ? t("common.saving") : t("common.save")}
                    </button>
                    <button
                      type="button"
                      className="btn-outline"
                      onClick={cancelEdit}
                      disabled={editSaving}
                    >
                      {t("common.cancel")}
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-2 border-t border-ink-100 pt-3">
        <div className="text-sm font-medium text-ink-700">{t("hermes.settingsModal.skills.create")}</div>
        <input
          className="w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono"
          value={newName}
          placeholder={t("hermes.settingsModal.skills.namePlaceholder")}
          onChange={(e) => setNewName(e.target.value)}
        />
        <input
          className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
          value={newDesc}
          placeholder={t("hermes.settingsModal.skills.descPlaceholder")}
          onChange={(e) => setNewDesc(e.target.value)}
        />
        <textarea
          className="w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs"
          rows={5}
          value={newContent}
          placeholder={t("hermes.settingsModal.skills.contentPlaceholder")}
          onChange={(e) => setNewContent(e.target.value)}
        />
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void create()}
          disabled={creating || !newName.trim() || !newContent.trim()}
        >
          {creating ? t("hermes.create.creating") : t("hermes.settingsModal.skills.add")}
        </button>
      </div>
    </div>
  );
}

function CronTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { alert } = useDialog();
  const [available, setAvailable] = useState(true);
  const [jobs, setJobs] = useState<HermesCronJob[]>([]);
  const [deliveryTargets, setDeliveryTargets] = useState<HermesCronDeliveryTarget[]>([
    { value: "local", label: "local" },
  ]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [schedule, setSchedule] = useState("");
  const [prompt, setPrompt] = useState("");
  const [jobName, setJobName] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [deliver, setDeliver] = useState("local");
  const [pickingWorkdir, setPickingWorkdir] = useState(false);
  // Inline edit state (one job at a time).
  const [editingId, setEditingId] = useState<string | null>(null);
  const [editSchedule, setEditSchedule] = useState("");
  const [editPrompt, setEditPrompt] = useState("");
  const [editName, setEditName] = useState("");
  const [editDeliver, setEditDeliver] = useState("local");
  const [editWorkdir, setEditWorkdir] = useState("");
  const [editSaving, setEditSaving] = useState(false);
  const [pickingEditWorkdir, setPickingEditWorkdir] = useState(false);
  const [adding, setAdding] = useState(false);
  const [actionBusyId, setActionBusyId] = useState<string | null>(null);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.getHermesCron(agentId);
      setAvailable(r.available);
      setJobs(r.items);
      if (r.deliveryTargets.length > 0) {
        setDeliveryTargets(r.deliveryTargets);
      }
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, [agentId]);

  useEffect(() => {
    void reload();
  }, [reload]);

  const pickCronWorkdir = async () => {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    setPickingWorkdir(true);
    try {
      const out = await api.pickDirectory({
        title: t("hermes.settingsModal.cron.workdir"),
        initialPath: workdir || undefined,
      });
      if (out.path) setWorkdir(out.path);
    } catch (e) {
      setError(errText(e));
    } finally {
      setPickingWorkdir(false);
    }
  };

  const add = async () => {
    if (!schedule.trim() || adding) return;
    setAdding(true);
    setError("");
    try {
      await api.createHermesCron(agentId, {
        schedule: schedule.trim(),
        prompt: prompt.trim(),
        name: jobName.trim(),
        workdir: workdir.trim(),
        deliver: deliver.trim() || "local",
      });
      setSchedule("");
      setPrompt("");
      setJobName("");
      setWorkdir("");
      setDeliver("local");
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setAdding(false);
    }
  };

  const act = async (jobId: string, action: "pause" | "resume" | "remove") => {
    if (actionBusyId) return;
    setActionBusyId(jobId);
    setError("");
    try {
      await api.hermesCronAction(agentId, jobId, action);
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setActionBusyId(null);
    }
  };

  const startEdit = (job: HermesCronJob) => {
    setError("");
    setEditingId(job.id);
    setEditSchedule(job.schedule);
    setEditPrompt(job.prompt);
    setEditName(job.name === job.id ? "" : job.name);
    setEditDeliver(job.deliver || "local");
    setEditWorkdir(job.workdir);
  };

  const cancelEdit = () => {
    setEditingId(null);
    setEditSaving(false);
  };

  const pickEditWorkdir = async () => {
    if (await alertIfNativeDirectoryBlocked(t, "pick")) return;
    setPickingEditWorkdir(true);
    try {
      const out = await api.pickDirectory({
        title: t("hermes.settingsModal.cron.workdir"),
        initialPath: editWorkdir || undefined,
      });
      if (out.path) setEditWorkdir(out.path);
    } catch (e) {
      setError(errText(e));
    } finally {
      setPickingEditWorkdir(false);
    }
  };

  const saveEdit = async () => {
    if (!editingId || !editSchedule.trim()) return;
    setEditSaving(true);
    setError("");
    try {
      await api.editHermesCron(agentId, editingId, {
        schedule: editSchedule.trim(),
        prompt: editPrompt.trim(),
        name: editName.trim(),
        deliver: editDeliver.trim() || "local",
        workdir: editWorkdir.trim(),
      });
      cancelEdit();
      await reload();
    } catch (e) {
      setError(errText(e));
    } finally {
      setEditSaving(false);
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  if (!available) {
    return <p className="text-sm text-ink-500">{t("hermes.settingsModal.cron.unavailable")}</p>;
  }
  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}
      <p className="rounded-md border border-amber-200 bg-amber-50 px-3 py-2 text-xs text-amber-700">
        {t("hermes.settingsModal.cron.gatewayHint")}
      </p>
      {jobs.length === 0 ? (
        <p className="text-sm text-ink-500">{t("hermes.settingsModal.cron.empty")}</p>
      ) : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {jobs.map((j) => (
            <li key={j.id} className="px-3 py-2 text-sm">
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="flex items-center gap-2">
                    <span className="truncate font-medium text-ink-800">{j.name}</span>
                    <span
                      className={cn(
                        "rounded px-1.5 py-0.5 text-[11px]",
                        j.enabled
                          ? "bg-emerald-50 text-emerald-600"
                          : "bg-ink-100 text-ink-500",
                      )}
                    >
                      {j.enabled
                        ? t("hermes.settingsModal.cron.active")
                        : t("hermes.settingsModal.cron.paused")}
                    </span>
                  </div>
                  {j.schedule && (
                    <p className="mt-0.5 font-mono text-xs text-ink-600">{j.schedule}</p>
                  )}
                  {j.nextRun && (
                    <p className="truncate text-xs text-ink-400">
                      {t("hermes.settingsModal.cron.nextRun", { time: j.nextRun })}
                    </p>
                  )}
                  {j.deliver && j.deliver !== "local" && (
                    <p className="truncate text-xs text-ink-400">
                      {t("hermes.settingsModal.cron.deliverTo", { target: j.deliver })}
                    </p>
                  )}
                  {j.workdir && (
                    <p className="truncate font-mono text-xs text-ink-400">{j.workdir}</p>
                  )}
                </div>
                <div className="flex shrink-0 items-center gap-2">
                  <button
                    type="button"
                    className="text-xs text-ink-500 hover:text-ink-800 disabled:opacity-50"
                    onClick={() => void act(j.id, j.enabled ? "pause" : "resume")}
                    disabled={actionBusyId === j.id || editSaving || adding}
                  >
                    {j.enabled
                      ? t("hermes.settingsModal.cron.pause")
                      : t("hermes.settingsModal.cron.resume")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-ink-500 hover:text-ink-800 disabled:opacity-50"
                    onClick={() => (editingId === j.id ? cancelEdit() : startEdit(j))}
                    disabled={actionBusyId === j.id || editSaving || adding}
                  >
                    {editingId === j.id
                      ? t("common.cancel")
                      : t("hermes.settingsModal.cron.edit")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-rose-500 hover:text-rose-700 disabled:opacity-50"
                    onClick={() => void act(j.id, "remove")}
                    disabled={actionBusyId === j.id || editSaving || adding}
                  >
                    {t("hermes.settingsModal.cron.remove")}
                  </button>
                </div>
              </div>
              {editingId === j.id && (
                <div className="mt-3 space-y-2 rounded border border-ink-100 bg-ink-50/40 p-3">
                  <input
                    className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
                    value={editSchedule}
                    placeholder={t("hermes.settingsModal.cron.schedule")}
                    onChange={(e) => setEditSchedule(e.target.value)}
                  />
                  <input
                    className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
                    value={editName}
                    placeholder={t("hermes.settingsModal.cron.name")}
                    onChange={(e) => setEditName(e.target.value)}
                  />
                  <textarea
                    className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
                    rows={2}
                    value={editPrompt}
                    placeholder={t("hermes.settingsModal.cron.prompt")}
                    onChange={(e) => setEditPrompt(e.target.value)}
                  />
                  <div>
                    <label className="label">{t("hermes.settingsModal.cron.deliver")}</label>
                    <select
                      className="select w-full"
                      value={editDeliver}
                      onChange={(e) => setEditDeliver(e.target.value)}
                    >
                      {deliveryTargets.map((target) => (
                        <option key={target.value} value={target.value}>
                          {target.label}
                        </option>
                      ))}
                    </select>
                  </div>
                  <div className="flex gap-2">
                    <input
                      className="input flex-1"
                      value={editWorkdir}
                      placeholder={t("hermes.settingsModal.cron.workdir")}
                      onChange={(e) => setEditWorkdir(e.target.value)}
                    />
                    <button
                      type="button"
                      className="btn-outline whitespace-nowrap"
                      onClick={() => void pickEditWorkdir()}
                      disabled={pickingEditWorkdir}
                    >
                      {t("hermes.settingsModal.cron.pickWorkdir")}
                    </button>
                  </div>
                  <div className="flex items-center gap-2">
                    <button
                      type="button"
                      className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
                      onClick={() => void saveEdit()}
                      disabled={editSaving || !editSchedule.trim()}
                    >
                      {editSaving ? t("common.saving") : t("common.save")}
                    </button>
                    <button
                      type="button"
                      className="btn-outline"
                      onClick={cancelEdit}
                      disabled={editSaving}
                    >
                      {t("common.cancel")}
                    </button>
                  </div>
                </div>
              )}
            </li>
          ))}
        </ul>
      )}
      <div className="space-y-2 border-t border-ink-100 pt-3">
        <div className="text-sm font-medium text-ink-700">{t("hermes.settingsModal.cron.create")}</div>
        <input
          className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
          value={schedule}
          placeholder={t("hermes.settingsModal.cron.schedule")}
          onChange={(e) => setSchedule(e.target.value)}
        />
        <input
          className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
          value={jobName}
          placeholder={t("hermes.settingsModal.cron.name")}
          onChange={(e) => setJobName(e.target.value)}
        />
        <textarea
          className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
          rows={2}
          value={prompt}
          placeholder={t("hermes.settingsModal.cron.prompt")}
          onChange={(e) => setPrompt(e.target.value)}
        />
        <div>
          <label className="label">{t("hermes.settingsModal.cron.deliver")}</label>
          <select
            className="select w-full"
            value={deliver}
            onChange={(e) => setDeliver(e.target.value)}
          >
            {deliveryTargets.map((target) => (
              <option key={target.value} value={target.value}>
                {target.label}
              </option>
            ))}
          </select>
        </div>
        <div className="flex gap-2">
          <input
            className="input flex-1"
            value={workdir}
            placeholder={t("hermes.settingsModal.cron.workdir")}
            onChange={(e) => setWorkdir(e.target.value)}
          />
          <button
            type="button"
            className="btn-outline whitespace-nowrap"
            onClick={() => void pickCronWorkdir()}
            disabled={pickingWorkdir}
          >
            {t("hermes.settingsModal.cron.pickWorkdir")}
          </button>
        </div>
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void add()}
          disabled={!schedule.trim() || adding || editSaving || actionBusyId !== null}
        >
          {adding ? t("common.saving") : t("hermes.settingsModal.cron.add")}
        </button>
      </div>
    </div>
  );
}

async function openHermesDashboard(
  t: (key: string, opts?: Record<string, string>) => string,
  onRemote: () => void,
  onError: (message: string) => void,
  setBusy?: (busy: boolean) => void,
  agentId?: string,
): Promise<void> {
  if (isRemoteBrowser()) {
    onRemote();
    return;
  }
  setBusy?.(true);
  try {
    // Pass the agent id so the dashboard opens scoped to THIS agent's Hermes
    // profile (and its sessions), not the root home.
    const { url } = await api.openHermesDashboard(agentId);
    window.open(url, "_blank", "noopener,noreferrer");
  } catch (e) {
    onError(t("hermes.dashboardOpenFailed", { message: errText(e) }));
  } finally {
    setBusy?.(false);
  }
}
