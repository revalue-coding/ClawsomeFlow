/** Small UI primitives used across pages. Kept in one file for ergonomic
 * import (`import { Card, EmptyState, ... } from "@/components/ui"`).
 */

import { ReactNode, useEffect, useRef } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";
import { cn } from "@/lib/cn";

/** Portal host id rendered by AppShell over the content area (not the sidebar),
 * so modals dim/cover only the page content and the nav stays clickable. */
export const MODAL_ROOT_ID = "csflow-modal-root";

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
    orphaned: "pill-danger",
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
        <div className="mb-4 inline-flex h-20 w-20 items-center justify-center rounded-2xl border border-brand-200 bg-brand-50 text-brandicon shadow-[0_0_30px_-10px_rgb(var(--brand-400))]">
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
  fullscreenBackdrop = false,
}: {
  open: boolean;
  onClose: () => void;
  title: string;
  children: ReactNode;
  width?: string;
  dismissible?: boolean;
  /** When true, render a viewport-covering, fully-opaque blocking overlay
   *  (covers the sidebar too) instead of the default modeless content-area dim.
   *  Used for the auto-upgrade modal, which must mask everything behind it. */
  fullscreenBackdrop?: boolean;
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
  // Modeless: the overlay covers only the content area (it is portaled into the
  // host AppShell renders over `<Outlet>`, NOT the sidebar), uses a light dim,
  // and does NOT dismiss on outside-click — so users can switch modules via the
  // nav while the modal stays open. Esc still closes when dismissible.
  const body = (
    <div
      className={cn(
        "overflow-y-auto px-4 pt-[max(1rem,env(safe-area-inset-top))] pb-[max(1rem,env(safe-area-inset-bottom))] pointer-events-auto",
        fullscreenBackdrop
          // Cover the WHOLE viewport (sidebar included), opaque + blurred, above
          // all app chrome — nothing behind the upgrade modal stays visible.
          // Literal black (not ink-900, which inverts to a light scrim in dark).
          ? "fixed inset-0 z-[100] bg-black/80 backdrop-blur-sm"
          // Stronger dim in dark mode so the page behind clearly recedes — a
          // 20% black scrim is nearly invisible over a dark canvas.
          : "absolute inset-0 z-40 bg-black/20 dark:bg-black/60",
      )}
    >
      <div className="flex min-h-full items-center justify-center">
        <div
          ref={ref}
          className={cn(
            // Border distinguishes the panel from the page. In dark mode the
            // surface is only slightly lighter than the canvas, so use a clearly
            // light border (ink-500 → light gray under .dark) to outline it.
            "mx-auto w-full max-h-[calc(100vh-2rem)] rounded-lg border border-ink-200 bg-surface shadow-xl dark:border-ink-500 flex flex-col",
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
  // Fullscreen backdrop portals to <body> so it escapes the content-area host
  // and covers the sidebar; the default modeless modal stays in MODAL_ROOT_ID.
  const host =
    typeof document !== "undefined"
      ? fullscreenBackdrop
        ? document.body
        : document.getElementById(MODAL_ROOT_ID)
      : null;
  return host ? createPortal(body, host) : body;
}
