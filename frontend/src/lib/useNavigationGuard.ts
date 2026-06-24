import { useEffect, useRef } from "react";
import { useBlocker } from "react-router-dom";

/**
 * Block the user from leaving the current page while `active` is true.
 *
 * Two leave paths are covered:
 *   1. **In-app SPA navigation** (clicking a link, breadcrumb, browser
 *      back/forward) — intercepted via react-router's `useBlocker`. A blocked
 *      navigation is cancelled (`blocker.reset()`) so the user stays put, and
 *      `onBlocked` fires so the caller can explain why (e.g. an alert).
 *   2. **Browser unload** (refresh / tab close / address-bar nav) — a
 *      `beforeunload` handler asks the browser to show its native "Leave site?"
 *      prompt. (Browsers only allow a prompt here, never a hard block.)
 *
 * Used to keep an irreversible in-flight operation (agent removal, create
 * cancellation) from being orphaned by the user navigating away mid-flight.
 *
 * NOTE: react-router supports only ONE active blocker at a time, so a page must
 * call this hook from a single always-mounted component — fold every blocking
 * condition into one `active` expression rather than calling it twice.
 */
export function useNavigationGuard(active: boolean, onBlocked?: () => void): void {
  // useBlocker accepts a boolean; calling it unconditionally keeps hook order
  // stable. When `active` is false it simply never blocks.
  const blocker = useBlocker(active);
  const onBlockedRef = useRef(onBlocked);
  onBlockedRef.current = onBlocked;

  useEffect(() => {
    if (blocker.state !== "blocked") return;
    if (active) {
      // Cancel the navigation and tell the user why.
      onBlockedRef.current?.();
      blocker.reset();
    } else {
      // The guard turned off between the click and this effect — let it through
      // so a stale blocked navigation doesn't trap the user.
      blocker.proceed();
    }
  }, [blocker, active]);

  useEffect(() => {
    if (!active) return;
    const handler = (e: BeforeUnloadEvent) => {
      e.preventDefault();
      // Legacy browsers require returnValue to be set to trigger the prompt.
      e.returnValue = "";
    };
    window.addEventListener("beforeunload", handler);
    return () => window.removeEventListener("beforeunload", handler);
  }, [active]);
}
