/** Shared agent-platform icon paths and size ratios (sidebar is the reference). */

export type AgentPlatform = "openclaw" | "hermes" | "claude" | "codex" | "cursor";

export type AgentIconSlot = "sidebar" | "card" | "header" | "empty";

type IconScale = "default" | "large" | "compact" | "compactSm";

const PLATFORM_ICONS: Record<AgentPlatform, string> = {
  openclaw: "/agent-icons/openclaw.png",
  hermes: "/agent-icons/hermes.png",
  claude: "/agent-icons/claude.png",
  codex: "/agent-icons/codex.png",
  cursor: "/agent-icons/claude.png",
};

/** Hermes artwork has more inset padding — render larger; Claude/Codex logos are denser. */
const PLATFORM_SCALE: Record<AgentPlatform, IconScale> = {
  openclaw: "default",
  hermes: "large",
  claude: "compact",
  codex: "compactSm",
  cursor: "compact",
};

/** Image span as a fraction of the sidebar's 9-unit slot (numerator / 9). */
const SCALE_NUMERATOR: Record<IconScale, number> = {
  default: 8,
  large: 12,
  compact: 6,
  compactSm: 5,
};

/** Container edge length in Tailwind spacing units (×4px). */
const SLOT_CONTAINER_UNITS: Record<AgentIconSlot, number> = {
  sidebar: 9,
  card: 14,
  header: 11,
  empty: 16,
};

/** Tailwind filter classes that soften the flat vermilion of the raster icons
 *  (agent mascots, timer) toward a lighter coral in BOTH themes, so they match
 *  the softened `text-brandicon` SVG icons instead of glaring. Shared by every
 *  place that renders one of these red PNGs (sidebar nav + agent cards). */
export const SOFT_RED_ICON =
  "saturate-[.7] brightness-[1.15] dark:saturate-[.65] dark:brightness-[1.25]";

export function agentIconSrc(platform: AgentPlatform): string {
  return PLATFORM_ICONS[platform];
}

/** Image class matching sidebar proportions for any layout slot. */
export function agentIconImgClass(platform: AgentPlatform, slot: AgentIconSlot): string {
  const scale = PLATFORM_SCALE[platform];
  const px = Math.round(
    (SCALE_NUMERATOR[scale] / 9) * SLOT_CONTAINER_UNITS[slot] * 4,
  );
  return `h-[${px}px] w-[${px}px]`;
}
