/** Management + direct chat for env-home managed agents (Claude Code / Codex / Cursor).
 *
 * One generic component parametrized by `kind`; mounted at /claude and /codex.
 * Identity/skills/MCP live in the agent's relocatable config home (injected at
 * spawn via a ClawTeam profile). A per-chat working-directory picker sets the cwd;
 * "open config home" opens the config dir.
 */
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useParams } from "react-router-dom";
import { useTranslation } from "react-i18next";

import { Card, CardTitle, EmptyState, ErrorBox, Loading, Modal } from "@/components/ui";
import {
  api,
  ApiError,
  type ChatHistoryMessage,
  type ManagedAgentSummary,
  type ManagedKind,
  type ManagedMcpServer,
  type ManagedSkill,
  type OpenclawTeam,
} from "@/lib/api";

const KIND_LABEL: Record<ManagedKind, string> = {
  claude: "Claude Code",
  codex: "Codex",
  cursor: "Cursor",
};

function isRemoteBrowser(): boolean {
  if (typeof window === "undefined") return false;
  const h = window.location.hostname;
  return h !== "localhost" && h !== "127.0.0.1" && h !== "::1" && h !== "";
}

function deriveId(name: string): string {
  return Array.from(name.toLowerCase())
    .filter((c) => /[a-z0-9-]/.test(c))
    .join("")
    .replace(/^-+|-+$/g, "");
}

function errText(e: unknown): string {
  if (e instanceof ApiError) return e.message;
  if (e instanceof Error) return e.message;
  return String(e);
}

export function ManagedChat({ kind }: { kind: ManagedKind }) {
  const { id } = useParams();
  const { t } = useTranslation();
  const [running, setRunning] = useState<boolean | null>(null);
  const [reason, setReason] = useState("");

  useEffect(() => {
    let alive = true;
    api.getManagedRuntimeStatus(kind)
      .then((s) => { if (alive) { setRunning(s.running); setReason(s.reason); } })
      .catch(() => { if (alive) { setRunning(false); setReason(""); } });
    return () => { alive = false; };
  }, [kind]);

  if (running === null) return <Loading label={t("common.loading")} />;
  if (!running) {
    return (
      <div className="mx-auto max-w-2xl py-16">
        <EmptyState
          title={t("managed.notInstalledTitle", { platform: KIND_LABEL[kind] })}
          hint={`${t("managed.notInstalled", { platform: KIND_LABEL[kind] })}${reason ? `\n\n${reason}` : ""}`}
        />
      </div>
    );
  }
  return id ? <ChatRoom kind={kind} agentId={id} /> : <Picker kind={kind} />;
}

function basePath(kind: ManagedKind): string {
  return kind === "claude" ? "/claude" : kind === "codex" ? "/codex" : "/cursor";
}

// ── Picker ─────────────────────────────────────────────────────────────

function Picker({ kind }: { kind: ManagedKind }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [agents, setAgents] = useState<ManagedAgentSummary[]>([]);
  const [teams, setTeams] = useState<OpenclawTeam[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [showCreate, setShowCreate] = useState(false);
  const [removeTarget, setRemoveTarget] = useState<ManagedAgentSummary | null>(null);

  const reload = useCallback(async () => {
    setLoading(true); setError("");
    try {
      const [a, tm] = await Promise.all([api.listManagedAgents(kind), api.listOpenclawTeams()]);
      setAgents(a.items); setTeams(tm.items);
    } catch (e) { setError(errText(e)); } finally { setLoading(false); }
  }, [kind]);

  useEffect(() => { void reload(); }, [reload]);

  const grouped = useMemo(() => {
    const m = new Map<string, ManagedAgentSummary[]>();
    for (const a of agents) {
      const key = a.teamName || t("hermes.ungrouped");
      (m.get(key) ?? m.set(key, []).get(key)!).push(a);
    }
    return Array.from(m.entries());
  }, [agents, t]);

  return (
    <div className="mx-auto max-w-5xl py-8 px-4">
      <div className="flex items-center justify-between mb-6">
        <div>
          <h1 className="text-2xl font-semibold text-ink-900">
            {t("managed.title", { platform: KIND_LABEL[kind] })}
          </h1>
          <p className="text-sm text-ink-500">{t("managed.pickerTitle", { platform: KIND_LABEL[kind] })}</p>
        </div>
        <div className="flex gap-2">
          <button type="button" className="rounded border border-ink-200 px-3 py-2 text-sm hover:bg-ink-50" onClick={() => void reload()}>
            {t("hermes.refresh")}
          </button>
          <button type="button" className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white hover:bg-brand-700" onClick={() => setShowCreate(true)}>
            {t("hermes.createAgent")}
          </button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}
      {loading ? (
        <Loading label={t("common.loading")} />
      ) : agents.length === 0 ? (
        <EmptyState title={t("managed.title", { platform: KIND_LABEL[kind] })} hint={t("managed.listEmpty")} />
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
                      <button type="button" className="text-xs text-rose-500 hover:text-rose-700" onClick={() => setRemoveTarget(a)}>
                        {t("hermes.removeAgent")}
                      </button>
                    </div>
                    <code className="text-xs text-ink-400">{a.id}</code>
                    {a.description && <p className="text-sm text-ink-600 line-clamp-3">{a.description}</p>}
                    <button type="button" className="mt-auto self-start rounded bg-ink-900 px-3 py-1.5 text-xs font-medium text-white hover:bg-ink-700" onClick={() => navigate(`${basePath(kind)}/${a.id}`)}>
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
        <CreateModal kind={kind} teams={teams} onClose={() => setShowCreate(false)} onDone={(c) => { setShowCreate(false); void reload(); navigate(`${basePath(kind)}/${c.id}`); }} />
      )}
      {removeTarget && (
        <RemoveModal agent={removeTarget} onClose={() => setRemoveTarget(null)} onDone={() => { setRemoveTarget(null); void reload(); }} />
      )}
    </div>
  );
}

