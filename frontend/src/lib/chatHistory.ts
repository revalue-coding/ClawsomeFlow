/**
 * Persistent chat-history helper backed by ``localStorage``.
 *
 * ``OpenclawChat`` uses this so user-visible chat transcripts survive page
 * refreshes. Only the **last N** messages are kept — long sessions
 * shouldn't eat the storage quota — and only an explicit "reset" should
 * wipe the bucket.
 */

import type { ChatAttachmentMeta, ChatAttachmentRoute } from "@/lib/api";

export interface PersistedMessage {
  role: "user" | "assistant" | "system";
  content: string;
  attachments?: ChatAttachmentMeta[];
  /** Epoch ms when the message was sent/received (client clock). Optional:
   *  server-recovered messages and older cached entries have no timestamp. */
  ts?: number;
  /** Stable server id. Present once persisted; the UI keys render + dedup off it. */
  id?: number;
  /** "session_divider" for the persistent reset marker; normal messages omit it. */
  kind?: string;
  /** Stable client id for optimistic (not-yet-persisted) rows, so React keys
   *  stay stable across streaming re-renders before the server id arrives. */
  cid?: string;
}

export const HISTORY_LIMIT = 20;

/** Persistent "new session" divider row inserted by a reset (kept, not cleared). */
export const SESSION_DIVIDER_KIND = "session_divider";

export function isSessionDivider(m: PersistedMessage): boolean {
  return m.kind === SESSION_DIVIDER_KIND;
}

let _cidCounter = 0;
/** Mint a stable client id for an optimistic message. */
export function newClientMessageId(): string {
  _cidCounter += 1;
  return `local-${Date.now()}-${_cidCounter}`;
}

/** Backend marker when OpenClaw/Hermes completes a tool-only turn with no visible text. */
export const NO_TEXT_REPLY_MARKER = "[[NO_TEXT_REPLY]]";

/** Normalize assistant content from server history / turn registry snapshots. */
export function normalizeAssistantContent(content: string): string {
  const trimmed = content.trim();
  if (!trimmed || trimmed === NO_TEXT_REPLY_MARKER) return "";
  return content;
}

/** Format a chat message timestamp as a short local time (e.g. "14:05"). */
export function formatChatTime(ts?: number): string {
  if (typeof ts !== "number" || !Number.isFinite(ts)) return "";
  try {
    return new Date(ts).toLocaleTimeString(undefined, {
      hour: "2-digit",
      minute: "2-digit",
    });
  } catch {
    return "";
  }
}

function storageKey(scope: string): string {
  return `csflow:chat-history:${scope}`;
}

function normaliseAttachments(value: unknown): ChatAttachmentMeta[] | undefined {
  if (!Array.isArray(value)) return undefined;
  const out = value
    .filter((row): row is Record<string, unknown> => !!row && typeof row === "object")
    .map((row) => {
      const route: ChatAttachmentRoute = row.route === "native" ? "native" : "path_injection";
      return {
        id: typeof row.id === "string" ? row.id : "",
        name: typeof row.name === "string" ? row.name : "",
        mimeType: typeof row.mimeType === "string" ? row.mimeType : "",
        sizeBytes: typeof row.sizeBytes === "number" ? row.sizeBytes : 0,
        absolutePath: typeof row.absolutePath === "string" ? row.absolutePath : "",
        relativePath: typeof row.relativePath === "string" ? row.relativePath : "",
        route,
      };
    })
    .filter((row) => row.id && row.name && row.absolutePath && row.relativePath);
  return out.length > 0 ? out : undefined;
}

