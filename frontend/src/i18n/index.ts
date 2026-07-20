/** ClawsomeFlow front-end i18n entry point.
 *
 * Stack: i18next + react-i18next + browser-languagedetector.
 *
 * Conventions:
 * * Two locales — ``en`` (default) and ``zh``.
 * * Detection order: ``localStorage["csflow-lang"]`` only — we
 *   deliberately do NOT auto-pick from ``navigator.language``, so a
 *   Chinese-speaking browser visitor lands in English (the platform's
 *   default) until they explicitly flip the LanguageSwitcher.
 * * Once a user picks a language, the choice is persisted in
 *   localStorage and honoured on every subsequent visit.
 * * The ``<html lang>`` attribute is updated whenever the language
 *   changes — useful for screen readers and CSS `:lang()` selectors.
 * * Keys are typed via ./types.ts so a typo at the call site (or a
 *   missing entry in en.ts/zh.ts) becomes a TS compile error.
 *
 * To add a string:
 *   1. Add it to ./en.ts under the right namespace.
 *   2. Mirror the key in ./zh.ts (TS check enforces parity).
 *   3. Use ``const { t } = useTranslation(); t('namespace.key')``.
 */

import i18n from "i18next";
import LanguageDetector from "i18next-browser-languagedetector";
import { initReactI18next } from "react-i18next";

import en from "./en";
import zh from "./zh";

// Order matters: this is also the render order in LanguageSwitcher.
// Default language ("en") goes first.
export const SUPPORTED_LANGS = ["en", "zh"] as const;
export type SupportedLang = (typeof SUPPORTED_LANGS)[number];

const STORAGE_KEY = "csflow-lang";

i18n
  .use(LanguageDetector)
  .use(initReactI18next)
  .init({
    fallbackLng: "en",
    supportedLngs: [...SUPPORTED_LANGS],
    // Single namespace — keeps the directory simple. The dictionary
    // itself is structured into sections (common / nav / flowList…).
    resources: {
      en: { translation: en },
      zh: { translation: zh },
    },
    interpolation: { escapeValue: false }, // React already escapes
    detection: {
      // localStorage only — never auto-pick from navigator.language, so
      // that English remains the default for first-time visitors
      // regardless of their browser locale.
      order: ["localStorage"],
      lookupLocalStorage: STORAGE_KEY,
      caches: ["localStorage"],
    },
    returnNull: false,
  });

// Reflect the current language onto the <html> element for a11y / CSS.
function reflectLang(lng: string) {
  if (typeof document !== "undefined") {
    document.documentElement.setAttribute("lang", lng.startsWith("zh") ? "zh-CN" : "en");
  }
}
reflectLang(i18n.language);
i18n.on("languageChanged", reflectLang);

/** Best-effort: persist UI language to the backend so webhook text matches. */
function syncUiLanguageToServer(lang: SupportedLang) {
  // Dynamic import avoids any i18n ↔ api circular dependency at module load.
  void import("@/lib/api")
    .then(({ api }) => api.setUiLanguage(lang))
    .catch(() => {
      /* offline / pre-login — localStorage still wins for the SPA */
    });
}

export function changeLang(lang: SupportedLang) {
  void i18n.changeLanguage(lang);
  syncUiLanguageToServer(lang);
}

// Keep server-side notify language in sync after the SPA finishes detecting
// the stored pill (first paint / subsequent visits).
i18n.on("languageChanged", (lng) => {
  const short = (lng || "").split("-")[0];
  if ((SUPPORTED_LANGS as readonly string[]).includes(short)) {
    syncUiLanguageToServer(short as SupportedLang);
  }
});

export function currentLang(): SupportedLang {
  const l = i18n.language?.split("-")[0];
  return (SUPPORTED_LANGS as readonly string[]).includes(l) ? (l as SupportedLang) : "en";
}

export default i18n;
