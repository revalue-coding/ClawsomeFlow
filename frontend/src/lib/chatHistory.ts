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
}

export const HISTORY_LIMIT = 20;

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

/** Count of messages the user has "seen": non-system, excluding a trailing
 *  empty assistant placeholder (the in-flight pending bubble). */
export function settledCount(msgs: PersistedMessage[]): number {
  const arr = msgs.filter((m) => m.role !== "system");
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

/**
 * Reconcile a locally-cached transcript against the server's chat history.
 *
 * Server history is authoritative for completed turns. If a tab switch detached
 * the in-page streaming closure, the cache keeps a stale *empty* assistant
 * bubble (which renders as the false "no reply" message) while the server holds
 * the real answer. We therefore prefer the server, and only keep a trailing
 * cached turn the server hasn't recorded yet (a genuine in-flight partial),
 * dropping an empty placeholder.
 */
export function reconcileTranscript<T extends PersistedMessage>(
  cached: T[],
  server: T[],
): T[] {
  if (server.length === 0) return cached;
  if (cached.length <= server.length) return server;
  const tail = cached.slice(server.length).filter((m) => m.content.trim() !== "");
  return tail.length > 0 ? [...server, ...tail] : server;
}