export function loadChatHistory(scope: string): PersistedMessage[] {
  try {
    const raw = localStorage.getItem(storageKey(scope));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (m): m is Record<string, unknown> =>
          !!m &&
          typeof m === "object" &&
          (m.role === "user" || m.role === "assistant" || m.role === "system") &&
          typeof m.content === "string",
      )
      .map((m) => ({
        role: m.role as PersistedMessage["role"],
        content: m.content as string,
        ts: typeof m.ts === "number" ? m.ts : undefined,
        attachments: normaliseAttachments(m.attachments),
        id: typeof m.id === "number" ? m.id : undefined,
        kind: typeof m.kind === "string" ? m.kind : undefined,
        cid: typeof m.cid === "string" ? m.cid : undefined,
      }))
      .slice(-HISTORY_LIMIT);
  } catch {
    return [];
  }
}

export function saveChatHistory(scope: string, msgs: PersistedMessage[]): void {
  try {
    const tail = msgs.slice(-HISTORY_LIMIT);
    localStorage.setItem(storageKey(scope), JSON.stringify(tail));
  } catch {
    /* localStorage disabled or quota exceeded — ignore */
  }
}

export function clearChatHistory(scope: string): void {
  try {
    localStorage.removeItem(storageKey(scope));
    localStorage.removeItem(seenKey(scope));
  } catch {
    /* ignore */
  }
}

// ── "New messages" marker ───────────────────────────────────────────────
// Remembers how many settled (non-system, non-pending) messages the user had
// already seen, so when they navigate back to the chat we can draw a divider
// above whatever arrived while they were away.

function seenKey(scope: string): string {
  return `csflow:chat-seen:${scope}`;
}

/** Count of display entries the user has "seen", excluding a trailing empty
 *  assistant placeholder (the in-flight pending bubble). Counts over the same
 *  list the transcript renders (`displayChatMessages`), which now includes
 *  persistent session-divider rows, so the transient "new messages" divider
 *  index stays aligned with the render. */
export function settledCount(msgs: PersistedMessage[]): number {
  const arr = displayChatMessages(msgs);
  let n = arr.length;
  const last = arr[n - 1];
  if (last && last.role === "assistant" && last.content.trim() === "") n -= 1;
  return n;
}

export function loadLastSeenCount(scope: string): number {
  try {
    const raw = localStorage.getItem(seenKey(scope));
    const n = raw ? parseInt(raw, 10) : 0;
    return Number.isFinite(n) && n > 0 ? n : 0;
  } catch {
    return 0;
  }
}

export function saveLastSeenCount(scope: string, n: number): void {
  try {
    localStorage.setItem(seenKey(scope), String(Math.max(0, n)));
  } catch {
    /* localStorage disabled or quota exceeded — ignore */
  }
}

/** Index (in the non-system message list) for the "new messages" divider when a
 *  turn completes in-page — divider sits above the assistant reply, not the user
 *  message that triggered it. */
export function turnDividerIndex(msgs: PersistedMessage[], appendUser: boolean): number {
  const count = settledCount(msgs);
  return appendUser ? count + 1 : count;
}

/** Index for the "new messages" divider on chat re-entry. When the newest settled
 *  message is an assistant reply, anchor above that reply instead of above a
 *  trailing user message the user has not read yet. */
export function reentryDividerIndex(
  msgs: PersistedMessage[],
  seenAtEntry: number,
): number {
  const shown = settledCount(msgs);
  if (seenAtEntry <= 0 || shown <= seenAtEntry) return -1;
  const display = displayChatMessages(msgs);
  const lastIdx = shown - 1;
  const last = display[lastIdx];
  if (last?.role === "assistant") return lastIdx;
  return seenAtEntry;
}

/** Transcript rows shown in the chat panel: real messages plus the persistent
 *  session-divider rows (only non-divider system rows are hidden). */
export function displayChatMessages<T extends PersistedMessage>(msgs: T[]): T[] {
  return msgs.filter((m) => isSessionDivider(m) || m.role !== "system");
}

