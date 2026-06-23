import { useCallback, useEffect, useRef } from "react";

import { api } from "./api";
import { useLocalStorageBackedState } from "./localState";
import { openOpStream } from "./ws";

/**
 * Recovery for a long-running operation's UI across page refresh and tab
 * close+reopen.
 *
 * The operation's identity lives server-side (the op registry); this hook keeps
 * a durable localStorage *pointer* to the active op so that on a fresh mount
 * (refresh / reopen / SPA nav-back) it can:
 *   1. query the terminal status (`GET /api/operations/{opId}`), and
 *   2. if still running, subscribe to live transitions over WebSocket.
 *
 * The in-page happy path (the awaited create POST) is unaffected — it resolves
 * the popup directly and calls `clear()`. The mount recovery only fires when the
 * pointer is already present at mount (i.e. the awaiting closure is gone). The
 * two never double-handle: recovery reads the pointer captured at FIRST render,
 * so a `track()` issued later in-page does not re-trigger it.
 */

export interface OpPointer {
  opId: string;
  agentId: string;
}

export interface OpRecoveryCallbacks {
  /** Rebuild the popup as "running" (op still in flight at mount). */
  onRunning: (p: OpPointer) => void;
  onSucceeded: (p: OpPointer, result: Record<string, unknown>) => void;
  onFailed: (p: OpPointer, detail: string) => void;
  /**
   * The op is genuinely gone — it stayed `not_found` (and not in-flight) for the
   * whole grace window, so it was never registered (or was evicted with no
   * recoverable entity). Tear the popup down silently. Optional: callers that
   * have nothing to dismiss can omit it.
   */
  onMissing?: (p: OpPointer) => void;
}

export interface OpRecoveryHandle {
  track: (p: OpPointer) => void;
  clear: () => void;
}

/**
 * How long a fresh mount tolerates a `not_found` / transient-error status before
 * concluding the op is truly gone. The op (and the in-flight marker) is recorded
 * server-side only *after* the create POST finishes its synchronous registration
 * work — id reindex, openclaw.json sanitize (an up-to-8s gateway call), runtime
 * default sync — so `GET /api/operations/{id}` legitimately returns `not_found`
 * for that whole window. Giving up early here is exactly what used to make the
 * popup vanish mid-create, so the budget is set comfortably above the worst-case
 * pre-registration latency. Erring long is cheap: the only cost is a stale
 * "running" shell for a genuinely-never-registered op (a create POST that never
 * reached the backend at all), which is rare and self-heals at the deadline.
 */
const OP_RECOVERY_GRACE_MS = 45_000;
/** Re-query cadence while waiting for a terminal transition (WS is the fast path). */
const OP_RECOVERY_POLL_MS = 1_000;

const sleep = (ms: number) => new Promise<void>((resolve) => window.setTimeout(resolve, ms));

export function useOpRecovery(storageKey: string, cb: OpRecoveryCallbacks): OpRecoveryHandle {
  const [, setPointer] = useLocalStorageBackedState<OpPointer | null>(storageKey, null, {
    isClosed: (v) => v === null,
  });
  // Capture the pointer present at FIRST render — that is the one to recover.
  const initialPointer = useRef<OpPointer | null>(null);
  const initialRead = useRef(false);
  if (!initialRead.current) {
    initialRead.current = true;
    try {
      const raw = window.localStorage.getItem(`csflow:op-state:${storageKey}`);
      initialPointer.current = raw ? (JSON.parse(raw) as OpPointer) : null;
    } catch {
      initialPointer.current = null;
    }
  }

  const cbRef = useRef(cb);
  useEffect(() => {
    cbRef.current = cb;
  }, [cb]);

  const streamRef = useRef<{ close: () => void } | null>(null);

  const clear = useCallback(() => {
    setPointer(null);
    streamRef.current?.close();
    streamRef.current = null;
  }, [setPointer]);

  const track = useCallback((p: OpPointer) => setPointer(p), [setPointer]);

  useEffect(() => {
    const p = initialPointer.current;
    if (!p) return;
    let cancelled = false;
    let terminalHandled = false;

    const finishTerminal = (state: "succeeded" | "failed", result: Record<string, unknown>, detail: string) => {
      if (terminalHandled) return; // GET and WS can both deliver it — first wins
      terminalHandled = true;
      if (state === "succeeded") cbRef.current.onSucceeded(p, result);
      else cbRef.current.onFailed(p, detail);
      setPointer(null);
      streamRef.current?.close();
      streamRef.current = null;
    };

    // Subscribe to live transitions BEFORE querying status. The op registry is
    // live-only (no replay), so a terminal transition that lands between a "GET
    // returns running" and the WS subscribe would otherwise be lost, leaving the
    // popup stuck "running" (the tab-switch-during-create symptom). Subscribing
    // first closes that gap: the WS catches anything published from now on, and
    // the poll below catches anything already recorded. The stream also survives
    // the whole grace window, so a terminal frame that lands during a transient
    // `not_found` is still delivered.
    streamRef.current = openOpStream(p.opId, {
      onStatus: (f) => {
        if (cancelled) return;
        if (f.state === "succeeded") finishTerminal("succeeded", f.result, f.detail);
        else if (f.state === "failed") finishTerminal("failed", f.result, f.detail);
      },
    });

    // Poll the status until it resolves to a terminal state, rather than acting
    // on a single shot. This is what keeps the popup stable across a remount:
    //   • running / still-in-flight  → rebuild the popup and keep reconciling
    //     (the WS frame, or a later poll, delivers the terminal transition);
    //   • not_found / transient error within the grace window → the create POST
    //     hasn't registered the op yet — rebuild the popup as running and keep
    //     waiting. Crucially we DO NOT clear the pointer or close the popup here;
    //     that premature teardown was the "弹窗直接不见了" bug;
    //   • not_found past the grace window → the op never materialised → onMissing.
    (async () => {
      const deadline = Date.now() + OP_RECOVERY_GRACE_MS;
      let rebuilt = false;
      const ensureRunning = () => {
        if (rebuilt) return;
        rebuilt = true;
        cbRef.current.onRunning(p);
      };
      while (!cancelled && !terminalHandled) {
        let st;
        try {
          st = await api.getOperationStatus(p.opId);
        } catch {
          // Transient (offline / server blip): keep the popup + live stream and
          // retry; only stop polling once the grace window is exhausted (the WS
          // stream stays open as a last-resort delivery path).
          ensureRunning();
          if (Date.now() >= deadline) return;
          await sleep(OP_RECOVERY_POLL_MS);
          continue;
        }
        if (cancelled || terminalHandled) return;
        if (st.state === "succeeded") return finishTerminal("succeeded", st.result, st.detail);
        if (st.state === "failed") return finishTerminal("failed", st.result, st.detail);
        if (st.state === "running" || st.inFlight) {
          // Confirmed live. Keep the popup up and keep reconciling (poll acts as
          // the safety net if a WS terminal frame is ever missed).
          ensureRunning();
          await sleep(OP_RECOVERY_POLL_MS);
          continue;
        }
        // not_found
        if (Date.now() >= deadline) {
          cbRef.current.onMissing?.(p);
          setPointer(null);
          streamRef.current?.close();
          streamRef.current = null;
          return;
        }
        ensureRunning();
        await sleep(OP_RECOVERY_POLL_MS);
      }
    })();

    return () => {
      cancelled = true;
      streamRef.current?.close();
      streamRef.current = null;
    };
    // Run once on mount; deps intentionally empty (reads the first-render pointer).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return { track, clear };
}
