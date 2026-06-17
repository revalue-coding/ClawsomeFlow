/** Live step-level progress for an in-flight single-agent chat turn.
 *
 * Shared by the OpenClaw and Hermes chat pages: shows elapsed time + counters
 * and the ordered list of trajectory steps (tool calls / progress lines)
 * surfaced from the agent's session store while the turn runs. */
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
  const elapsed = Math.round(progress?.elapsedSec ?? 0);
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
