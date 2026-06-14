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
  // Neutral "frosted" frame — a faint veil instead of a red brand tint. Light:
  // a barely-there dark veil; dark: a faint white fog over the card surface.
  const frame =
    "items-center justify-center rounded-xl border border-ink-200 bg-ink-900/[0.04] dark:bg-white/[0.06]";
  const boxClass =
    size === "card"
      ? `mb-3 inline-flex h-14 w-14 ${frame} shadow-sm transition-shadow group-hover:shadow`
      : size === "header"
        ? `inline-flex h-11 w-11 ${frame} shadow-sm`
        : `inline-flex h-16 w-16 ${frame}`;

  return (
    <div className={cn(boxClass, className)}>
      <img
        src={agentIconSrc(platform)}
        alt=""
        // The mascots are a vivid vermilion — soften it in BOTH themes (less
        // saturation) so it reads as a gentle accent rather than a hot red blob;
        // in dark mode also lift brightness a touch so it stays visible.
        className={cn(
          agentIconImgClass(platform, slot),
          "object-contain saturate-[.85] dark:saturate-[.75] dark:brightness-110",
        )}
      />
    </div>
  );
}