/** Scroll so the divider sits in the upper-middle of the chat viewport. */
export function scrollToNewMessagesDivider(
  container: HTMLElement,
  divider: HTMLElement,
): void {
  const dividerRect = divider.getBoundingClientRect();
  const containerRect = container.getBoundingClientRect();
  const dividerTopInContainer =
    dividerRect.top - containerRect.top + container.scrollTop;
  const targetTop = Math.max(0, dividerTopInContainer - container.clientHeight * 0.35);
  container.scrollTo({ top: targetTop });
}

/** Whether two transcript rows are the same message. Prefer stable ids; fall
 *  back to (role, kind, content) for optimistic rows that have no id yet. */
function sameMessageIdentity(a: PersistedMessage, b: PersistedMessage): boolean {
  if (typeof a.id === "number" && typeof b.id === "number") return a.id === b.id;
  return (
    a.role === b.role &&
    (a.kind ?? "") === (b.kind ?? "") &&
    a.content === b.content
  );
}

/**
 * Reconcile a locally-cached transcript against the server's chat history.
 *
 * Server history is authoritative and carries stable ids. The only rows the
 * local list may legitimately add are a trailing *in-flight* turn the server
 * has not recorded yet (an optimistic user row and/or a streaming assistant
 * placeholder). This is identity-based (not positional): each local row is
 * matched to a distinct server row, and only a **contiguous unmatched suffix**
 * of the local list is kept as the in-flight tail. That way a windowed local
 * cache reconciled against the full server list — or a server list that gained
 * a persistent `session_divider` row — can never re-append a message the server
 * already has, which is what used to render the same message twice.
 */
export function reconcileTranscript<T extends PersistedMessage>(
  cached: T[],
  server: T[],
): T[] {
  if (server.length === 0) return cached;
  const usedServer = new Set<number>();
  const matched = cached.map((lm) => {
    for (let i = 0; i < server.length; i++) {
      if (usedServer.has(i)) continue;
      if (sameMessageIdentity(lm, server[i])) {
        usedServer.add(i);
        return true;
      }
    }
    return false;
  });
  // Keep only a trailing run of unmatched local rows (the genuine in-flight
  // tail); a mid-list mismatch is ignored so the server wins for settled turns.
  let start = cached.length;
  while (start > 0 && !matched[start - 1]) start -= 1;
  const tail = cached
    .slice(start)
    .filter((m) => m.content.trim() !== "" && !isSessionDivider(m));
  return tail.length > 0 ? [...server, ...tail] : server;
}

/** OpenClaw writes delivery failures into the assistant bubble with this prefix. */
export function isErrorAssistantContent(content: string): boolean {
  return content.trimStart().startsWith("(error)");
}

/**
 * Drop a trailing user+assistant turn that already failed and was reported to
 * the user. Used when the user sends a *new* message so the failed turn does
 * not linger in the transcript (and cannot look like it will be re-sent).
 *
 * Detects:
 * - OpenClaw ``(error) …`` assistant bubbles
 * - Empty assistant + ``errorReported`` (Hermes ErrorBox / reconnect error)
 * - Trailing unanswered user + ``errorReported`` (server kept user, no reply)
 *
 * Does **not** drop stopped turns or successful empty (tool-only) replies when
 * ``errorReported`` is false.
 */
export function dropFailedTrailingTurn<T extends PersistedMessage>(
  msgs: T[],
  opts?: { errorReported?: boolean },
): T[] {
  if (msgs.length === 0) return msgs;
  const last = msgs[msgs.length - 1];
  if (last.role === "user" && opts?.errorReported) {
    return msgs.slice(0, -1);
  }
  if (msgs.length < 2) return msgs;
  const prev = msgs[msgs.length - 2];
  if (prev.role !== "user" || last.role !== "assistant") return msgs;
  const isErrorBubble = isErrorAssistantContent(last.content);
  const isEmptyFailed = last.content.trim() === "" && !!opts?.errorReported;
  if (!isErrorBubble && !isEmptyFailed) return msgs;
  return msgs.slice(0, -2);
}
