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
}

export const HISTORY_LIMIT = 20;

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
