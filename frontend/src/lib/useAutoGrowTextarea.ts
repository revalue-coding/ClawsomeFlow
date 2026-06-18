import { useEffect, useRef } from "react";

interface UseAutoGrowTextareaOptions {
  minHeightPx?: number;
  maxHeightPx?: number;
}

/**
 * Auto-grows a textarea as content increases, up to a max height.
 * After hitting the cap, the textarea keeps a stable height and scrolls.
 */
export function useAutoGrowTextarea(
  value: string,
  options: UseAutoGrowTextareaOptions = {},
) {
  const { minHeightPx = 80, maxHeightPx = 240 } = options;
  const ref = useRef<HTMLTextAreaElement | null>(null);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;

    el.style.height = "auto";
    const naturalHeight = Math.max(el.scrollHeight, minHeightPx);
    const nextHeight = Math.min(naturalHeight, maxHeightPx);
    el.style.height = `${nextHeight}px`;
    el.style.overflowY = naturalHeight > maxHeightPx ? "auto" : "hidden";
  }, [value, minHeightPx, maxHeightPx]);

  return ref;
}