function TeamSelect({ teams, value, onChange }: { teams: OpenclawTeam[]; value: string; onChange: (v: string) => void }) {
  const { t } = useTranslation();
  return (
    <select className="w-full rounded border border-ink-200 px-3 py-2 text-sm" value={value} onChange={(e) => onChange(e.target.value)}>
      <option value="">{t("hermes.ungrouped")}</option>
      {teams.map((tm) => <option key={tm.id} value={tm.id}>{tm.name}</option>)}
    </select>
  );
}

function CreateModal({ kind, teams, onClose, onDone }: { kind: ManagedKind; teams: OpenclawTeam[]; onClose: () => void; onDone: (c: { id: string }) => void }) {
  const { t } = useTranslation();
  const [name, setName] = useState("");
  const [responsibility, setResponsibility] = useState("");
  const [id, setId] = useState("");
  const [teamId, setTeamId] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const effectiveId = id.trim() || deriveId(name);

  const submit = async () => {
    setBusy(true); setError("");
    try {
      const created = await api.createManagedAgent({ kind, id: id.trim() || undefined, name: name.trim(), responsibility: responsibility.trim(), teamId: teamId || undefined });
      onDone(created);
    } catch (e) { setError(errText(e)); setBusy(false); }
  };

  return (
    <Modal open onClose={onClose} title={t("managed.create.title", { platform: KIND_LABEL[kind] })} dismissible={!busy}>
      <div className="space-y-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.name")}</span>
          <input className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm" value={name} placeholder={t("hermes.create.namePlaceholder")} onChange={(e) => setName(e.target.value)} />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.responsibility")}</span>
          <textarea className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm" rows={4} value={responsibility} placeholder={t("hermes.create.responsibilityPlaceholder")} onChange={(e) => setResponsibility(e.target.value)} />
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.idLabel")}</span>
          <input className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono" value={id} placeholder={effectiveId} onChange={(e) => setId(e.target.value)} />
          <span className="mt-1 block text-xs text-ink-400">{t("managed.create.idHint")}</span>
        </label>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.create.teamLabel")}</span>
          <div className="mt-1"><TeamSelect teams={teams} value={teamId} onChange={setTeamId} /></div>
        </label>
        <div className="flex justify-end gap-2 pt-2">
          <button type="button" className="rounded border border-ink-200 px-3 py-2 text-sm" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button type="button" className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50" onClick={() => void submit()} disabled={busy || !name.trim() || !effectiveId}>
            {busy ? t("hermes.create.creating") : t("hermes.create.submit")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function RemoveModal({ agent, onClose, onDone }: { agent: ManagedAgentSummary; onClose: () => void; onDone: () => void }) {
  const { t } = useTranslation();
  const [confirm, setConfirm] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const remove = async () => {
    setBusy(true); setError("");
    try { await api.deleteManagedAgent(agent.id); onDone(); } catch (e) { setError(errText(e)); setBusy(false); }
  };
  return (
    <Modal open onClose={onClose} title={t("managed.remove.title")} dismissible={!busy}>
      <div className="space-y-4">
        {error && <ErrorBox>{error}</ErrorBox>}
        <p className="text-sm text-rose-600">{t("managed.remove.warning")}</p>
        <label className="block text-sm">
          <span className="text-ink-600">{t("hermes.remove.confirmLabel")}</span>
          <input className="mt-1 w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono" value={confirm} placeholder={agent.id} onChange={(e) => setConfirm(e.target.value)} />
        </label>
        <div className="flex justify-end gap-2">
          <button type="button" className="rounded border border-ink-200 px-3 py-2 text-sm" onClick={onClose} disabled={busy}>{t("common.cancel")}</button>
          <button type="button" className="rounded bg-rose-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50" onClick={() => void remove()} disabled={busy || confirm.trim() !== agent.id}>
            {busy ? t("hermes.remove.deleting") : t("hermes.remove.submit")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

// ── Chat room ───────────────────────────────────────────────────────────

interface ChatMsg { role: "user" | "assistant" | "system"; content: string }

function ChatRoom({ kind, agentId }: { kind: ManagedKind; agentId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [name, setName] = useState(agentId);
  const [configHome, setConfigHome] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [workdir, setWorkdir] = useState(() => localStorage.getItem(`managed-workdir-${agentId}`) ?? "");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [showSettings, setShowSettings] = useState(false);
  const [opening, setOpening] = useState(false);
  const scrollRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    void (async () => {
      try {
        const [detail, hist] = await Promise.all([api.getManagedAgent(agentId), api.getManagedAgentChatHistory(agentId)]);
        setName(detail.name); setConfigHome(detail.configHome);
        setMessages(hist.messages.map((m: ChatHistoryMessage) => ({ role: m.role, content: m.content })));
      } catch (e) { setError(errText(e)); }
    })();
  }, [agentId]);

  useEffect(() => { scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight }); }, [messages]);

  const pickWorkdir = async () => {
    if (isRemoteBrowser()) { window.alert(t("hermes.remoteUnavailable")); return; }
    try {
      const out = await api.pickDirectory({ title: t("hermes.workdir"), initialPath: workdir || undefined });
      if (out.path) { setWorkdir(out.path); localStorage.setItem(`managed-workdir-${agentId}`, out.path); }
    } catch (e) { setError(errText(e)); }
  };

  const openHome = async () => {
    if (isRemoteBrowser()) { window.alert(t("hermes.remoteUnavailable")); return; }
    setOpening(true);
    try { await api.openDirectory({ path: configHome }); }
    catch (e) { window.alert(t("managed.openHomeFailed", { message: errText(e) })); }
    finally { setOpening(false); }
  };

  const send = async () => {
    const message = input.trim();
    if (!message) return;
    if (!workdir) { setError(t("hermes.workdirNeeded")); return; }
    setError(""); setSending(true); setInput("");
    setMessages((p) => [...p, { role: "user", content: message }, { role: "assistant", content: "" }]);
    try {
      const res = await api.chatWithManagedAgent(agentId, { message, workdir });
      if (!res.ok || !res.body) throw new Error(`HTTP ${res.status}`);
      const reader = res.body.getReader();
      const dec = new TextDecoder();
      let buf = ""; let streamErr = "";
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        buf += dec.decode(value, { stream: true });
        let idx: number;
        while ((idx = buf.indexOf("\n\n")) >= 0) {
          const block = buf.slice(0, idx); buf = buf.slice(idx + 2);
          const line = block.split("\n").find((l) => l.startsWith("data:"));
          if (!line) continue;
          const data = line.slice(5).trim();
          if (data === "[DONE]") continue;
          try {
            const obj = JSON.parse(data) as { delta?: string; error?: string };
            if (obj.error) streamErr = obj.error;
            else if (typeof obj.delta === "string") {
              setMessages((p) => {
                const next = [...p];
                next[next.length - 1] = { role: "assistant", content: next[next.length - 1].content + obj.delta };
                return next;
              });
            }
          } catch { /* ignore */ }
        }
      }
      if (streamErr) setError(t("hermes.chatError", { message: streamErr }));
    } catch (e) { setError(t("hermes.chatError", { message: errText(e) })); }
    finally { setSending(false); }
  };

  const reset = async () => {
    try { await api.resetManagedAgentChat(agentId); setMessages([]); } catch (e) { setError(errText(e)); }
  };

  return (
    <div className="mx-auto flex h-[calc(100vh-4rem)] max-w-4xl flex-col py-4 px-4">
      <div className="mb-3 flex items-center justify-between border-b border-ink-100 pb-3">
        <div className="flex items-center gap-3">
          <button type="button" className="text-sm text-ink-500 hover:text-ink-800" onClick={() => navigate(basePath(kind))}>← {t("hermes.back")}</button>
          <h2 className="text-lg font-semibold text-ink-900">{name}</h2>
          <code className="text-xs text-ink-400">{agentId}</code>
        </div>
        <div className="flex items-center gap-2">
          <button type="button" className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50 disabled:opacity-50" onClick={() => void openHome()} disabled={opening || !configHome}>
            {opening ? t("hermes.opening") : t("managed.openHome")}
          </button>
          <button type="button" className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50" onClick={() => setShowSettings(true)}>{t("hermes.settings")}</button>
          <button type="button" className="rounded border border-ink-200 px-2.5 py-1.5 text-xs hover:bg-ink-50" onClick={() => void reset()}>{t("hermes.reset")}</button>
        </div>
      </div>

      {error && <ErrorBox>{error}</ErrorBox>}

      <div ref={scrollRef} className="flex-1 space-y-3 overflow-y-auto py-2">
        {messages.map((m, i) => (
          <div key={i} className={m.role === "user" ? "ml-auto max-w-[80%] rounded-lg bg-brand-600 px-3 py-2 text-sm text-white" : "mr-auto max-w-[80%] rounded-lg bg-ink-100 px-3 py-2 text-sm text-ink-900 whitespace-pre-wrap"}>
            {m.content || (m.role === "assistant" && sending ? "…" : "")}
          </div>
        ))}
      </div>

      <div className="mt-2 border-t border-ink-100 pt-3">
        <div className="mb-2 flex items-center gap-2 text-xs text-ink-500">
          <button type="button" className="rounded border border-ink-200 px-2 py-1 hover:bg-ink-50" onClick={() => void pickWorkdir()}>{t("hermes.pickWorkdir")}</button>
          <span className="truncate font-mono">{workdir || t("hermes.workdirNeeded")}</span>
        </div>
        <div className="flex gap-2">
          <textarea className="flex-1 resize-none rounded border border-ink-200 px-3 py-2 text-sm" rows={2} value={input} placeholder={t("hermes.messagePlaceholder")} onChange={(e) => setInput(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) void send(); }} disabled={sending} />
          <button type="button" className="rounded bg-brand-600 px-4 text-sm font-medium text-white disabled:opacity-50" onClick={() => void send()} disabled={sending || !input.trim()}>
            {sending ? t("hermes.sending") : t("hermes.send")}
          </button>
        </div>
      </div>

      {showSettings && <SettingsModal kind={kind} agentId={agentId} onClose={() => setShowSettings(false)} />}
    </div>
  );
}

