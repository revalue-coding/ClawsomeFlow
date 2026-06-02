/** Compact language toggle (zh ⇄ en) shown in the AppShell top bar.
 *
 * Persists to localStorage via i18next-browser-languagedetector. Updating
 * the language triggers a tree-wide re-render through react-i18next's
 * context, so every consumer of ``t(...)`` flips immediately — no page
 * reload required.
 */

import { useTranslation } from "react-i18next";

import { changeLang, currentLang, SUPPORTED_LANGS, SupportedLang } from "@/i18n";
import { cn } from "@/lib/cn";

const LABELS: Record<SupportedLang, string> = {
  zh: "ZH",
  en: "EN",
};

export function LanguageSwitcher() {
  const { t } = useTranslation();
  const lang = currentLang();
  return (
    <div
      className="inline-flex rounded-md border border-ink-200 bg-white text-xs overflow-hidden"
      role="group"
      aria-label={t("common.languageLabel")}
    >
      {SUPPORTED_LANGS.map((l) => (
        <button
          key={l}
          type="button"
          onClick={() => changeLang(l)}
          aria-pressed={lang === l}
          className={cn(
            "px-2.5 py-1 transition-colors",
            lang === l
              ? "bg-brand-50 text-brand-700 font-semibold"
              : "text-ink-500 hover:bg-ink-50",
          )}
        >
          {LABELS[l]}
        </button>
      ))}
    </div>
  );
}
