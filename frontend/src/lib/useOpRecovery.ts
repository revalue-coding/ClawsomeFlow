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
}

export interface OpRecoveryHandle {
  track: (p: OpPointer) => void;
  clear: () => void;
}

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
    // the GET below catches anything already recorded.
    streamRef.current = openOpStream(p.opId, {
      onStatus: (f) => {
        if (cancelled) return;
        if (f.state === "succeeded") finishTerminal("succeeded", f.result, f.detail);
        else if (f.state === "failed") finishTerminal("failed", f.result, f.detail);
      },
    });

    (async () => {
      let st;
      try {
        st = await api.getOperationStatus(p.opId);
      } catch {
        return; // transient — leave the pointer + live stream for a later recovery
      }
      if (cancelled || terminalHandled) return;
      if (st.state === "succeeded") {
        finishTerminal("succeeded", st.result, st.detail);
      } else if (st.state === "failed") {
        finishTerminal("failed", st.result, st.detail);
      } else if (st.state === "not_found") {
        setPointer(null);
        streamRef.current?.close();
        streamRef.current = null;
      } else {
        // running → reconstruct the popup; the already-open stream above will
        // deliver the terminal transition.
        cbRef.current.onRunning(p);
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
