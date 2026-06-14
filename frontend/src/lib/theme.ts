/** Color-theme (light/dark) controller — the visual sibling of the i18n
 * language switch.
 *
 * Mirrors the i18n conventions deliberately:
 *  * Persisted to ``localStorage["csflow-theme"]`` only — first-time visitors
 *    always land in **light** (we do NOT read ``prefers-color-scheme``), so the
 *    default presentation is stable regardless of OS setting until the user
 *    flips the toggle.
 *  * Flipping the theme toggles a ``.dark`` class on ``<html>``; every color
 *    is CSS-variable driven (see ``styles.css`` + ``tailwind.config.js``), so
 *    the whole tree re-skins instantly with no React re-render needed for the
 *    colors themselves. The tiny ``useTheme`` store exists only so the toggle
 *    control can show which option is active.
 *
 * A no-flash inline script in ``index.html`` applies the stored class before
 * first paint; ``initTheme()`` (called from ``main.tsx``) re-syncs at runtime.
 */

import { useSyncExternalStore } from "react";

export const THEMES = ["light", "dark"] as const;
export type Theme = (typeof THEMES)[number];

const STORAGE_KEY = "csflow-theme";

function readStored(): Theme | null {
  try {
    const v = window.localStorage.getItem(STORAGE_KEY);
    return v === "light" || v === "dark" ? v : null;
  } catch {
    return null;
  }
}

// Default to light — matches the i18n "English by default" stance: explicit
// user choice over environment sniffing.
let current: Theme = readStored() ?? "light";

const listeners = new Set<() => void>();

function applyClass(theme: Theme): void {
  if (typeof document !== "undefined") {
    document.documentElement.classList.toggle("dark", theme === "dark");
  }
}

/** Apply the persisted theme to <html>. Call once at app startup. */
export function initTheme(): void {
  applyClass(current);
}

export function getTheme(): Theme {
  return current;
}

export function setTheme(theme: Theme): void {
  if (theme === current) return;
  current = theme;
  try {
    window.localStorage.setItem(STORAGE_KEY, theme);
  } catch {
    /* localStorage disabled / quota — ignore, just skip persistence */
  }
  applyClass(theme);
  listeners.forEach((l) => l());
}

export function toggleTheme(): void {
  setTheme(current === "dark" ? "light" : "dark");
}

function subscribe(cb: () => void): () => void {
  listeners.add(cb);
  return () => {
    listeners.delete(cb);
  };
}

/** React hook: re-renders the caller whenever the theme changes. */
export function useTheme(): Theme {
  return useSyncExternalStore(subscribe, getTheme, getTheme);
}
