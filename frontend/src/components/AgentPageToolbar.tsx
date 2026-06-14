import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** List-page header: title + description on top, actions on a separate row. */
export function AgentManagementHeader({
  title,
  description,
  leading,
  actions,
  className,
}: {
  title: string;
  description?: string;
  leading?: ReactNode;
  actions?: ReactNode;
  className?: string;
}) {
  const hasToolbarRow = Boolean(leading || actions);
  return (
    <div
      className={cn(
        "overflow-hidden rounded-2xl border border-ink-200/70 bg-surface shadow-sm",
        className,
      )}
    >
      <div className="border-b border-ink-100/80 bg-gradient-to-br from-ink-50/90 via-white to-brand-50/25 px-5 py-4">
        <h1 className="text-xl font-semibold tracking-tight text-ink-900">{title}</h1>
        {description ? (
          <p className="mt-1.5 text-sm leading-relaxed text-ink-500">{description}</p>
        ) : null}
      </div>
      {hasToolbarRow ? (
        <div className="flex flex-col gap-3 px-5 py-3 sm:flex-row sm:items-center sm:justify-between">
          <div className="flex flex-wrap items-center gap-2">{leading}</div>
          <div className="flex flex-wrap items-center gap-2 sm:justify-end">{actions}</div>
        </div>
      ) : null}
    </div>
  );
}

export function AgentViewModeToggle({
  viewMode,
  onChange,
  cardLabel,
  listLabel,
}: {
  viewMode: "card" | "list";
  onChange: (mode: "card" | "list") => void;
  cardLabel: string;
  listLabel: string;
}) {
  return (
    <div
      className="inline-flex rounded-lg border border-ink-200/80 bg-ink-50/60 p-0.5"
      role="group"
      aria-label={cardLabel}
    >
      {(["card", "list"] as const).map((mode) => (
        <button
          key={mode}
          type="button"
          className={cn(
            "min-w-[52px] rounded-md px-2.5 py-1 text-xs font-semibold transition-colors",
            "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300",
            viewMode === mode
              ? "bg-surface text-brand-700 shadow-sm"
              : "text-ink-600 hover:text-ink-900",
          )}
          onClick={() => onChange(mode)}
        >
          {mode === "card" ? cardLabel : listLabel}
        </button>
      ))}
    </div>
  );
}

/** @deprecated Use AgentManagementHeader action row instead. */
export function AgentPageToolbar({
  children,
  primary,
  className,
}: {
  children: ReactNode;
  primary?: ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex shrink-0 flex-wrap items-center justify-end gap-2", className)}>
      <div
        className="inline-flex flex-wrap items-center gap-0.5 rounded-xl border border-ink-200/90
                   bg-surface p-1 shadow-sm"
      >
        {children}
      </div>
      {primary}
    </div>
  );
}

export function AgentToolbarButton({
  children,
  className,
  icon,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { icon?: ReactNode }) {
  return (
    <button
      type="button"
      className={cn(
        "inline-flex h-9 items-center justify-center gap-1.5 rounded-lg px-3 text-sm font-medium",
        "text-ink-700 transition-colors hover:bg-ink-50 hover:text-ink-900",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...rest}
    >
      {icon}
      {children}
    </button>
  );
}

export function AgentToolbarIconButton({
  className,
  icon,
  label,
  ...rest
}: React.ButtonHTMLAttributes<HTMLButtonElement> & { icon: ReactNode; label: string }) {
  return (
    <button
      type="button"
      title={label}
      aria-label={label}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-lg text-ink-600",
        "transition-colors hover:bg-ink-50 hover:text-ink-900",
        "focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-brand-300",
        "disabled:pointer-events-none disabled:opacity-50",
        className,
      )}
      {...rest}
    >
      {icon}
    </button>
  );
}

export function AgentToolbarDivider() {
  return <div className="mx-0.5 hidden h-5 w-px bg-ink-200 sm:block" aria-hidden="true" />;
}
