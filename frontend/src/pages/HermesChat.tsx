/** Hermes Agent management + direct chat ("My Team" → Hermes Agent).
 *
 * Mirrors the OpenClaw chat page but for managed Hermes agents (= Hermes
 * profiles). Differences: permanent delete only, a "Claim existing" action,
 * a per-chat working-directory picker, and a "my-profile" button (opens the
 * profile root). No Import&Optimize / Agent Store / "to Hermes" button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Link, useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  Card,
  EmptyState,
  ErrorBox,
  Loading,
  Modal,
} from "@/components/ui";
import { AgentCardAvatar } from "@/components/AgentCardAvatar";
import { ChatBubble } from "@/components/ChatBubble";
import { DesktopIcon, SettingsIcon } from "@/components/icons";
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
  type HermesSecretSetting,
  type HermesSkillSetting,
  type OpenclawTeam,
} from "@/lib/api";
import { useSessionBackedModalFlag, useSessionBackedState } from "@/lib/sessionState";

const CREATE_TEAM_SENTINEL = "__create_team__";
const DEFAULT_WORKDIR = "~";

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
  return agentCardShowsIdLine(agent) ? name : agent.id;
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

  useEffect(() => {
    void reload();
  }, [reload]);

  const grouped = useMemo(() => {
    const m = new Map<string, HermesAgentSummary[]>();
    for (const a of agents) {
      const key = a.teamName || t("hermes.ungrouped");
      const arr = m.get(key) ?? [];
      arr.push(a);
      m.set(key, arr);
    }
    return Array.from(m.entries());
  }, [agents, t]);

  return (
    <div className="space-y-5">
      <div className="flex items-start justify-between gap-3">
        <div>
          <h1 className="text-xl font-semibold text-ink-900">{t("hermes.title")}</h1>
          <p className="text-sm text-ink-500">{t("hermes.pickerTitle")}</p>
        </div>
        <div className="flex shrink-0 gap-2">
          <button type="button" className="btn-outline" onClick={() => void reload()}>
            {t("hermes.refresh")}
          </button>
          <button type="button" className="btn-primary" onClick={() => setShowCreate(true)}>
            {t("hermes.createAgent")}
          </button>
        </div>
      </div>

      <div className="inline-flex rounded-lg border border-brand-200 bg-brand-50/60 p-0.5 shadow-[inset_0_1px_0_rgba(255,255,255,0.8)]">
        {(["card", "list"] as const).map((m) => (
          <button
            key={m}
            type="button"
            className={cn(
              "min-w-[52px] rounded-md px-2 py-1 text-xs font-semibold transition-all duration-200",
              viewMode === m
                ? "bg-gradient-to-r from-brand-600 to-brand-400 text-white"
                : "text-ink-600 hover:bg-white/70 hover:text-brand-700",
            )}
            onClick={() => setViewMode(m)}
          >
            {m === "card" ? t("chat.viewCard") : t("chat.viewList")}
          </button>
        ))}
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {loading ? (
        <Loading label={t("common.loading")} />
      ) : agents.length === 0 ? (
        <EmptyState title={t("hermes.title")} hint={t("hermes.listEmpty")} />
      ) : viewMode === "card" ? (
        <div className="space-y-5">
          {grouped.map(([team, list]) => (
            <div key={team} className="space-y-3">
              <div className="inline-flex items-center gap-1 rounded-full border border-brand-200 bg-brand-50 px-3 py-1 text-xs font-semibold text-brand-700">
                {t("hermes.team")}: {team}
              </div>
              <div className="grid grid-cols-1 gap-4 md:grid-cols-2 lg:grid-cols-3">
                {list.map((a) => (
                  <Link
                    key={a.id}
                    to={`/hermes/${a.id}`}
                    className="group card block p-5 transition-all hover:border-brand-300 hover:shadow-[0_0_24px_-6px_theme(colors.brand.300)]"
                  >
                    <div className="flex items-start justify-between">
                      <AgentCardAvatar platform="hermes" />
                      <button
                        type="button"
                        className="text-xs text-rose-500 hover:text-rose-700"
                        onClick={(e) => {
                          e.preventDefault();
                          e.stopPropagation();
                          setRemoveTarget(a);
                        }}
                      >
                        {t("hermes.removeAgent")}
                      </button>
                    </div>
                    <div className="font-semibold text-ink-900">{agentCardTitle(a)}</div>
                    {agentCardShowsIdLine(a) && (
                      <div className="mt-0.5 font-mono text-xs text-ink-500">{a.id}</div>
                    )}
                    {a.description && (
                      <p className="mt-2 line-clamp-3 text-xs text-ink-500">{a.description}</p>
                    )}
                  </Link>
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
          agents={agents}
          onClose={() => setShowCreate(false)}
          onDone={(created) => {
            setShowCreate(false);
            void reload();
            navigate(`/hermes/${created.id}`);
          }}
        />
      )}
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

function CreateModal({
  teams,
  agents,
  onClose,
  onDone,
}: {
  teams: OpenclawTeam[];
  agents: HermesAgentSummary[];
  onClose: () => void;
  onDone: (created: { id: string }) => void;
}) {
  const { t } = useTranslation();
  // ``name`` is the human-friendly Agent Name (shown as the card title and
  // injected into the bootstrap prompt). ``profileId`` is the Hermes profile id.
  // Session-backed so switching modules mid-create keeps the form + busy state.
  const [name, setName] = useSessionBackedState("hermes:create:name", "");
  const [profileId, setProfileId] = useSessionBackedState("hermes:create:profileId", "");
  const [responsibility, setResponsibility] = useSessionBackedState("hermes:create:responsibility", "");
  const [teamChoice, setTeamChoice] = useSessionBackedState("hermes:create:teamChoice", "");
  const [newTeamName, setNewTeamName] = useSessionBackedState("hermes:create:newTeamName", "");
  const [busy, setBusy] = useSessionBackedState("hermes:create:busy", false);
  const [cancelling, setCancelling] = useState(false);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const mountedRef = useRef(true);
  useEffect(() => {
    mountedRef.current = true;
    return () => { mountedRef.current = false; };
  }, []);

  const resetForm = () => {
    setName(""); setProfileId(""); setResponsibility("");
    setTeamChoice(""); setNewTeamName(""); setBusy(false);
  };

  // Reconcile-on-return: if we navigated away mid-create and the agent finished
  // while unmounted, the freshly-reloaded list now contains it → treat as done.
  useEffect(() => {
    if (!busy) return;
    const created = profileId.trim();
    if (created && agents.some((a) => a.id === created)) {
      setBusy(false);
      onDone({ id: created });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agents]);

  const submit = async () => {
    setBusy(true);
    setError("");
    const ac = new AbortController();
    abortRef.current = ac;
    try {
      const teamId = await resolveTeamId(teamChoice, newTeamName);
      const created = await api.createHermesAgent(
        {
          id: profileId.trim(),
          name: name.trim(),
          responsibility: responsibility.trim(),
          teamId,
        },
        { signal: ac.signal },
      );
      if (!mountedRef.current) return; // returned via reconcile instead
      resetForm();
      onDone(created);
    } catch (e) {
      if (ac.signal.aborted) return; // cancelled — handled by cancelCreate
      if (!mountedRef.current) return;
      setError(errText(e));
      setBusy(false);
    }
  };

  // Bootstrap can run for minutes; let the user abort it. We abort the request
  // AND tell the backend to kill the bootstrap + roll back the half-built agent.
  const cancelCreate = async () => {
    setCancelling(true);
    abortRef.current?.abort();
    try {
      await api.cancelHermesAgentCreate(profileId.trim());
    } catch {
      /* best-effort — closing anyway */
    }
    resetForm();
    onClose();
  };

  const teamReady = teamChoice !== CREATE_TEAM_SENTINEL || newTeamName.trim().length > 0;

  return (
    <Modal open onClose={onClose} title={t("hermes.create.title")} dismissible={!busy} width="max-w-2xl">
      <div className="space-y-3">
        {error && <ErrorBox>{error}</ErrorBox>}
        <div>
          <label className="label">{t("hermes.create.nameLabel")}</label>
          <input
            className="input"
            value={name}
            placeholder={t("hermes.create.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </div>
        <div>
          <label className="label">{t("hermes.create.idLabel")}</label>
          <input
            className="input font-mono"
            value={profileId}
            placeholder={t("hermes.create.idPlaceholder")}
            onChange={(e) => setProfileId(e.target.value)}
          />
          <div className="mt-1 text-xs text-ink-400">{t("hermes.create.idHint")}</div>
        </div>
        <div>
          <label className="label">{t("hermes.create.responsibility")}</label>
          <textarea
            className="textarea h-24"
            value={responsibility}
            placeholder={t("hermes.create.responsibilityPlaceholder")}
            onChange={(e) => setResponsibility(e.target.value)}
          />
        </div>
        <div>
          <label className="label">{t("hermes.create.teamLabel")}</label>
          <TeamSelect
            teams={teams}
            value={teamChoice}
            onChange={setTeamChoice}
            newTeamName={newTeamName}
            onNewTeamNameChange={setNewTeamName}
          />
        </div>
        <div className="flex justify-end gap-2 pt-2">
          <button
            type="button"
            className="btn-outline"
            onClick={busy ? () => void cancelCreate() : onClose}
            disabled={cancelling}
          >
            {busy
              ? cancelling
                ? t("hermes.create.cancelling")
                : t("hermes.create.cancelCreate")
              : t("common.cancel")}
          </button>
          <button
            type="button"
            className="btn-primary"
            onClick={() => void submit()}
            disabled={busy || !name.trim() || !profileId.trim() || !teamReady}
          >
            {busy ? t("hermes.create.creating") : t("hermes.create.submit")}
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
  const [input, setInput] = useState("");
  const [workdir, setWorkdir] = useState(
    () => localStorage.getItem(`hermes-workdir-${agentId}`) || DEFAULT_WORKDIR,
  );
  const updateWorkdir = (next: string) => {
    setWorkdir(next);
    if (next.trim()) localStorage.setItem(`hermes-workdir-${agentId}`, next);
    else localStorage.removeItem(`hermes-workdir-${agentId}`);
  };
  const [sending, setSending] = useState(false);
  const [resetting, setResetting] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useSessionBackedModalFlag(`hermes:${agentId}:settings:open`);
  const [opening, setOpening] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void (async () => {
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
        setMessages(hist.messages.map((m: ChatHistoryMessage) => ({ role: m.role, content: m.content })));
      } catch (e) {
        setError(errText(e));
      }
    })();
  }, [agentId]);

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight });
  }, [messages]);

  const pickWorkdir = async () => {
    if (isRemoteBrowser()) {
      window.alert(t("hermes.remoteUnavailable"));
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
      window.alert(t("hermes.remoteUnavailable"));
      return;
    }
    setOpening(true);
    try {
      await api.openDirectory({ path: profileRoot });
    } catch (e) {
      window.alert(t("hermes.openFailed", { message: errText(e) }));
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
          <button
            type="button"
            className="btn-outline inline-flex h-10 items-center justify-center gap-2 px-4 py-0 text-sm font-medium"
            onClick={() => setShowSettings(true)}
          >
            <SettingsIcon className="h-4 w-4" />
            {t("hermes.settings")}
          </button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      <Card className="flex min-h-0 flex-1 flex-col overflow-hidden p-0">
        <div
          ref={scrollRef}
          className="min-h-[280px] flex-1 space-y-4 overflow-auto bg-ink-50/40 px-5 py-4"
        >
          {messages
            .filter((m) => m.role !== "system")
            .map((m, i, list) => (
              <ChatBubble
                key={i}
                msg={m}
                pending={
                  sending &&
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

type SettingsTab = "soul" | "model" | "skills" | "cron";

function SettingsModal({ agentId, onClose }: { agentId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<SettingsTab>("soul");

  return (
    <Modal open onClose={onClose} title={t("hermes.settingsModal.title")} width="max-w-3xl">
      <div className="flex gap-2 border-b border-ink-100 pb-2 mb-4">
        {(["soul", "model", "skills", "cron"] as SettingsTab[]).map((k) => (
          <button
            key={k}
            type="button"
            className={
              tab === k
                ? "rounded bg-ink-900 px-3 py-1.5 text-xs font-medium text-white"
                : "rounded px-3 py-1.5 text-xs text-ink-600 hover:bg-ink-50"
            }
            onClick={() => setTab(k)}
          >
            {t(`hermes.settingsModal.tabs.${k}`)}
          </button>
        ))}
      </div>
      {tab === "soul" && <SoulTab agentId={agentId} />}
      {tab === "model" && <ModelTab agentId={agentId} />}
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
  const [secrets, setSecrets] = useState<HermesSecretSetting[]>([]);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  const [newKey, setNewKey] = useState("");
  const [newVal, setNewVal] = useState("");

  const reloadSecrets = useCallback(async () => {
    setSecrets(await api.getHermesSecrets(agentId));
  }, [agentId]);

  useEffect(() => {
    Promise.all([api.getHermesModel(agentId), api.getHermesSecrets(agentId)])
      .then(([m, s]) => {
        setModel(m);
        setSecrets(s);
      })
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  }, [agentId]);

  const saveModel = async () => {
    setBusy(true);
    setSaved(false);
    setError("");
    try {
      setModel(await api.putHermesModel(agentId, model));
      setSaved(true);
    } catch (e) {
      setError(errText(e));
    } finally {
      setBusy(false);
    }
  };

  const addSecret = async () => {
    if (!newKey.trim()) return;
    setError("");
    try {
      await api.putHermesSecret(agentId, { key: newKey.trim(), value: newVal });
      setNewKey("");
      setNewVal("");
      await reloadSecrets();
    } catch (e) {
      setError(errText(e));
    }
  };

  const delSecret = async (key: string) => {
    try {
      await api.deleteHermesSecret(agentId, key);
      await reloadSecrets();
    } catch (e) {
      setError(errText(e));
    }
  };

  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-4">
      {error && <ErrorBox>{error}</ErrorBox>}
      <div className="space-y-2">
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.model.modelLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={model.default}
            onChange={(e) => setModel({ ...model, default: e.target.value })}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.model.providerLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={model.provider}
            onChange={(e) => setModel({ ...model, provider: e.target.value })}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.settingsModal.model.baseUrlLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={model.baseUrl}
            onChange={(e) => setModel({ ...model, baseUrl: e.target.value })}
          />
        </label>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => void saveModel()}
            disabled={busy}
          >
            {t("hermes.settingsModal.save")}
          </button>
          {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
        </div>
      </div>

      <div className="border-t border-ink-100 pt-3">
        <div className="mb-2 text-sm font-medium text-ink-700">
          {t("hermes.settingsModal.model.apiKeys")}
        </div>
        {secrets.length === 0 ? (
          <p className="text-xs text-ink-400">{t("hermes.settingsModal.model.noKeys")}</p>
        ) : (
          <ul className="mb-2 divide-y divide-ink-100 rounded border border-ink-100">
            {secrets.map((s) => (
              <li key={s.key} className="flex items-center justify-between px-3 py-1.5 text-sm">
                <code>{s.key}</code>
                <div className="flex items-center gap-3">
                  <span className="text-xs text-ink-400">
                    {s.isSet ? s.preview : ""}
                  </span>
                  <button
                    type="button"
                    className="text-xs text-rose-500 hover:text-rose-700"
                    onClick={() => void delSecret(s.key)}
                  >
                    {t("hermes.settingsModal.skills.delete")}
                  </button>
                </div>
              </li>
            ))}
          </ul>
        )}
        <div className="flex gap-2">
          <input
            className="w-40 rounded border border-ink-200 px-2 py-1.5 text-xs font-mono"
            value={newKey}
            placeholder={t("hermes.settingsModal.model.keyName")}
            onChange={(e) => setNewKey(e.target.value)}
          />
          <input
            className="flex-1 rounded border border-ink-200 px-2 py-1.5 text-xs font-mono"
            value={newVal}
            placeholder={t("hermes.settingsModal.model.keyValue")}
            onChange={(e) => setNewVal(e.target.value)}
          />
          <button
            type="button"
            className="rounded border border-ink-200 px-3 py-1.5 text-xs hover:bg-ink-50 disabled:opacity-50"
            onClick={() => void addSecret()}
            disabled={!newKey.trim()}
          >
            {t("hermes.settingsModal.model.addKey")}
          </button>
        </div>
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
  const [available, setAvailable] = useState(true);
  const [jobs, setJobs] = useState<HermesCronJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [schedule, setSchedule] = useState("");
  const [prompt, setPrompt] = useState("");
  const [jobName, setJobName] = useState("");

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

  const add = async () => {
    if (!schedule.trim()) return;
    setError("");
    try {
      await api.createHermesCron(agentId, {
        schedule: schedule.trim(),
        prompt: prompt.trim(),
        name: jobName.trim(),
      });
      setSchedule("");
      setPrompt("");
      setJobName("");
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
