/**
 * Sticky-scroll behaviour for chat transcripts.
 *
 * Auto-scrolls to the latest message ONLY when the user is already near the
 * bottom, so reading scrollback isn't yanked away when new content (or a live
 * streaming reply) arrives. Exposes `atBottom` to drive a "back to latest"
 * button and `scrollToBottom` to jump down on demand (or when the user sends).
 */
import { useCallback, useRef, useState } from "react";

const NEAR_BOTTOM_PX = 80;

export function useStickyScroll<T extends HTMLElement = HTMLDivElement>() {
  const ref = useRef<T>(null);
  const atBottomRef = useRef(true);
  const [atBottom, setAtBottom] = useState(true);

  const handleScroll = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    const near = el.scrollHeight - el.scrollTop - el.clientHeight <= NEAR_BOTTOM_PX;
    if (near !== atBottomRef.current) {
      atBottomRef.current = near;
      setAtBottom(near);
    }
  }, []);

  const scrollToBottom = useCallback(() => {
    const el = ref.current;
    if (!el) return;
    el.scrollTo({ top: el.scrollHeight });
    atBottomRef.current = true;
    setAtBottom(true);
    // The caller often invokes this right after a state update (e.g. appending
    // the user's just-sent message), before React has committed the new nodes to
    // the DOM. At that point `scrollHeight` is still the old value, so the jump
    // above lands short of the real bottom. Re-run after the next paint to catch
    // the freshly rendered content and truly pin to the latest.
    window.requestAnimationFrame(() => {
      const node = ref.current;
      if (node) node.scrollTo({ top: node.scrollHeight });
    });
  }, []);

  const suppressStickyRef = useRef(false);

  /** Skip the next auto-stick and mark the user as scrolled away — used when a
   *  completed turn should scroll to the "new messages" divider instead. */
  const suppressNextStickyScroll = useCallback(() => {
    suppressStickyRef.current = true;
    atBottomRef.current = false;
    setAtBottom(false);
  }, []);

  /** Stick to the bottom, but only if the user was already there. Call from an
   *  effect keyed on the transcript so live updates don't fight manual scroll. */
  const stickIfAtBottom = useCallback(() => {
    if (suppressStickyRef.current) {
      suppressStickyRef.current = false;
      return;
    }
    if (atBottomRef.current) {
      const el = ref.current;
      if (el) el.scrollTo({ top: el.scrollHeight });
    }
  }, []);

  return {
    ref,
    atBottom,
    scrollToBottom,
    handleScroll,
    stickIfAtBottom,
    suppressNextStickyScroll,
  };
}
