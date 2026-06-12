import type { ReactNode } from "react";

import { cn } from "@/lib/cn";

/** Secondary actions grouped in a bordered toolbar (refresh / import / …). */
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
                   bg-white p-1 shadow-sm"
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
