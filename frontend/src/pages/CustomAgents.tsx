/**
 * 自定义Agent module (我的团队 → 自定义Agent).
 *
 * Two views on one route family:
 *   /custom-agents      — registry list + add/edit modal (full CLI contract)
 *   /custom-agents/:id  — deliberately simple chat room (one-shot headless
 *                         turns; SSE shape shared with the Hermes chat page)
 */
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useParams } from "react-router-dom";

import { AgentManagementHeader } from "@/components/AgentPageToolbar";
import { ChatBubble, SessionDivider } from "@/components/ChatBubble";
import { useDialog } from "@/components/dialog";
import { BackIcon, EditIcon, PlusIcon, TrashIcon } from "@/components/icons";
import { EmptyState, ErrorBox, Loading, Modal } from "@/components/ui";
import {
  api,
  type CustomAgentSummary,
  type CustomAgentUpsertPayload,
} from "@/lib/api";
import { newClientMessageId } from "@/lib/chatHistory";
import { cn } from "@/lib/cn";

// ── shared bits ───────────────────────────────────────────────────────

function errText(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

interface ChatMsg {
  role: "user" | "assistant" | "system";
  content: string;
  ts?: number;
  kind?: string;
  cid: string;
}

// ── registry list + add/edit modal ────────────────────────────────────

type FormState = Required<CustomAgentUpsertPayload>;

const EMPTY_FORM: FormState = {
  name: "",
  description: "",
  spawnCommand: "",
  resumeCommand: "",
  headlessCommand: "",
  headlessResumeCommand: "",
  readyPattern: "",
  chatWorkdir: "",
};

function agentToForm(a: CustomAgentSummary): FormState {
  return {
    name: a.name,
    description: a.description,
    spawnCommand: a.spawnCommand,
    resumeCommand: a.resumeCommand,
    headlessCommand: a.headlessCommand,
    headlessResumeCommand: a.headlessResumeCommand,
    readyPattern: a.readyPattern,
    chatWorkdir: a.chatWorkdir,
  };
}

function FieldRow({
  label,
  required,
  help,
  children,
}: {
  label: string;
  required?: boolean;
  help?: string;
  children: React.ReactNode;
}) {
  return (
    <label className="block">
      <span className="mb-1 block text-sm font-medium text-ink-700">
        {label}
        {required ? <span className="ml-1 text-rose-500">*</span> : null}
      </span>
      {children}
      {help ? <p className="mt-1 text-xs leading-relaxed text-ink-400">{help}</p> : null}
    </label>
  );
}

const INPUT_CLS =
  "w-full rounded-lg border border-ink-200 bg-surface px-3 py-2 text-sm text-ink-900 " +
  "placeholder:text-ink-300 focus:border-brand-400 focus:outline-none focus:ring-2 " +
  "focus:ring-brand-100 font-mono";

function AgentFormModal({
  open,
  editing,
  onClose,
  onSaved,
}: {
  open: boolean;
  /** null → create; otherwise the agent being edited. */
  editing: CustomAgentSummary | null;
  onClose: () => void;
  onSaved: () => void;
}) {
  const { t } = useTranslation();
  const [form, setForm] = useState<FormState>(EMPTY_FORM);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");

  useEffect(() => {
    if (open) {
      setForm(editing ? agentToForm(editing) : EMPTY_FORM);
      setError("");
    }
  }, [open, editing]);

  const set = (k: keyof FormState) => (
    e: React.ChangeEvent<HTMLInputElement | HTMLTextAreaElement>,
  ) => setForm((prev) => ({ ...prev, [k]: e.target.value }));

  const save = async () => {
    if (saving) return;
    setError("");
    if (!form.name.trim()) {
      setError(t("customAgents.form.nameRequired"));
      return;
    }
    if (!form.spawnCommand.trim()) {
      setError(t("customAgents.form.spawnRequired"));
      return;
    }
    setSaving(true);
    try {
      if (editing) {
        await api.patchCustomAgent(editing.id, form);
      } else {
        await api.createCustomAgent(form);
      }
      onSaved();
      onClose();
    } catch (e) {
      setError(errText(e));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={onClose}
      title={editing ? t("customAgents.editTitle") : t("customAgents.addTitle")}
      width="max-w-2xl"
    >
      <div className="space-y-4 p-1">
        {/* CLI behaviour contract — shown up front so users know what a
            compliant agent must do BEFORE they fill anything in. */}
        <div className="rounded-lg border border-brand-200/60 bg-brand-50/40 px-4 py-3">
          <p className="text-sm font-semibold text-ink-800">
            {t("customAgents.contract.title")}
          </p>
          <ul className="mt-1.5 list-disc space-y-1 pl-5 text-xs leading-relaxed text-ink-600">
            <li>{t("customAgents.contract.item1")}</li>
            <li>{t("customAgents.contract.item2")}</li>
            <li>{t("customAgents.contract.item3")}</li>
          </ul>
        </div>

        <FieldRow
          label={t("customAgents.form.name")}
          required
        >
          <input
            className={cn(INPUT_CLS, "font-sans")}
            value={form.name}
            onChange={set("name")}
            placeholder={t("customAgents.form.namePlaceholder")}
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.spawnCommand")}
          required
          help={t("customAgents.form.spawnCommandHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.spawnCommand}
            onChange={set("spawnCommand")}
            placeholder="mycli --yolo"
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.resumeCommand")}
          help={t("customAgents.form.resumeCommandHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.resumeCommand}
            onChange={set("resumeCommand")}
            placeholder="mycli --yolo --continue"
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.headlessCommand")}
          help={t("customAgents.form.headlessCommandHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.headlessCommand}
            onChange={set("headlessCommand")}
            placeholder='mycli --yolo -p "{message}"'
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.headlessResumeCommand")}
          help={t("customAgents.form.headlessResumeCommandHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.headlessResumeCommand}
            onChange={set("headlessResumeCommand")}
            placeholder='mycli --yolo --continue -p "{message}"'
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.readyPattern")}
          help={t("customAgents.form.readyPatternHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.readyPattern}
            onChange={set("readyPattern")}
            placeholder={t("customAgents.form.readyPatternPlaceholder")}
          />
        </FieldRow>

        <FieldRow
          label={t("customAgents.form.chatWorkdir")}
          help={t("customAgents.form.chatWorkdirHelp")}
        >
          <input
            className={INPUT_CLS}
            value={form.chatWorkdir}
            onChange={set("chatWorkdir")}
            placeholder="~"
          />
        </FieldRow>

        <FieldRow label={t("customAgents.form.description")}>
          <textarea
            className={cn(INPUT_CLS, "font-sans")}
            rows={2}
            value={form.description}
            onChange={set("description")}
          />
        </FieldRow>

        {error ? <ErrorBox>{error}</ErrorBox> : null}

        <div className="flex justify-end gap-2 pb-2">
          <button
            type="button"
            className="rounded-lg border border-ink-200 px-4 py-2 text-sm font-medium text-ink-600 hover:bg-ink-50"
            onClick={onClose}
          >
            {t("common.cancel")}
          </button>
          <button
            type="button"
            className="rounded-lg bg-brand-600 px-4 py-2 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
            disabled={saving}
            onClick={() => void save()}
          >
            {saving ? t("common.saving") : t("common.save")}
          </button>
        </div>
      </div>
    </Modal>
  );
}

function Badge({
  tone,
  children,
}: {
  tone: "ok" | "warn" | "muted";
  children: React.ReactNode;
}) {
  return (
    <span
      className={cn(
        "inline-flex items-center rounded-full px-2 py-0.5 text-[11px] font-medium",
        tone === "ok" && "bg-emerald-50 text-emerald-700 dark:bg-emerald-500/10 dark:text-emerald-400",
        tone === "warn" && "bg-rose-50 text-rose-700 dark:bg-rose-500/10 dark:text-rose-400",
        tone === "muted" && "bg-ink-100 text-ink-500",
      )}
    >
      {children}
    </span>
  );
}

function CustomAgentList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { confirm, alert } = useDialog();
  const [items, setItems] = useState<CustomAgentSummary[] | null>(null);
  const [error, setError] = useState("");
  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<CustomAgentSummary | null>(null);

  const reload = useCallback(async () => {
    try {
      const res = await api.listCustomAgents();
      setItems(res.items);
      setError("");
    } catch (e) {
      setError(errText(e));
      setItems([]);
    }
  }, []);

  useEffect(() => {
    void reload();
  }, [reload]);

  const onDelete = async (agent: CustomAgentSummary) => {
    if (!(await confirm(t("customAgents.deleteConfirm", { name: agent.name }), { danger: true }))) {
      return;
    }
    try {
      const res = await api.deleteCustomAgent(agent.id);
      if (res.referencedFlows.length > 0) {
        void alert(
          t("customAgents.deleteReferencedWarn", {
            flows: res.referencedFlows.map((f) => f.name).join("、"),
          }),
        );
      }
    } catch (e) {
      void alert(errText(e));
    }
    void reload();
  };

  return (
    <div className="mx-auto max-w-6xl space-y-4 px-6 py-6">
      <AgentManagementHeader
        title={t("customAgents.title")}
        description={t("customAgents.pageNote")}
        actions={
          <button
            type="button"
            className="inline-flex items-center gap-1.5 rounded-lg bg-brand-600 px-3.5 py-2 text-sm font-semibold text-white hover:bg-brand-700"
            onClick={() => {
              setEditing(null);
              setModalOpen(true);
            }}
          >
            <PlusIcon className="h-4 w-4" />
            {t("customAgents.add")}
          </button>
        }
      />

      {error ? <ErrorBox>{error}</ErrorBox> : null}
      {items === null ? (
        <Loading />
      ) : items.length === 0 ? (
        <EmptyState
          title={t("customAgents.emptyTitle")}
          hint={t("customAgents.emptyHint")}
        />
      ) : (
        <div className="grid grid-cols-1 gap-4 md:grid-cols-2 xl:grid-cols-3">
          {items.map((a) => (
            <div
              key={a.id}
              className="flex flex-col rounded-2xl border border-ink-200/70 bg-surface p-4 shadow-sm"
            >
              <div className="flex items-start justify-between gap-2">
                <div className="min-w-0">
                  <div className="truncate text-base font-semibold text-ink-900">{a.name}</div>
                  <div className="truncate text-xs text-ink-400">{a.id}</div>
                </div>
                <div className="flex shrink-0 flex-wrap justify-end gap-1">
                  {a.chatAvailable ? (
                    <Badge tone="ok">{t("customAgents.badgeChatReady")}</Badge>
                  ) : (
                    <Badge tone="muted">{t("customAgents.badgeChatNotConfigured")}</Badge>
                  )}
                </div>
              </div>
              {a.description ? (
                <p className="mt-2 line-clamp-2 text-sm text-ink-500">{a.description}</p>
              ) : null}
              <code className="mt-2 truncate rounded bg-ink-50 px-2 py-1 text-xs text-ink-600">
                {a.spawnCommand}
              </code>
              <div className="mt-3 flex items-center justify-end gap-1 border-t border-ink-100 pt-3">
                <button
                  type="button"
                  className="rounded-lg bg-brand-600 px-3 py-1.5 text-xs font-semibold text-white hover:bg-brand-700"
                  onClick={() => navigate(`/custom-agents/${a.id}`)}
                >
                  {t("customAgents.chat")}
                </button>
                <button
                  type="button"
                  title={t("common.edit")}
                  className="rounded-lg p-1.5 text-ink-500 hover:bg-ink-50 hover:text-ink-800"
                  onClick={() => {
                    setEditing(a);
                    setModalOpen(true);
                  }}
                >
                  <EditIcon className="h-4 w-4" />
                </button>
                <button
                  type="button"
                  title={t("common.delete")}
                  className="rounded-lg p-1.5 text-ink-500 hover:bg-rose-50 hover:text-rose-600"
                  onClick={() => void onDelete(a)}
                >
                  <TrashIcon className="h-4 w-4" />
                </button>
              </div>
            </div>
          ))}
        </div>
      )}

      <AgentFormModal
        open={modalOpen}
        editing={editing}
        onClose={() => setModalOpen(false)}
        onSaved={() => void reload()}
      />
    </div>
  );
}

// ── chat room ─────────────────────────────────────────────────────────

function serverMsgToChatMsg(m: {
  role: string;
  content: string;
  ts?: number;
  id?: number;
  kind?: string;
}): ChatMsg {
  return {
    role: (m.role as ChatMsg["role"]) || "assistant",
    content: m.content,
    ts: m.ts,
    kind: m.kind,
    cid: m.id != null ? `srv-${m.id}` : newClientMessageId(),
  };
}

function CustomAgentChatRoom({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { confirm } = useDialog();
  const [agent, setAgent] = useState<CustomAgentSummary | null>(null);
  const [loadError, setLoadError] = useState("");
  const [messages, setMessages] = useState<ChatMsg[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const abortRef = useRef<AbortController | null>(null);
  const bottomRef = useRef<HTMLDivElement>(null);
  const pollTimerRef = useRef<number | null>(null);

  const scrollToBottom = useCallback(() => {
    window.setTimeout(
      () => bottomRef.current?.scrollIntoView({ behavior: "smooth" }),
      50,
    );
  }, []);

  const loadHistory = useCallback(async () => {
    const hist = await api.getCustomAgentChatHistory(agentId);
    setMessages(hist.messages.map(serverMsgToChatMsg));
  }, [agentId]);

  // Poll the status endpoint until a reconnected in-flight turn completes,
  // then swap the pending bubble for the persisted history.
  const pollUntilDone = useCallback(() => {
    const tick = async () => {
      try {
        const st = await api.getCustomAgentChatStatus(agentId);
        if (st.status === "running") {
          pollTimerRef.current = window.setTimeout(() => void tick(), 2000);
          return;
        }
        setSending(false);
        if (st.status === "error" && st.error) {
          setError(t("customAgents.chatError", { message: st.error }));
        }
        await loadHistory();
      } catch {
        setSending(false);
      }
    };
    void tick();
  }, [agentId, loadHistory, t]);

  useEffect(() => {
    let cancelled = false;
    (async () => {
      try {
        const [a] = await Promise.all([api.getCustomAgent(agentId), loadHistory()]);
        if (cancelled) return;
        setAgent(a);
        const st = await api.getCustomAgentChatStatus(agentId);
        if (cancelled) return;
        if (st.status === "running") {
          // Reconnect after a tab switch / refresh: show a pending bubble.
          setSending(true);
          setMessages((prev) => [
            ...prev,
            { role: "assistant", content: "", cid: newClientMessageId() },
          ]);
          pollUntilDone();
        }
      } catch (e) {
        if (!cancelled) setLoadError(errText(e));
      }
    })();
    return () => {
      cancelled = true;
      abortRef.current?.abort();
      if (pollTimerRef.current != null) window.clearTimeout(pollTimerRef.current);
    };
  }, [agentId, loadHistory, pollUntilDone]);

  const send = async () => {
    const message = input.trim();
    if (!message || sending || !agent) return;
    setInput("");
    setError("");
    setSending(true);
    setMessages((prev) => [
      ...prev,
      { role: "user", content: message, ts: Date.now(), cid: newClientMessageId() },
      { role: "assistant", content: "", cid: newClientMessageId() },
    ]);
    scrollToBottom();
    const controller = new AbortController();
    abortRef.current = controller;
    let streamErr = "";
    let aborted = false;
    try {
      const res = await api.chatWithCustomAgent(
        agentId,
        { message },
        { signal: controller.signal },
      );
      if (!res.ok || !res.body) {
        let detail = `HTTP ${res.status}`;
        try {
          const body = (await res.json()) as { message?: string };
          if (body.message) detail = body.message;
        } catch {
          /* non-JSON error body */
        }
        throw new Error(detail);
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
            const obj = JSON.parse(data) as { delta?: string; error?: string };
            if (obj.error) {
              streamErr = obj.error;
            } else if (typeof obj.delta === "string") {
              setMessages((prev) => {
                const next = [...prev];
                next[next.length - 1] = {
                  ...next[next.length - 1],
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
      if (streamErr) setError(t("customAgents.chatError", { message: streamErr }));
    } catch (e) {
      if (controller.signal.aborted) {
        aborted = true;
      } else {
        streamErr = errText(e);
        setError(t("customAgents.chatError", { message: streamErr }));
      }
    } finally {
      abortRef.current = null;
      setSending(false);
      if (aborted) {
        setMessages((prev) => {
          const next = [...prev];
          const last = next[next.length - 1];
          if (last && last.role === "assistant" && !last.content) {
            next[next.length - 1] = {
              ...last,
              content: t("chat.stopped"),
              ts: Date.now(),
            };
          }
          return next;
        });
      } else {
        // Reconcile with the persisted transcript (authoritative ids/ts; also
        // drops the empty pending bubble after an error turn).
        try {
          await loadHistory();
        } catch {
          /* keep the streamed view */
        }
      }
      scrollToBottom();
    }
  };

  const stop = async () => {
    try {
      await api.stopCustomAgentChat(agentId);
    } catch {
      /* best-effort */
    }
    abortRef.current?.abort();
  };

  const reset = async () => {
    if (!(await confirm(t("customAgents.resetConfirm")))) return;
    try {
      await api.resetCustomAgentChat(agentId);
      await loadHistory();
      setError("");
    } catch (e) {
      setError(errText(e));
    }
  };

  if (loadError) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-6">
        <ErrorBox>{loadError}</ErrorBox>
      </div>
    );
  }
  if (!agent) {
    return (
      <div className="mx-auto max-w-3xl px-6 py-6">
        <Loading />
      </div>
    );
  }

  const chatDisabled = !agent.chatAvailable;

  return (
    <div className="mx-auto flex h-full max-w-3xl flex-col px-6 py-4">
      {/* header */}
      <div className="flex items-center justify-between border-b border-ink-100 pb-3">
        <div className="flex min-w-0 items-center gap-2">
          <button
            type="button"
            title={t("common.back")}
            className="rounded-lg p-1.5 text-ink-500 hover:bg-ink-50 hover:text-ink-800"
            onClick={() => navigate("/custom-agents")}
          >
            <BackIcon className="h-5 w-5" />
          </button>
          <div className="min-w-0">
            <div className="truncate text-base font-semibold text-ink-900">{agent.name}</div>
            <div className="truncate text-xs text-ink-400">
              {agent.chatWorkdir || "~"}
            </div>
          </div>
        </div>
        <button
          type="button"
          className="rounded-lg border border-ink-200 px-3 py-1.5 text-xs font-medium text-ink-600 hover:bg-ink-50"
          onClick={() => void reset()}
        >
          {t("chat.reset")}
        </button>
      </div>

      {/* messages */}
      <div className="flex-1 space-y-3 overflow-y-auto py-4">
        {chatDisabled ? (
          <EmptyState
            title={t("customAgents.chatNotConfiguredTitle")}
            hint={t("customAgents.chatNotConfiguredHint")}
          />
        ) : messages.length === 0 && !sending ? (
          <EmptyState
            title={t("customAgents.chatEmptyTitle")}
            hint={t("customAgents.chatEmptyHint", { name: agent.name })}
          />
        ) : null}
        {messages.map((m, i) =>
          m.kind === "session_divider" ? (
            <SessionDivider key={m.cid} label={t("chat.sessionDivider")} />
          ) : (
            <ChatBubble
              key={m.cid}
              msg={m}
              pending={
                sending && i === messages.length - 1 && m.role === "assistant" && !m.content
              }
              noTextReply={t("chat.noTextReply")}
            />
          ),
        )}
        <div ref={bottomRef} />
      </div>

      {error ? <ErrorBox>{error}</ErrorBox> : null}

      {/* composer */}
      <div className="mt-2 flex items-end gap-2 border-t border-ink-100 pt-3">
        <textarea
          className="max-h-40 min-h-[44px] flex-1 resize-y rounded-xl border border-ink-200 bg-surface px-3 py-2.5 text-sm text-ink-900 placeholder:text-ink-300 focus:border-brand-400 focus:outline-none focus:ring-2 focus:ring-brand-100 disabled:opacity-50"
          rows={1}
          value={input}
          disabled={chatDisabled}
          placeholder={
            chatDisabled
              ? t("customAgents.chatDisabledPlaceholder")
              : t("chat.inputPlaceholder")
          }
          onChange={(e) => setInput(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
        />
        {sending ? (
          <button
            type="button"
            className="rounded-xl border border-rose-200 px-4 py-2.5 text-sm font-semibold text-rose-600 hover:bg-rose-50"
            onClick={() => void stop()}
          >
            {t("chat.stop")}
          </button>
        ) : (
          <button
            type="button"
            className="rounded-xl bg-brand-600 px-4 py-2.5 text-sm font-semibold text-white hover:bg-brand-700 disabled:opacity-50"
            disabled={chatDisabled || !input.trim()}
            onClick={() => void send()}
          >
            {t("chat.send")}
          </button>
        )}
      </div>
    </div>
  );
}

// ── route entry ───────────────────────────────────────────────────────

export function CustomAgents() {
  const { id } = useParams<{ id: string }>();
  if (id) return <CustomAgentChatRoom agentId={id} />;
  return <CustomAgentList />;
}
