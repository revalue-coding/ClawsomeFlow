/** Live step-level progress for an in-flight single-agent chat turn.
 *
 * Shared by the OpenClaw and Hermes chat pages: shows elapsed time + counters
 * and the ordered list of trajectory steps (tool calls / progress lines)
 * surfaced from the agent's session store while the turn runs. */
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { ChatProgress, ChatStep } from "@/lib/api";

export function ChatStepTrail({
  steps,
  progress,
}: {
  steps: ChatStep[];
  progress: ChatProgress | null;
}) {
  const { t } = useTranslation();

  // Tick the elapsed counter client-side every second, re-anchoring on the
  // server's ``elapsedSec`` whenever a fresh progress event arrives. Relying on
  // the server value alone made the timer look frozen (it historically only
  // advanced when tool/api counts changed); a local tick keeps it smooth and
  // never frozen regardless of SSE cadence.
  const anchorRef = useRef({ wall: Date.now(), base: progress?.elapsedSec ?? 0 });
  const [now, setNow] = useState(() => Date.now());
  const serverElapsed = progress?.elapsedSec;
  useEffect(() => {
    if (typeof serverElapsed === "number") {
      anchorRef.current = { wall: Date.now(), base: serverElapsed };
      setNow(Date.now());
    }
  }, [serverElapsed]);
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(id);
  }, []);
  const elapsed = Math.max(
    0,
    Math.round(anchorRef.current.base + (now - anchorRef.current.wall) / 1000),
  );
  return (
    <div className="rounded-lg border border-brand-200 bg-brand-50/60 px-3 py-2 text-xs text-ink-600">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1 font-medium text-brand-700">
        <span>{t("chat.progress.title")}</span>
        <span className="text-ink-500">
          {t("chat.progress.elapsed", { seconds: String(elapsed) })}
        </span>
        {progress && (progress.toolCalls > 0 || progress.apiCalls > 0) && (
          <span className="text-ink-500">
            {t("chat.progress.counters", {
              tools: String(progress.toolCalls),
              api: String(progress.apiCalls),
            })}
          </span>
        )}
      </div>
      {steps.length > 0 && (
        <ul className="mt-1.5 space-y-0.5">
          {steps.slice(-8).map((s) => (
            <li key={s.seq} className="flex items-center gap-1.5">
              <span className="text-brand-400">›</span>
              <span className="font-mono">
                {s.kind === "tool" && s.name
                  ? t("chat.progress.tool", { name: s.name })
                  : s.name || t("chat.progress.working")}
              </span>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
