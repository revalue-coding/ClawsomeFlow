/** Hermes Agent management + direct chat ("My Team" → Hermes Agent).
 *
 * Mirrors the OpenClaw chat page but for managed Hermes agents (= Hermes
 * profiles). Differences: permanent delete only, a "Claim existing" action,
 * a per-chat working-directory picker, and a "my-profile" button (opens the
 * profile root). No Import&Optimize / Agent Store / "to Hermes" button.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import {
  Card,
  CardTitle,
  EmptyState,
  ErrorBox,
  Loading,
  Modal,
} from "@/components/ui";
import {
  api,
  ApiError,
  type ChatHistoryMessage,
  type HermesAgentSummary,
  type HermesCronJob,
  type HermesModelSetting,
  type HermesSecretSetting,
  type HermesSkillSetting,
  type OpenclawTeam,
} from "@/lib/api";

function isRemoteBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
}

function deriveId(name: string): string {
  return Array.from(name.toLowerCase())
    .filter((c) => /[a-z0-9]/.test(c))
    .join("");
}

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
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

  useEffect(() => {
    let alive = true;
    api
      .getHermesRuntimeStatus()
      .then((s) => {
        if (!alive) return;
        setRunning(s.running);
        setReason(s.reason);
      })
      .catch(() => {
        if (!alive) return;
        setRunning(false);
        setReason("");
      });
    return () => {
      alive = false;
    };
  }, []);

  if (running === null) return <Loading label={t("common.loading")} />;
  if (!running) {
    return (
      <div className="mx-auto max-w-2xl py-16">
        <EmptyState
          title={t("hermes.notInstalledTitle")}
          hint={`${t("hermes.notInstalled")}${reason ? `\n\n${reason}` : ""}`}
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
  const [showCreate, setShowCreate] = useState(false);
  const [showClaim, setShowClaim] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<HermesAgentSummary | null>(null);

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
    <div className="mx-auto max-w-5xl py-8 px-4">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">{t("hermes.title")}</h1>
          <p className="text-sm text-ink-500">{t("hermes.pickerTitle")}</p>
        </div>
        <div className="flex gap-2">
          <button
            type="button"
            className="rounded border border-ink-200 px-3 py-2 text-sm hover:bg-ink-50"
            onClick={() => void reload()}
          >
            {t("hermes.refresh")}
          </button>
          <button
            type="button"
            className="rounded border border-ink-200 px-3 py-2 text-sm hover:bg-ink-50"
            onClick={() => setShowClaim(true)}
          >
            {t("hermes.claimAgent")}
          </button>
          <button
            type="button"
            className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white hover:bg-brand-700"
            onClick={() => setShowCreate(true)}
          >
            {t("hermes.createAgent")}
          </button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {loading ? (
        <Loading label={t("common.loading")} />
      ) : agents.length === 0 ? (
        <EmptyState title={t("hermes.title")} hint={t("hermes.listEmpty")} />
      ) : (
        <div className="space-y-8">
          {grouped.map(([team, list]) => (
            <div key={team}>
              <div className="text-xs font-semibold uppercase tracking-wide text-ink-400 mb-2">
                {t("hermes.team")}: {team}
              </div>
              <div className="grid grid-cols-1 gap-3 sm:grid-cols-2 lg:grid-cols-3">
                {list.map((a) => (
                  <Card key={a.id} className="flex flex-col gap-2">
                    <div className="flex items-start justify-between">
                      <CardTitle>{a.name}</CardTitle>
                      <button
                        type="button"
                        className="text-xs text-rose-500 hover:text-rose-700"
                        onClick={() => setRemoveTarget(a)}
                      >
                        {t("hermes.removeAgent")}
                      </button>
                    </div>
                    <code className="text-xs text-ink-400">{a.id}</code>
                    {a.description && (
                      <p className="text-sm text-ink-600 line-clamp-3">{a.description}</p>
                    )}
                    <button
                      type="button"
                      className="mt-auto self-start rounded bg-ink-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-ink-700"
                      onClick={() => navigate(`/hermes/${a.id}`)}
                    >
                      {t("hermes.open")}
                    </button>
                  </Card>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {showCreate && (
        <CreateModal
          teams={teams}
          onClose={() => setShowCreate(false)}
          onDone={(created) => {
            setShowCreate(false);
            void reload();
            navigate(`/hermes/${created.id}`);
          }}
        />
      )}
      {showClaim && (
        <ClaimModal
          teams={teams}
          onClose={() => setShowClaim(false)}
          onDone={() => {
            setShowClaim(false);
            void reload();
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

function TeamSelect({
  teams,
  value,
  onChange,
}: {
  teams: OpenclawTeam[];
  value: string;
  onChange: (v: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <select
      className="w-full rounded border border-ink-200 px-3 py-2 text-sm"
      value={value}
      onChange={(e) => onChange(e.target.value)}
    >
      <option value="">{t("hermes.ungrouped")}</option>
      {teams.map((tm) => (
        <option key={tm.id} value={tm.id}>
          {tm.name}
        </option>
      ))}
    </select>
  );
}

function CreateModal({
  teams,
  onClose,
  onDone,
}: {
  teams: OpenclawTeam[];
  onClose: () => void;
  onDone: (created: { id: string }) => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [responsibility, setResponsibility] = useState("");
  const [id, setId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");

  const effectiveId = id.trim() || deriveId(name);

  const submit = async () => {
    setBusy(true);
    setError("");
    try {
      const created = await api.createHermesAgent({
        id: id.trim() || undefined,
        name: name.trim(),
        responsibility: responsibility.trim(),
        teamId: teamId || undefined,
      });
      onDone(created);
    } catch (e) {
      setError(errText(e));
      setBusy(false);
    }
  };

  return (
    <Modal open onClose={onClose} title={t("hermes.create.title")} dismissible={!busy}>
      <div className="space-y-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.name")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            value={name}
            placeholder={t("hermes.create.namePlaceholder")}
            onChange={(e) => setName(e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.responsibility")}</span>
          <textarea
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm"
            rows={4}
            value={responsibility}
            placeholder={t("hermes.create.responsibilityPlaceholder")}
            onChange={(e) => setResponsibility(e.target.value)}
          />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.idLabel")}</span>
          <input
            className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono"
            value={id}
            placeholder={effectiveId}
            onChange={(e) => setId(e.target.value)}
          />
          <span className="mt-1 block text-xs text-ink-400">{t("hermes.create.idHint")}</span>
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.teamLabel")}</span>
          <div className="mt-1">
            <TeamSelect teams={teams} value={teamId} onChange={setTeamId} />
          </div>
        </label>
        <div className="flex justify-end gap-2 pt-2">
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
            className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => void submit()}
            disabled={busy || !name.trim() || !effectiveId}
          >
            {busy ? t("hermes.create.creating") : t("hermes.create.submit")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function ClaimModal({
  teams,
  onClose,
  onDone,
}: {
  teams: OpenclawTeam[];
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const [candidates, setCandidates] = useState<{ id: string; description: string }[]>([]);
  const [loading, setLoading] = useState(true);
  const [teamId, setTeamId] = useState("");
  const [busyId, setBusyId] = useState("");
  const [error, setError] = useState("");

  useEffect(() => {
    api
      .listHermesClaimable()
      .then((r) => setCandidates(r.items))
      .catch((e) => setError(errText(e)))
      .finally(() => setLoading(false));
  }, []);

  const claim = async (cid: string) => {
    setBusyId(cid);
    setError("");
    try {
      await api.claimHermesAgent({ id: cid, teamId: teamId || undefined });
      onDone();
    } catch (e) {
      setError(errText(e));
      setBusyId("");
    }
  };

  return (
    <Modal open onClose={onClose} title={t("hermes.claim.title")}>
      <div className="space-y-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        <TeamSelect teams={teams} value={teamId} onChange={setTeamId} />
        {loading ? (
          <Loading label={t("common.loading")} />
        ) : candidates.length === 0 ? (
          <p className="text-sm text-ink-500">{t("hermes.claim.empty")}</p>
        ) : (
          <ul className="divide-y divide-ink-100 rounded border border-ink-100">
            {candidates.map((c) => (
              <li key={c.id} className="flex items-center justify-between px-3 py-2">
                <div>
                  <code className="text-sm">{c.id}</code>
                  {c.description && (
                    <p className="text-xs text-ink-400 line-clamp-1">{c.description}</p>
                  )}
                </div>
                <button
                  type="button"
                  className="rounded bg-brand-600 px-3 py-1.5 text-xs font-medium text-white disabled:opacity-50"
                  onClick={() => void claim(c.id)}
                  disabled={!!busyId}
                >
                  {t("hermes.claim.submit")}
                </button>
              </li>
            ))}
          </ul>
        )}
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
  const [profileRoot, setProfileRoot] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [workdir, setWorkdir] = useState(
    () => localStorage.getItem(`hermes-workdir-${agentId}`) ?? "",
  );
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [opening, setOpening] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [detail, hist] = await Promise.all([
          api.getHermesAgent(agentId),
          api.getHermesAgentChatHistory(agentId),
        ]);
        setName(detail.name);
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
      if (out.path) {
        setWorkdir(out.path);
        localStorage.setItem(`hermes-workdir-${agentId}`, out.path);
      }
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
    try {
      await api.resetHermesAgentChat(agentId);
      setMessages([]);
    } catch (e) {
      setError(errText(e));
    }
  };

  return (
    <div className="mx-auto flex h-[calc(100vh-4rem)] max-w-4xl flex-col py-4 px-4">
      <div className="mb-3 flex items-center justify-between border-b border-ink-100 pb-3">
        <div className="flex items-center gap-3">
          <button
            type="button"
            className="text-sm text-ink-500 hover:text-ink-800"
            onClick={() => navigate("/hermes")}
          >
            ← {t("hermes.back")}
          </button>
          <h2 className="text-lg font-semibold text-ink-900">{name}</h2>
          <code className="text-xs text-ink-400">{agentId}</code>
        </div>
        <div className="flex items-center gap-2">
          <button
            type="button"
            className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50 disabled:opacity-50"
            onClick={() => void openProfile()}
            disabled={opening || !profileRoot}
          >
            {opening ? t("hermes.opening") : t("hermes.myProfile")}
          </button>
          <button
            type="button"
            className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50"
            onClick={() => setShowSettings(true)}
          >
            {t("hermes.settings")}
          </button>
          <button
            type="button"
            className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50"
            onClick={() => void reset()}
          >
            {t("hermes.reset")}
          </button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto py-2">
        {messages.map((m, i) => (
          <div
            key={i}
            className={
              m.role === "user"
                ? "ml-auto max-w-[80%] rounded-lg bg-brand-600 px-3 py-2 text-sm text-white"
                : "mr-auto max-w-[80%] rounded-lg bg-ink-100 px-3 py-2 text-sm text-ink-900 whitespace-pre-wrap"
            }
          >
            {m.content || (m.role === "assistant" && sending ? "…" : "")}
          </div>
        ))}
      </div>

      <div className="mt-2 border-t border-ink-100 pt-3">
        <div className="mb-2 flex items-center gap-2 text-xs text-ink-500">
          <button
            type="button"
            className="rounded border border-ink-200 px-2 py-1 hover:bg-ink-50"
            onClick={() => void pickWorkdir()}
          >
            {t("hermes.pickWorkdir")}
          </button>
          <span className="truncate font-mono">{workdir || t("hermes.workdirNeeded")}</span>
        </div>
        <div className="flex gap-2">
          <textarea
            className="flex-1 resize-none rounded border border-ink-200 px-3 py-2 text-sm"
            rows={2}
            value={input}
            placeholder={t("hermes.messagePlaceholder")}
            onChange={(e) => setInput(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send();
            }}
            disabled={sending}
          />
          <button
            type="button"
            className="rounded bg-brand-600 px-4 text-sm font-medium text-white disabled:opacity-50"
            onClick={() => void send()}
            disabled={sending || !input.trim()}
          >
            {sending ? t("hermes.sending") : t("hermes.send")}
          </button>
        </div>
      </div>

      {showSettings && (
        <SettingsModal agentId={agentId} onClose={() => setShowSettings(false)} />
      )}
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