// ── Settings ────────────────────────────────────────────────────────────

type Tab = "role" | "mcp" | "skills";

function SettingsModal({ kind, agentId, onClose }: { kind: ManagedKind; agentId: string; onClose: () => void }) {
  const { t } = useTranslation();
  const [tab, setTab] = useState<Tab>("role");
  return (
    <Modal open onClose={onClose} title={t("managed.settingsModal.title", { platform: KIND_LABEL[kind] })} width="max-w-3xl">
      <div className="flex gap-2 border-b border-ink-100 pb-2 mb-4">
        {(["role", "mcp", "skills"] as Tab[]).map((k) => (
          <button key={k} type="button" className={tab === k ? "rounded bg-ink-900 px-3 py-1.5 text-xs font-medium text-white" : "rounded px-3 py-1.5 text-xs text-ink-600 hover:bg-ink-50"} onClick={() => setTab(k)}>
            {t(`managed.settingsModal.tabs.${k}`, k === "role" ? { file: kind === "claude" ? "CLAUDE.md" : "AGENTS.md" } : {})}
          </button>
        ))}
      </div>
      {tab === "role" && <RoleTab agentId={agentId} />}
      {tab === "mcp" && <McpTab agentId={agentId} />}
      {tab === "skills" && <SkillsTab agentId={agentId} />}
    </Modal>
  );
}

function RoleTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [content, setContent] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [saved, setSaved] = useState(false);
  const [error, setError] = useState("");
  useEffect(() => { api.getManagedRole(agentId).then((r) => setContent(r.content)).catch((e) => setError(errText(e))).finally(() => setLoading(false)); }, [agentId]);
  const save = async () => { setBusy(true); setSaved(false); setError(""); try { await api.putManagedRole(agentId, content); setSaved(true); } catch (e) { setError(errText(e)); } finally { setBusy(false); } };
  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-3">
      {error && <ErrorBox>{error}</ErrorBox>}
      <p className="text-xs text-ink-400">{t("managed.settingsModal.roleHint")}</p>
      <textarea className="h-72 w-full rounded border border-ink-200 px-3 py-2 font-mono text-xs" value={content} onChange={(e) => { setContent(e.target.value); setSaved(false); }} />
      <div className="flex items-center gap-2">
        <button type="button" className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50" onClick={() => void save()} disabled={busy}>{t("hermes.settingsModal.save")}</button>
        {saved && <span className="text-xs text-emerald-600">{t("hermes.settingsModal.saved")}</span>}
      </div>
    </div>
  );
}

function McpTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [servers, setServers] = useState<ManagedMcpServer[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [name, setName] = useState("");
  const [cmd, setCmd] = useState("");
  const reload = useCallback(async () => {
    setLoading(true);
    try { setServers(await api.getManagedMcp(agentId)); } catch (e) { setError(errText(e)); } finally { setLoading(false); }
  }, [agentId]);
  useEffect(() => { void reload(); }, [reload]);
  const add = async () => {
    if (!name.trim() || !cmd.trim()) return;
    setError("");
    try {
      await api.addManagedMcp(agentId, { name: name.trim(), command: cmd.trim().split(/\s+/) });
      setName(""); setCmd(""); await reload();
    } catch (e) { setError(errText(e)); }
  };
  const del = async (n: string) => { setError(""); try { await api.deleteManagedMcp(agentId, n); await reload(); } catch (e) { setError(errText(e)); } };
  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-3">
      {error && <ErrorBox>{error}</ErrorBox>}
      {servers.length === 0 ? <p className="text-sm text-ink-500">{t("managed.settingsModal.mcp.empty")}</p> : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {servers.map((s) => (
            <li key={s.name} className="flex items-center justify-between px-3 py-2 text-sm">
              <div className="min-w-0"><code>{s.name}</code><p className="truncate text-xs text-ink-400">{s.detail}</p></div>
              <button type="button" className="text-xs text-rose-500 hover:text-rose-700" onClick={() => void del(s.name)}>{t("hermes.settingsModal.skills.delete")}</button>
            </li>
          ))}
        </ul>
      )}
      <div className="space-y-2 border-t border-ink-100 pt-3">
        <div className="text-sm font-medium text-ink-700">{t("managed.settingsModal.mcp.add")}</div>
        <input className="w-full rounded border border-ink-200 px-3 py-2 text-sm" value={name} placeholder={t("managed.settingsModal.mcp.name")} onChange={(e) => setName(e.target.value)} />
        <input className="w-full rounded border border-ink-200 px-3 py-2 text-sm font-mono" value={cmd} placeholder={t("managed.settingsModal.mcp.command")} onChange={(e) => setCmd(e.target.value)} />
        <button type="button" className="rounded bg-brand-600 px-3 py-2 text-sm font-medium text-white disabled:opacity-50" onClick={() => void add()} disabled={!name.trim() || !cmd.trim()}>{t("managed.settingsModal.mcp.addBtn")}</button>
      </div>
    </div>
  );
}

