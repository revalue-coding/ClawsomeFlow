/** Hermes Agent management + direct chat ("My Team" → Hermes Agent).
 *
 * Mirrors the OpenClaw chat page but for managed Hermes agents (= Hermes
 * profiles). Differences: permanent delete only, a "Claim existing" action,
 * a per-chat working-directory picker, and a "my-profile" button (opens the
 * profile root). No Import&Optimize / Agent Store / "to Hermes" button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
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
import { ChatBubble } from "@/components/ChatBubble";
import { DesktopIcon, ExternalLinkIcon, SettingsIcon, TrashIcon } from "@/components/icons";
import {
  clearChatHistory,
  loadChatHistory,
  reconcileTranscript,
  saveChatHistory,
} from "@/lib/chatHistory";
import { handleChatTextareaEnterKey } from "@/lib/chatInput";
import { cn } from "@/lib/cn";
import {
  api,
  ApiError,
  isNetworkError,
  type ChatHistoryMessage,
  type HermesAgentSummary,
  type HermesCronJob,
  type HermesModelSetting,
  type HermesMcpServer,
  type HermesSkillSetting,
  type OpenclawTeam,
} from "@/lib/api";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";
import { useOpRecovery } from "@/lib/useOpRecovery";

const CREATE_TEAM_SENTINEL = "__create_team__";
const DEFAULT_WORKDIR = "~";
// Cancel only unlocks once the backend confirms rollback; mirror OpenClaw's
// verify-by-list loop so the popup doesn't close before the agent is gone.
const CREATE_CANCEL_VERIFY_TIMEOUT_MS = 30 * 1000;
const CREATE_CANCEL_VERIFY_POLL_MS = 800;

type CreateCancelState = {
  agentId: string;
  cancelling: boolean;
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

function isRemoteBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
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
  const navigate = useNavigate();
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

  // ── Build-progress popup (mirrors OpenClaw's work popup). `workPopupText`
  // and `createCancelState` are session-backed so the popup restores on return;
  // `workPopupRunning`/`workPopupSuccess` are transient (recomputed from
  // createCancelState after a remount).
  const [workPopupOpen, setWorkPopupOpen] = useSessionBackedModalFlag("hermes:create:workPopupOpen");
  const [workPopupRunning, setWorkPopupRunning] = useState(false);
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
  const createAbortRef = useRef<AbortController | null>(null);
  const createCancelRequestedRef = useRef(false);
  // Guards against a double-submit (e.g. a fast double-click in the window
  // before the modal closes) firing two POST /hermes/agents for the same id —
  // which on the backend would race and could clobber the winner's profile.
  const createInFlightRef = useRef(false);

  const reload = useCallback(async () => {
    setLoading(true);
    setError("");
    try {
      const [a, tm] = await Promise.all([
        api.listHermesAgents(),
        api.listOpenclawTeams(),
      ]);
      setAgents(a.items);
      setTeams(tm.items);
    } catch (e) {
      setError(errText(e));
    } finally {
      setLoading(false);
    }
  }, []);

  // Quiet refetch (no loading spinner) used by the build-progress poll.
  const refreshAgents = useCallback(async () => {
    try {
      const a = await api.listHermesAgents();
      setAgents(a.items);
    } catch {
      /* ignore poll errors — the next tick retries */
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const workPopupBusy = workPopupRunning || createCancelState !== null;
  const showCreateCancelAction = createCancelState !== null;
  const createCancelEnabled =
    showCreateCancelAction && !(createCancelState?.cancelling ?? false);
  const workPopupDisplayText =
    workPopupText || (workPopupOpen ? t("hermes.create.workPopup.running") : "");

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

  // Durable recovery across refresh / tab close+reopen: a localStorage pointer
  // to the in-flight create op + on-mount status query (+ WS for the terminal
  // transition). The in-page awaited POST still drives the happy path; this only
  // fires when the awaiting closure is gone (the page was reloaded/reopened).
  const { track: trackOp, clear: clearOp } = useOpRecovery("hermes:create:op", {
    onRunning: (p) => {
      setCreateCancelState({ agentId: p.agentId, cancelling: false });
      openWorkPopup();
    },
    onSucceeded: (p) => {
      finishWorkPopup(true, t("hermes.create.workPopup.created", { id: p.agentId }));
      resetCreateForm();
      void reload();
    },
    onFailed: (_p, detail) => {
      finishWorkPopup(
        false,
        detail === "cancelled" ? t("hermes.create.workPopup.cancelled") : detail,
      );
    },
  });

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
      setCreateCancelState({ agentId: profileId, cancelling: false });
      trackOp({ opId: `hermes_create:${profileId}`, agentId: profileId });
      openWorkPopup();
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
      createInFlightRef.current = false;
    }
  }, [
    createProfileId, createName, createResponsibility, createModelInheritFrom,
    createCloneFrom, createCloneAll,
    createTeamChoice, createNewTeamName,
    agents, setShowCreate, setCreateCancelState, openWorkPopup, finishWorkPopup,
    resetCreateForm, resetWorkPopupDisplayState, resetCreateCancelState, setWorkPopupOpen,
    reload, trackOp, clearOp, t,
  ]);

  const cancelCreate = useCallback(async () => {
    if (createCancelState === null || createCancelState.cancelling) return;
    const agentId = createCancelState.agentId;
    createCancelRequestedRef.current = true;
    createAbortRef.current?.abort();
    setCreateCancelState((prev) => (prev === null ? prev : { ...prev, cancelling: true }));
    setWorkPopupText(t("hermes.create.workPopup.cancelRunning"));
    try {
      await api.cancelHermesAgentCreate(agentId);
      setWorkPopupText(t("hermes.create.workPopup.cancelVerifying"));
      const deadline = Date.now() + CREATE_CANCEL_VERIFY_TIMEOUT_MS;
      for (;;) {
        const listed = await api.listHermesAgents();
        if (!listed.items.some((item) => item.id === agentId)) break;
        if (Date.now() >= deadline) {
          throw new Error(t("hermes.create.workPopup.cancelAgentStillVisible"));
        }
        await new Promise((resolve) => window.setTimeout(resolve, CREATE_CANCEL_VERIFY_POLL_MS));
      }
      clearOp();
      setWorkPopupOpen(false);
      resetWorkPopupDisplayState();
      resetCreateCancelState();
      void refreshAgents();
    } catch (e) {
      finishWorkPopup(false, t("hermes.create.workPopup.cancelFailed", { message: errText(e) }));
    }
  }, [
    createCancelState, setCreateCancelState, setWorkPopupText, setWorkPopupOpen,
    resetWorkPopupDisplayState, resetCreateCancelState, finishWorkPopup, refreshAgents, clearOp, t,
  ]);

  // Recovery across refresh / close+reopen is handled by useOpRecovery above
  // (durable localStorage pointer + GET status + WS), replacing the old 3s list
  // poll + list-presence reconcile effect.

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
          setWorkPopupOpen(false);
          resetWorkPopupDisplayState();
          resetCreateCancelState();
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
          {workPopupBusy && <Loading />}
          {showCreateCancelAction && (
            <div className="flex justify-end">
              <button
                type="button"
                className="btn-outline"
                onClick={() => void cancelCreate()}
                disabled={!createCancelEnabled}
              >
                {createCancelState?.cancelling
                  ? t("hermes.create.cancelling")
                  : t("hermes.create.cancelCreate")}
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
                {t("hermes.create.workPopup.close")}
              </button>
            </div>
          )}
        </div>
      </Modal>

      {removeTarget && (
        <RemoveModal
          agent={removeTarget}
          onClose={() => setRemoveTarget(null)}
          onDone={() => {
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
}: {
  agent: HermesAgentSummary;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const remove = async () => {
    setBusy(true);
    setError("");
    try {
      await api.deleteHermesAgent(agent.id);
      onDone();
    } catch (e) {
      setError(errText(e));
      setBusy(false);
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
}

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
  const [workdir, setWorkdir] = useState(
    () => localStorage.getItem(`hermes-workdir-${agentId}`) || DEFAULT_WORKDIR,
  );
  const updateWorkdir = (next: string) => {
    setWorkdir(next);
    if (next.trim()) localStorage.setItem(`hermes-workdir-${agentId}`, next);
    else localStorage.removeItem(`hermes-workdir-${agentId}`);
  };
  const [sending, setSending] = useState(false);
  // True while polling for an assistant reply whose stream was detached by a tab
  // switch (the answer is still landing in server history). Drives a pending
  // bubble without persisting an empty placeholder to the cache.
  const [recovering, setRecovering] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useSessionBackedModalFlag(`hermes:${agentId}:settings:open`);
  const [opening, setOpening] = useState(false);
  const [dashboardBusy, setDashboardBusy] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  // localStorage scope for the transcript cache. Namespaced under `hermes:` so
  // it can't collide with an OpenClaw agent's cache for the same id.
  const chatScope = `hermes:${agentId}`;

  useEffect(() => {
    void (async () => {
      // Show the cache immediately (it may hold an in-flight partial), then
      // reconcile against server history once it loads.
      const cached: ChatMsg[] = loadChatHistory(chatScope).map((m) => ({
        role: m.role,
        content: m.content,
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
          content: m.content,
        }));
        const merged = reconcileTranscript(cached, server);
        setMessages(merged);
        if (merged.length > 0) saveChatHistory(chatScope, merged);
        // A trailing user turn means a reply is still being produced server-side
        // (its stream was detached); poll for it instead of showing "no reply".
        const last = merged[merged.length - 1];
        if (last && last.role === "user") setRecovering(true);
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

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages, recovering]);

  // Recover a reply whose stream was detached by a tab switch: poll server
  // history until the assistant turn lands (the backend persists it even after a
  // client disconnect), then adopt it. Bounded so a genuinely answerless turn
  // (e.g. a prior error) doesn't poll forever.
  useEffect(() => {
    if (!recovering) return;
    let cancelled = false;
    let timer: number | undefined;
    let tries = 0;
    const MAX_TRIES = 30; // ~60s at 2s intervals
    const tick = async () => {
      tries += 1;
      try {
        const hist = await api.getHermesAgentChatHistory(agentId);
        if (cancelled) return;
        const server: ChatMsg[] = hist.messages.map((m: ChatHistoryMessage) => ({
          role: m.role,
          content: m.content,
        }));
        const last = server[server.length - 1];
        if (last && last.role === "assistant" && last.content.trim() !== "") {
          setMessages(server);
          saveChatHistory(chatScope, server);
          setRecovering(false);
          return;
        }
      } catch {
        /* transient — keep polling */
      }
      if (cancelled) return;
      if (tries >= MAX_TRIES) {
        setRecovering(false);
        return;
      }
      timer = window.setTimeout(() => void tick(), 2000);
    };
    timer = window.setTimeout(() => void tick(), 2000);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [recovering, agentId, chatScope]);

  const pickWorkdir = async () => {
    if (isRemoteBrowser()) {
      void alert(t("hermes.remoteUnavailable"));
      return;
    }
    try {
      const out = await api.pickDirectory({ title: t("hermes.workdir"), initialPath: workdir || undefined });
      if (out.path) updateWorkdir(out.path);
    } catch (e) {
      setError(errText(e));
    }
  };

  const openProfile = async () => {
    if (isRemoteBrowser()) {
      void alert(t("hermes.remoteUnavailable"));
      return;
    }
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

  const send = async () => {
    const message = input.trim();
    if (!message) return;
    if (!workdir) {
      setError(t("hermes.workdirNeeded"));
      return;
    }
    setError("");
    setRecovering(false); // a fresh send supersedes any in-flight recovery poll
    setSending(true);
    setInput("");
    setMessages((prev) => [...prev, { role: "user", content: message }, { role: "assistant", content: "" }]);
    try {
      const res = await api.chatWithHermesAgent(agentId, { message, workdir });
      if (!res.ok || !res.body) {
        throw new Error(`HTTP ${res.status}`);
      }
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = "";
      let streamErr = "";
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
            const obj = JSON.parse(data) as { delta?: string; error?: string };
            if (obj.error) {
              streamErr = obj.error;
            } else if (typeof obj.delta === "string") {
              setMessages((prev) => {
                const next = [...prev];
                next[next.length - 1] = {
                  role: "assistant",
                  content: next[next.length - 1].content + obj.delta,
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
      setError(t("hermes.chatError", { message: errText(e) }));
    } finally {
      setSending(false);
    }
  };

  const reset = async () => {
    if (sending || resetting) return;
    setResetting(true);
    setError("");
    try {
      await api.resetHermesAgentChat(agentId);
      clearChatHistory(chatScope);
      setRecovering(false);
      setMessages([]);
      setInput("");
    } catch (e) {
      setError(errText(e));
    } finally {
      setResetting(false);
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
              {opening ? t("hermes.opening") : t("hermes.myProfile")}
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
                onClick={() => void openHermesDashboard(t, () => void alert(t("hermes.dashboardRemoteUnavailable")), (msg) => void alert(msg), setDashboardBusy)}
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
        <div
          ref={scrollRef}
          className="min-h-[280px] flex-1 space-y-4 overflow-auto bg-ink-50/40 px-5 py-4"
        >
          {(recovering &&
          messages.length > 0 &&
          messages[messages.length - 1].role === "user"
            ? [...messages, { role: "assistant" as const, content: "" }]
            : messages)
            .filter((m) => m.role !== "system")
            .map((m, i, list) => (
              <ChatBubble
                key={i}
                msg={m}
                pending={
                  (sending || recovering) &&
                  i === list.length - 1 &&
                  m.role === "assistant" &&
                  !m.content
                }
                noTextReply={t("chat.noTextReply")}
              />
            ))}
        </div>
        <form
          onSubmit={(e) => {
            e.preventDefault();
            if (!sending && !resetting) void send();
          }}
          className="space-y-2 border-t border-ink-100 p-3"
        >
          <div className="flex items-center gap-2 text-xs text-ink-500">
            <button
              type="button"
              className="btn-outline shrink-0 !px-2 !py-1 text-xs"
              onClick={() => void pickWorkdir()}
            >
              {t("hermes.pickWorkdir")}
            </button>
            <input
              className="input flex-1 font-mono text-xs"
              value={workdir}
              placeholder={t("hermes.workdirNeeded")}
              onChange={(e) => updateWorkdir(e.target.value)}
            />
          </div>
          <div className="flex items-end gap-2">
            <textarea
              className="textarea h-20 flex-1 resize-none"
              placeholder={t("chat.inputPlaceholder")}
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                handleChatTextareaEnterKey(e, () => {
                  if (!sending && !resetting) {
                    (e.currentTarget.form as HTMLFormElement).requestSubmit();
                  }
                });
              }}
              disabled={sending || resetting}
            />
            <button
              type="button"
              className="btn-outline"
              disabled={sending || resetting}
              onClick={() => void reset()}
            >
              {resetting ? t("chat.resetting") : t("chat.reset")}
            </button>
            <button type="submit" className="btn-primary" disabled={sending || resetting || !input.trim()}>
              {sending ? t("chat.sending") : t("chat.send")}
            </button>
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

type SettingsTab = "soul" | "model" | "mcp" | "skills" | "cron";

function SettingsModal({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<SettingsTab>("soul");

  return (
    <Modal open onClose={onClose} title={t("hermes.settingsModal.title")} width="max-w-3xl">
      {/* Segmented tab control — mirrors the OpenClaw settings modal so the
          selected tab reads in both themes (a frosted surface chip with brand
          text + ring), instead of the old white-on-near-white in dark mode. */}
      <div className="mb-4 rounded-xl border border-ink-100 bg-ink-50/60 p-1.5">
        <div className="grid grid-cols-5 gap-1">
          {(["soul", "model", "mcp", "skills", "cron"] as SettingsTab[]).map((k) => (
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

function McpTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<HermesMcpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [saving, setSaving] = useState(false);
  const [name, setName] = useState("");
  const [transport, setTransport] = useState<"http_sse" | "sse">("http_sse");
  const [url, setUrl] = useState("");
  const [environment, setEnvironment] = useState("");

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

  const save = async () => {
    if (!name.trim() || !url.trim()) return;
    setSaving(true);
    setError("");
    try {
      await api.putHermesMcpServer(agentId, {
        name: name.trim(),
        transport,
        url: url.trim(),
        environment,
      });
      setName("");
      setTransport("http_sse");
      setUrl("");
      setEnvironment("");
      setServers(await api.getHermesMcpServers(agentId));
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  const remove = async (serverName: string) => {
    setError("");
    try {
      await api.deleteHermesMcpServer(agentId, serverName);
      setServers(await api.getHermesMcpServers(agentId));
    } catch (e) {
      setError(errText(e));
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
                    {server.transport === "sse"
                      ? t("hermes.settingsModal.mcp.transportSse")
                      : t("hermes.settingsModal.mcp.transportHttp")}
                  </span>
                </div>
                <p className="truncate font-mono text-xs text-ink-500">{server.url}</p>
                {server.envKeys.length > 0 && (
                  <p className="truncate text-xs text-ink-400">
                    {t("hermes.settingsModal.mcp.envKeys", { keys: server.envKeys.join(", ") })}
                  </p>
                )}
              </div>
              <button
                type="button"
                className="text-xs text-rose-500 hover:text-rose-700"
                onClick={() => void remove(server.name)}
              >
                {t("hermes.settingsModal.mcp.remove")}
              </button>
            </li>
          ))}
        </ul>
      )}

      <div className="space-y-2 border-t border-ink-100 pt-3">
        <div className="text-sm font-medium text-ink-700">{t("hermes.settingsModal.mcp.add")}</div>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.nameLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={name}
            placeholder={t("hermes.settingsModal.mcp.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.transportLabel")}</span>
          <select
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={transport}
            onChange={(e) => setTransport(e.target.value as "http_sse" | "sse")}
          >
            <option value="http_sse">{t("hermes.settingsModal.mcp.transportHttp")}</option>
            <option value="sse">{t("hermes.settingsModal.mcp.transportSse")}</option>
          </select>
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.mcp.urlLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs"
            value={url}
            placeholder={t("hermes.settingsModal.mcp.urlPlaceholder")}
            onChange={(e) => setUrl(e.target.value)}
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
        </label>
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void save()}
          disabled={saving || !name.trim() || !url.trim()}
        >
          {saving ? t("common.saving") : t("hermes.settingsModal.mcp.add")}
        </button>
      </div>
    </div>
  );
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

  const del = async (nameKey: string) => {
    setError("");
    try {
      await api.deleteHermesSkill(agentId, nameKey);
      await reload();
    } catch (e) {
      setError(errText(e));
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
                    className="text-xs text-ink-500 hover:text-ink-800"
                    onClick={() => void view(s.name)}
                  >
                    {expanded === s.name
                      ? t("hermes.settingsModal.skills.hide")
                      : t("hermes.settingsModal.skills.view")}
                  </button>
                  <button
                    type="button"
                    className="text-xs text-rose-500 hover:text-rose-700"
                    onClick={() => void del(s.name)}
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
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [schedule, setSchedule] = useState("");
  const [prompt, setPrompt] = useState("");
  const [jobName, setJobName] = useState("");
  const [workdir, setWorkdir] = useState("");
  const [pickingWorkdir, setPickingWorkdir] = useState(false);

  const reload = useCallback(async () => {
    setLoading(true);
    try {
      const r = await api.getHermesCron(agentId);
      setAvailable(r.available);
      setJobs(r.items);
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
    if (isRemoteBrowser()) {
      void alert(t("hermes.remoteUnavailable"));
      return;
    }
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
    if (!schedule.trim()) return;
    setError("");
    try {
      await api.createHermesCron(agentId, {
        schedule: schedule.trim(),
        prompt: prompt.trim(),
        name: jobName.trim(),
        workdir: workdir.trim(),
      });
      setSchedule("");
      setPrompt("");
      setJobName("");
      setWorkdir("");
      await reload();
    } catch (e) {
      setError(errText(e));
    }
  };

  const act = async (jobId: string, action: "pause" | "resume" | "remove") => {
    setError("");
    try {
      await api.hermesCronAction(agentId, jobId, action);
      await reload();
    } catch (e) {
      setError(errText(e));
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  if (!available) {
    return <p className="text-sm text-ink-500">{t("hermes.settingsModal.cron.unavailable")}</p>;
  }
  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}
      {jobs.length === 0 ? (
        <p className="text-sm text-ink-500">{t("hermes.settingsModal.cron.empty")}</p>
      ) : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {jobs.map((j) => (
            <li key={j.id} className="flex items-center justify-between px-3 py-2 text-sm">
              <div className="min-w-0">
                <code className="text-xs">{j.id}</code>
                <p className="truncate text-xs text-ink-500">{j.detail || j.raw}</p>
              </div>
              <div className="flex items-center gap-2">
                <button
                  type="button"
                  className="text-xs text-ink-500 hover:text-ink-800"
                  onClick={() => void act(j.id, j.enabled ? "pause" : "resume")}
                >
                  {j.enabled
                    ? t("hermes.settingsModal.cron.pause")
                    : t("hermes.settingsModal.cron.resume")}
                </button>
                <button
                  type="button"
                  className="text-xs text-rose-500 hover:text-rose-700"
                  onClick={() => void act(j.id, "remove")}
                >
                  {t("hermes.settingsModal.cron.remove")}
                </button>
              </div>
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
            {pickingWorkdir
              ? t("hermes.settingsModal.cron.pickingWorkdir")
              : t("hermes.settingsModal.cron.pickWorkdir")}
          </button>
        </div>
        <button
          type="button"
          className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
          onClick={() => void add()}
          disabled={!schedule.trim()}
        >
          {t("hermes.settingsModal.cron.add")}
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
): Promise<void> {
  if (isRemoteBrowser()) {
    onRemote();
    return;
  }
  setBusy?.(true);
  try {
    const { url } = await api.openHermesDashboard();
    window.open(url, "_blank", "noopener,noreferrer");
  } catch (e) {
    onError(t("hermes.dashboardOpenFailed", { message: errText(e) }));
  } finally {
    setBusy?.(false);
  }
}
