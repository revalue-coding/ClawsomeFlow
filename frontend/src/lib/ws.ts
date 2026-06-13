/**
 * WebSocket helper for `/ws/{run_id}` (Run live event stream).
 *
 * Reconnects with exponential backoff (1s → 8s, capped). Maintains the
 * highest-seen event id so that on reconnect it asks the server to
 * backfill missed events via `?sinceId=N`.
 */

import type { RunEventView } from "./api";

export type RunWsEvent = {
  id: number;
  ts: string;
  type: string;
  agentId: string | null;
  taskId: string | null;
  payload: Record<string, unknown>;
  dropped?: boolean;
};

export interface RunWsHandle {
  close: () => void;
}

export interface RunWsOptions {
  onEvent: (e: RunWsEvent) => void;
  onStatus?: (status: "connecting" | "open" | "closed" | "error") => void;
  /** Initial sinceId; subsequent reconnects use the highest received id. */
  initialSinceId?: number;
}

export function openRunStream(runId: string, opts: RunWsOptions): RunWsHandle {
  let socket: WebSocket | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let backoff = 1000;
  let lastSeen = opts.initialSinceId ?? 0;
  let closedByUser = false;

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const baseUrl = `${proto}//${window.location.host}/ws/${runId}`;

  function connect() {
    if (closedByUser) return;
    opts.onStatus?.("connecting");
    const url = lastSeen > 0 ? `${baseUrl}?sinceId=${lastSeen}` : baseUrl;
    socket = new WebSocket(url);

    socket.onopen = () => {
      backoff = 1000;
      opts.onStatus?.("open");
      pingTimer = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }));
        }
      }, 30_000);
    };

    socket.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data?.type === "pong") return;
        if (typeof data?.id === "number") lastSeen = Math.max(lastSeen, data.id);
        opts.onEvent(data as RunWsEvent);
      } catch {
        /* ignore parse errors */
      }
    };

    socket.onerror = () => opts.onStatus?.("error");

    socket.onclose = () => {
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = null;
      socket = null;
      opts.onStatus?.("closed");
      if (closedByUser) return;
      reconnectTimer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
  }

  connect();

  return {
    close: () => {
      closedByUser = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (pingTimer) clearInterval(pingTimer);
      if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    },
  };
}

// ── Operation-status stream (`/ws/op/{op_id}`) ──────────────────────────
// Live-only sibling of openRunStream: no sinceId/backfill. The server sends a
// snapshot frame on connect (current state) then one frame per transition.

export type OpStatusFrame = {
  type: "op_status";
  opId: string;
  kind: string;
  state: "running" | "succeeded" | "failed";
  detail: string;
  result: Record<string, unknown>;
  ts: number;
};

export interface OpStreamHandle {
  close: () => void;
}

export interface OpStreamOptions {
  onStatus: (frame: OpStatusFrame) => void;
  onConn?: (status: "connecting" | "open" | "closed" | "error") => void;
}

export function openOpStream(opId: string, opts: OpStreamOptions): OpStreamHandle {
  let socket: WebSocket | null = null;
  let pingTimer: ReturnType<typeof setInterval> | null = null;
  let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
  let backoff = 1000;
  let closedByUser = false;

  const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
  const url = `${proto}//${window.location.host}/ws/op/${encodeURIComponent(opId)}`;

  function connect() {
    if (closedByUser) return;
    opts.onConn?.("connecting");
    socket = new WebSocket(url);

    socket.onopen = () => {
      backoff = 1000;
      opts.onConn?.("open");
      pingTimer = setInterval(() => {
        if (socket?.readyState === WebSocket.OPEN) {
          socket.send(JSON.stringify({ type: "ping" }));
        }
      }, 30_000);
    };

    socket.onmessage = (msg) => {
      try {
        const data = JSON.parse(msg.data);
        if (data?.type === "op_status") opts.onStatus(data as OpStatusFrame);
      } catch {
        /* ignore parse errors */
      }
    };

    socket.onerror = () => opts.onConn?.("error");

    socket.onclose = () => {
      if (pingTimer) clearInterval(pingTimer);
      pingTimer = null;
      socket = null;
      opts.onConn?.("closed");
      if (closedByUser) return;
      reconnectTimer = setTimeout(connect, backoff);
      backoff = Math.min(backoff * 2, 8000);
    };
  }

  connect();

  return {
    close: () => {
      closedByUser = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (pingTimer) clearInterval(pingTimer);
      if (socket && socket.readyState <= WebSocket.OPEN) socket.close();
    },
  };
}

/** Convenience: convert REST `RunEventView` → `RunWsEvent` (same shape). */
export function eventViewToWs(e: RunEventView): RunWsEvent {
  return {
    id: e.id, ts: e.ts, type: e.type,
    agentId: e.agentId, taskId: e.taskId, payload: e.payload,
  };
}
