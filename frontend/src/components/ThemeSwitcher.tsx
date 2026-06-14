/** Compact light/dark theme toggle shown in the AppShell top bar, right next
 * to the LanguageSwitcher and styled to match it (segmented pill).
 *
 * Persists via src/lib/theme.ts (localStorage["csflow-theme"]). Flipping the
 * theme toggles a ``.dark`` class on <html>; the CSS-variable palette
 * (styles.css + tailwind.config.js) re-skins the whole UI instantly. The brand
 * red stays red in both themes — only the neutral surfaces/text invert.
 */

import { useTranslation } from "react-i18next";

import { MoonIcon, SunIcon } from "@/components/icons";
import { cn } from "@/lib/cn";
import { setTheme, THEMES, Theme, useTheme } from "@/lib/theme";

const LABEL_KEY: Record<Theme, "common.themeLight" | "common.themeDark"> = {
  light: "common.themeLight",
  dark: "common.themeDark",
};

export function ThemeSwitcher() {
  const { t } = useTranslation();
  const theme = useTheme();
  return (
    <div
      className="inline-flex rounded-md border border-ink-200 bg-surface text-xs overflow-hidden"
      role="group"
      aria-label={t("common.themeLabel")}
    >
      {THEMES.map((th) => {
        const active = theme === th;
        return (
          <button
            key={th}
            type="button"
            onClick={() => setTheme(th)}
            aria-pressed={active}
            title={t(LABEL_KEY[th])}
            aria-label={t(LABEL_KEY[th])}
            className={cn(
              "inline-flex items-center px-2.5 py-1 transition-colors",
              active
                ? "bg-brand-50 text-brand-700"
                : "text-ink-500 hover:bg-ink-50",
            )}
          >
            {th === "light" ? (
              <SunIcon className="h-4 w-4" />
            ) : (
              <MoonIcon className="h-4 w-4" />
            )}
          </button>
        );
      })}
    </div>
  );
}
