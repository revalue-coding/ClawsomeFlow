/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // Class strategy: a `.dark` class on <html> flips the theme. The actual
  // color values are driven by CSS variables (see src/styles.css :root / .dark)
  // so every existing `ink-*` / `bg-surface` utility adapts with no per-page
  // `dark:` variants. `brand` stays a literal red in BOTH themes — the lobster
  // shell accent (icons, focus rings) is intentionally theme-independent.
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // OpenClaw-inspired palette: warm crimson accents on warm grays.
        // Literal hex (theme-independent) — keeps icons/accents red in dark mode.
        brand: {
          50: "#fff5f4",
          100: "#ffe4e1",
          200: "#fbc8c2",
          300: "#f49b91",
          400: "#e96a5c",
          500: "#dd3f30",   // primary (red lobster shell)
          600: "#c12d1f",
          700: "#9d2317",
          800: "#7c1c12",
          900: "#5b150d",
        },
        // Neutral scale — CSS-variable driven so it inverts under `.dark`.
        // Light values live in :root, dark values in `.dark` (src/styles.css).
        ink: {
          50: "rgb(var(--ink-50) / <alpha-value>)",
          100: "rgb(var(--ink-100) / <alpha-value>)",
          200: "rgb(var(--ink-200) / <alpha-value>)",
          300: "rgb(var(--ink-300) / <alpha-value>)",
          400: "rgb(var(--ink-400) / <alpha-value>)",
          500: "rgb(var(--ink-500) / <alpha-value>)",
          600: "rgb(var(--ink-600) / <alpha-value>)",
          700: "rgb(var(--ink-700) / <alpha-value>)",
          800: "rgb(var(--ink-800) / <alpha-value>)",
          900: "rgb(var(--ink-900) / <alpha-value>)",
        },
        // Raised-surface background (cards, sidebar, top bar, inputs, popovers).
        // Was `bg-white` everywhere; now a token that flips to a dark panel.
        // NOTE: `text-white` stays literal white (on-color text on brand) — only
        // surface *backgrounds* use this token.
        surface: "rgb(var(--surface) / <alpha-value>)",
      },
      fontFamily: {
        sans: [
          "Inter",
          "-apple-system",
          "BlinkMacSystemFont",
          "Segoe UI",
          "Roboto",
          "sans-serif",
        ],
      },
      boxShadow: {
        card: "0 1px 2px rgba(0,0,0,0.04), 0 1px 1px rgba(0,0,0,0.04)",
        // Subtle warm-brand glow used to lift focused / hovered surfaces
        // — keeps the palette tied to the lobster-red brand while giving
        // the UI a slightly more "tech" feel.
        glow: "0 0 24px -6px rgba(221, 63, 48, 0.45)",
        "glow-sm": "0 0 14px -4px rgba(221, 63, 48, 0.35)",
      },
      keyframes: {
        // Soft brand pulse — used on the brand badge & a few accent chips.
        "pulse-glow": {
          "0%, 100%": {
            boxShadow: "0 0 0 0 rgba(221, 63, 48, 0.0), 0 0 18px -6px rgba(221, 63, 48, 0.45)",
          },
          "50%": {
            boxShadow: "0 0 0 4px rgba(221, 63, 48, 0.08), 0 0 30px -4px rgba(221, 63, 48, 0.55)",
          },
        },
        // Slow scan-line drift used as a background accent on the shell.
        "scan-drift": {
          "0%": { backgroundPosition: "0% 0%" },
          "100%": { backgroundPosition: "0% 100%" },
        },
        // Brand-gradient sweep across text — paired with `bg-clip-text` +
        // `text-transparent` to produce a metallic shimmer on hype copy.
        "text-shimmer": {
          "0%": { backgroundPosition: "0% 50%" },
          "100%": { backgroundPosition: "200% 50%" },
        },
        // Soft pulsing drop-shadow used together with the shimmer so the
        // hype tagline visibly "breathes" even when the user isn't moving.
        "text-glow": {
          "0%, 100%": {
            filter: "drop-shadow(0 0 4px rgba(221, 63, 48, 0.35))",
          },
          "50%": {
            filter: "drop-shadow(0 0 10px rgba(221, 63, 48, 0.7))",
          },
        },
      },
      animation: {
        "pulse-glow": "pulse-glow 3.2s ease-in-out infinite",
        "scan-drift": "scan-drift 22s linear infinite",
        "text-shimmer": "text-shimmer 5s linear infinite",
        "text-glow": "text-glow 3.4s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
