import { cn } from "@/lib/cn";
import {
  agentIconImgClass,
  agentIconSrc,
  type AgentIconSlot,
  type AgentPlatform,
} from "@/lib/agentIconSizing";

export type { AgentPlatform };

type AgentCardAvatarProps = {
  className?: string;
  /** Card grid uses 56px tile; chat header uses 44px. */
  size?: "card" | "header" | "empty";
  platform?: AgentPlatform;
};

const SIZE_TO_SLOT: Record<NonNullable<AgentCardAvatarProps["size"]>, AgentIconSlot> = {
  card: "card",
  header: "header",
  empty: "empty",
};

export function AgentCardAvatar({
  className,
  size = "card",
  platform = "openclaw",
}: AgentCardAvatarProps) {
  const slot = SIZE_TO_SLOT[size];
  const boxClass =
    size === "card"
      ? "mb-3 inline-flex h-14 w-14 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 shadow-[0_0_18px_-8px_theme(colors.brand.400)] transition-shadow group-hover:shadow-[0_0_22px_-6px_theme(colors.brand.400)]"
      : size === "header"
        ? "inline-flex h-11 w-11 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 shadow-[0_0_18px_-8px_theme(colors.brand.400)]"
        : "inline-flex h-16 w-16 items-center justify-center rounded-xl border border-brand-200 bg-brand-50 text-brand-500";

  return (
    <div className={cn(boxClass, className)}>
      <img
        src={agentIconSrc(platform)}
        alt=""
        className={cn(agentIconImgClass(platform, slot), "object-contain")}
      />
    </div>
  );
}
