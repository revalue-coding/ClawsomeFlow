/**
 * Persistent chat-history helper backed by ``localStorage``.
 *
 * ``OpenclawChat`` uses this so user-visible chat transcripts survive page
 * refreshes. Only the **last N** messages are kept — long sessions
 * shouldn't eat the storage quota — and only an explicit "reset" should
 * wipe the bucket.
 */

export interface PersistedMessage {
  role: "user" | "assistant" | "system";
  content: string;
  /** Epoch ms when the message was sent/received (client clock). Optional:
   *  server-recovered messages and older cached entries have no timestamp. */
  ts?: number;
}

export const HISTORY_LIMIT = 20;

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

export function loadChatHistory(scope: string): PersistedMessage[] {
  try {
    const raw = localStorage.getItem(storageKey(scope));
    if (!raw) return [];
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed
      .filter(
        (m): m is PersistedMessage =>
          !!m &&
          typeof m === "object" &&
          (m.role === "user" || m.role === "assistant" || m.role === "system") &&
          typeof m.content === "string",
      )
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
  } catch {
    /* ignore */
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
