import { cn } from "@/lib/cn";
import { OpenclawGlyph, HermesGlyph } from "@/components/agentGlyphs";
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

  // OpenClaw/Hermes ship as single-color SVG glyphs painted with the shared
  // `text-brandicon` tint (exact match with every other icon). Other platforms
  // (claude/codex/cursor) keep their raster logos.
  const Glyph =
    platform === "openclaw" ? OpenclawGlyph : platform === "hermes" ? HermesGlyph : null;

  return (
    <div className={cn(boxClass, className)}>
      {Glyph ? (
        <Glyph className={cn(agentIconImgClass(platform, slot), "text-brandicon")} />
      ) : (
        <img
          src={agentIconSrc(platform)}
          alt=""
          className={cn(agentIconImgClass(platform, slot), "object-contain")}
        />
      )}
    </div>
  );
}
