/** Small UI primitives used across pages. Kept in one file for ergonomic
 * import (`import { Card, EmptyState, ... } from "@/components/ui"`).
 */

import { ReactNode, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/cn";

// ── Card ──────────────────────────────────────────────────────────

export function Card({
  className,
  children,
}: {
  className?: string;
  children: ReactNode;
}) {
  return <div className={cn("card p-5", className)}>{children}</div>;
}

export function CardTitle({
  children,
  hint,
  right,
}: {
  children: ReactNode;
  hint?: string;
  right?: ReactNode;
}) {
  return (
    <div className="flex items-center justify-between mb-3">
      <div>
        <h3 className="text-base font-semibold text-ink-900">{children}</h3>
        {hint && <div className="text-xs text-ink-500 mt-0.5">{hint}</div>}
      </div>
      {right}
    </div>
  );
}

// ── Status pill (RunStatus) ─────────────────────

export function StatusPill({ status }: { status: string }) {
  const { t, i18n } = useTranslation();
  const palette: Record<string, string> = {
    pending: "pill-default",
    compiling: "pill-info",
    running: "pill-info",
    awaiting_user_checkpoint: "pill-warning",
    awaiting_user_review: "pill-warning",
    awaiting_user_complaint: "pill-warning",
    complaint_processing: "pill-info",
    complaint_failed: "pill-danger",
    completed: "pill-success",
    completed_with_conflicts: "pill-warning",
    failed: "pill-danger",
    aborted: "pill-default",
    succeeded: "pill-success",
    dispatched: "pill-info",
    timed_out: "pill-danger",
  };
  // Translate when we have a label for it; fall back to the raw enum
  // value so ad-hoc strings don't disappear silently.
  const key = `statusLabel.${status}`;
  const label = i18n.exists(key) ? t(key) : status;
  return <span className={palette[status] ?? "pill-default"}>{label}</span>;
}

// ── Empty state ──────────────────────────────────────────────────

export function EmptyState({
  icon,
  title,
  hint,
  action,
}: {
  icon?: ReactNode;
  title?: string;
  hint?: string;
  action?: ReactNode;
}) {
  return (
    <div className="flex flex-col items-center justify-center py-16 text-center">
      {icon ? (
        <div className="mb-4 inline-flex h-20 w-20 items-center justify-center rounded-2xl border border-brand-200 bg-brand-50 text-brand-500 shadow-[0_0_30px_-10px_theme(colors.brand.400)]">
          {icon}
        </div>
      ) : null}
      {title ? <div className="text-base font-medium text-ink-700">{title}</div> : null}
      {hint && (
        <div className="text-sm text-ink-500 mt-1 max-w-md">{hint}</div>
      )}
      {action && <div className="mt-4">{action}</div>}
    </div>
  );
}

// ── Loading + error inline blocks ────────────────────────────────

export function Loading({ label }: { label?: string }) {
  const { t } = useTranslation();
  return (
    <div className="flex items-center gap-2 text-sm text-ink-500 py-6">
      <span className="inline-block h-2 w-2 rounded-full bg-brand-500 animate-pulse" />
      {label ?? t("common.loading")}
    </div>
  );
}

export function ErrorBox({ children }: { children: ReactNode }) {
  return (
    <div className="rounded-md border border-rose-200 bg-rose-50 px-4 py-3 text-sm text-rose-700">
      {children}
    </div>
  );
}

// ── Modal ─────────────────────────────────────────────────────────

export function Modal({
  open,
  onClose,
  title,
  children,
  width = "max-w-xl",
  dismissible = true,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  width?: string;
  dismissible?: boolean;
}) {
  const { t } = useTranslation();
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!open || !dismissible) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !e.ctrlKey && !e.metaKey && !e.altKey) {
        onClose();
      }
    };
    document.addEventListener("keydown", onKey);
    return () => document.removeEventListener("keydown", onKey);
  }, [open, onClose, dismissible]);
  if (!open) return null;
  return (
    <div
      className="fixed inset-0 z-50 overflow-y-auto bg-black/40 px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))]"
    >
      <div className="flex min-h-full items-center justify-center">
        <div
          ref={ref}
          className={cn(
            "mx-auto w-full max-h-[calc(100vh-2rem)] rounded-lg bg-white shadow-xl flex flex-col",
            width,
          )}
        >
          <div className="px-6 py-4 border-b border-ink-100 flex items-center justify-between">
            <h3 className="font-semibold text-ink-900">{title}</h3>
            {dismissible ? (
              <button
                className="text-ink-400 hover:text-ink-700 text-xl leading-none"
                onClick={onClose}
                aria-label={t("common.close")}
                type="button"
              >
                ×
              </button>
            ) : (
              <span className="w-5" aria-hidden />
            )}
          </div>
          <div className="p-6 overflow-y-auto">{children}</div>
        </div>
      </div>
    </div>
  );
}