function SkillsTab({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [skills, setSkills] = useState<ManagedSkill[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [expanded, setExpanded] = useState<string | null>(null);
  const [content, setContent] = useState("");
  useEffect(() => { api.getManagedSkills(agentId).then(setSkills).catch((e) => setError(errText(e))).finally(() => setLoading(false)); }, [agentId]);
  const view = async (n: string) => {
    if (expanded === n) { setExpanded(null); return; }
    setError("");
    try { const r = await api.getManagedSkill(agentId, n); setContent(r.content ?? ""); setExpanded(n); } catch (e) { setError(errText(e)); }
  };
  if (loading) return <Loading label={t("common.loading")} />;
  return (
    <div className="space-y-2">
      {error && <ErrorBox>{error}</ErrorBox>}
      {skills.length === 0 ? <p className="text-sm text-ink-500">{t("hermes.settingsModal.skills.empty")}</p> : (
        <ul className="divide-y divide-ink-100 rounded border border-ink-100">
          {skills.map((s) => (
            <li key={s.name} className="px-3 py-2">
              <div className="flex items-center justify-between">
                <div><code className="text-sm">{s.name}</code>{s.description && <p className="text-xs text-ink-400 line-clamp-1">{s.description}</p>}</div>
                <button type="button" className="text-xs text-ink-500 hover:text-ink-800" onClick={() => void view(s.name)}>
                  {expanded === s.name ? t("hermes.settingsModal.skills.hide") : t("hermes.settingsModal.skills.view")}
                </button>
              </div>
              {expanded === s.name && <pre className="mt-2 max-h-60 overflow-auto rounded bg-ink-50 p-2 text-xs">{content}</pre>}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
