import { cn } from "@/lib/cn";

export type AgentPlatform = "openclaw" | "hermes" | "claude" | "codex" | "cursor";

const PLATFORM_ICONS: Record<AgentPlatform, string> = {
  openclaw: "/agent-icons/openclaw.png",
  hermes: "/agent-icons/hermes.png",
  claude: "/agent-icons/claude.png",
  codex: "/agent-icons/codex.png",
  cursor: "/agent-icons/claude.png",
};

type AgentCardAvatarProps = {
  className?: string;
  /** Card grid uses 56px tile; chat header uses 44px. */
  size?: "card" | "header" | "empty";
  platform?: AgentPlatform;
};

export function AgentCardAvatar({
  className,
  size = "card",
  platform = "openclaw",
}: AgentCardAvatarProps) {
  const boxClass =
    size === "card"
      ? "mb-3 inline-flex h-14 w-14 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 shadow-[0_0_18px_-8px_theme(colors.brand.400)] transition-shadow group-hover:shadow-[0_0_22px_-6px_theme(colors.brand.400)]"
      : size === "header"
        ? "inline-flex h-11 w-11 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 shadow-[0_0_18px_-8px_theme(colors.brand.400)]"
        : "inline-flex h-16 w-16 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 text-brand-500";
  const imgClass =
    size === "card" ? "h-9 w-9" : size === "header" ? "h-7 w-7" : "h-10 w-10";

  return (
    <div className={cn(boxClass, className)}>
      <img
        src={PLATFORM_ICONS[platform]}
        alt=""
        className={cn(imgClass, "object-contain")}
      />
    </div>
  );
}
