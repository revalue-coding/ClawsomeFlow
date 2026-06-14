/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  // Class strategy: a `.dark` class on <html> flips the theme. The actual
  // color values are driven by CSS variables (see src/styles.css :root / .dark)
  // so every existing `ink-*` / `bg-surface` utility adapts with no per-page
  // `dark:` variants. The ramp mirrors 50<->950 with 500 as the pivot, so
  // `brand-500` (icons, focus rings, primary buttons) is the SAME red in both
  // themes — only the pale tint ends invert.
  darkMode: "class",
  theme: {
    extend: {
      colors: {
        // OpenClaw + accent palette — ALL families are CSS-variable driven so
        // they invert under `.dark` (see styles.css). The ramp mirrors 50<->950
        // with 500 as the pivot, so `brand-500` (icons / primary buttons) stays
        // the SAME red in both themes — only the tint ends (50/100 surfaces and
        // 700/800 text) swap. `slate` is intentionally NOT here: it is a literal
        // self-dark palette used only by the (hidden) Agent Store page.
        brand: {
          50: "rgb(var(--brand-50) / <alpha-value>)",
          100: "rgb(var(--brand-100) / <alpha-value>)",
          200: "rgb(var(--brand-200) / <alpha-value>)",
          300: "rgb(var(--brand-300) / <alpha-value>)",
          400: "rgb(var(--brand-400) / <alpha-value>)",
          500: "rgb(var(--brand-500) / <alpha-value>)",
          600: "rgb(var(--brand-600) / <alpha-value>)",
          700: "rgb(var(--brand-700) / <alpha-value>)",
          800: "rgb(var(--brand-800) / <alpha-value>)",
          900: "rgb(var(--brand-900) / <alpha-value>)",
          950: "rgb(var(--brand-950) / <alpha-value>)",
        },
        amber: {
          50: "rgb(var(--amber-50) / <alpha-value>)",
          100: "rgb(var(--amber-100) / <alpha-value>)",
          200: "rgb(var(--amber-200) / <alpha-value>)",
          300: "rgb(var(--amber-300) / <alpha-value>)",
          400: "rgb(var(--amber-400) / <alpha-value>)",
          500: "rgb(var(--amber-500) / <alpha-value>)",
          600: "rgb(var(--amber-600) / <alpha-value>)",
          700: "rgb(var(--amber-700) / <alpha-value>)",
          800: "rgb(var(--amber-800) / <alpha-value>)",
          900: "rgb(var(--amber-900) / <alpha-value>)",
          950: "rgb(var(--amber-950) / <alpha-value>)",
        },
        rose: {
          50: "rgb(var(--rose-50) / <alpha-value>)",
          100: "rgb(var(--rose-100) / <alpha-value>)",
          200: "rgb(var(--rose-200) / <alpha-value>)",
          300: "rgb(var(--rose-300) / <alpha-value>)",
          400: "rgb(var(--rose-400) / <alpha-value>)",
          500: "rgb(var(--rose-500) / <alpha-value>)",
          600: "rgb(var(--rose-600) / <alpha-value>)",
          700: "rgb(var(--rose-700) / <alpha-value>)",
          800: "rgb(var(--rose-800) / <alpha-value>)",
          900: "rgb(var(--rose-900) / <alpha-value>)",
          950: "rgb(var(--rose-950) / <alpha-value>)",
        },
        emerald: {
          50: "rgb(var(--emerald-50) / <alpha-value>)",
          100: "rgb(var(--emerald-100) / <alpha-value>)",
          200: "rgb(var(--emerald-200) / <alpha-value>)",
          300: "rgb(var(--emerald-300) / <alpha-value>)",
          400: "rgb(var(--emerald-400) / <alpha-value>)",
          500: "rgb(var(--emerald-500) / <alpha-value>)",
          600: "rgb(var(--emerald-600) / <alpha-value>)",
          700: "rgb(var(--emerald-700) / <alpha-value>)",
          800: "rgb(var(--emerald-800) / <alpha-value>)",
          900: "rgb(var(--emerald-900) / <alpha-value>)",
          950: "rgb(var(--emerald-950) / <alpha-value>)",
        },
        indigo: {
          50: "rgb(var(--indigo-50) / <alpha-value>)",
          100: "rgb(var(--indigo-100) / <alpha-value>)",
          200: "rgb(var(--indigo-200) / <alpha-value>)",
          300: "rgb(var(--indigo-300) / <alpha-value>)",
          400: "rgb(var(--indigo-400) / <alpha-value>)",
          500: "rgb(var(--indigo-500) / <alpha-value>)",
          600: "rgb(var(--indigo-600) / <alpha-value>)",
          700: "rgb(var(--indigo-700) / <alpha-value>)",
          800: "rgb(var(--indigo-800) / <alpha-value>)",
          900: "rgb(var(--indigo-900) / <alpha-value>)",
          950: "rgb(var(--indigo-950) / <alpha-value>)",
        },
        fuchsia: {
          50: "rgb(var(--fuchsia-50) / <alpha-value>)",
          100: "rgb(var(--fuchsia-100) / <alpha-value>)",
          200: "rgb(var(--fuchsia-200) / <alpha-value>)",
          300: "rgb(var(--fuchsia-300) / <alpha-value>)",
          400: "rgb(var(--fuchsia-400) / <alpha-value>)",
          500: "rgb(var(--fuchsia-500) / <alpha-value>)",
          600: "rgb(var(--fuchsia-600) / <alpha-value>)",
          700: "rgb(var(--fuchsia-700) / <alpha-value>)",
          800: "rgb(var(--fuchsia-800) / <alpha-value>)",
          900: "rgb(var(--fuchsia-900) / <alpha-value>)",
          950: "rgb(var(--fuchsia-950) / <alpha-value>)",
        },
        orange: {
          50: "rgb(var(--orange-50) / <alpha-value>)",
          100: "rgb(var(--orange-100) / <alpha-value>)",
          200: "rgb(var(--orange-200) / <alpha-value>)",
          300: "rgb(var(--orange-300) / <alpha-value>)",
          400: "rgb(var(--orange-400) / <alpha-value>)",
          500: "rgb(var(--orange-500) / <alpha-value>)",
          600: "rgb(var(--orange-600) / <alpha-value>)",
          700: "rgb(var(--orange-700) / <alpha-value>)",
          800: "rgb(var(--orange-800) / <alpha-value>)",
          900: "rgb(var(--orange-900) / <alpha-value>)",
          950: "rgb(var(--orange-950) / <alpha-value>)",
        },
        sky: {
          50: "rgb(var(--sky-50) / <alpha-value>)",
          100: "rgb(var(--sky-100) / <alpha-value>)",
          200: "rgb(var(--sky-200) / <alpha-value>)",
          300: "rgb(var(--sky-300) / <alpha-value>)",
          400: "rgb(var(--sky-400) / <alpha-value>)",
          500: "rgb(var(--sky-500) / <alpha-value>)",
          600: "rgb(var(--sky-600) / <alpha-value>)",
          700: "rgb(var(--sky-700) / <alpha-value>)",
          800: "rgb(var(--sky-800) / <alpha-value>)",
          900: "rgb(var(--sky-900) / <alpha-value>)",
          950: "rgb(var(--sky-950) / <alpha-value>)",
        },
        cyan: {
          50: "rgb(var(--cyan-50) / <alpha-value>)",
          100: "rgb(var(--cyan-100) / <alpha-value>)",
          200: "rgb(var(--cyan-200) / <alpha-value>)",
          300: "rgb(var(--cyan-300) / <alpha-value>)",
          400: "rgb(var(--cyan-400) / <alpha-value>)",
          500: "rgb(var(--cyan-500) / <alpha-value>)",
          600: "rgb(var(--cyan-600) / <alpha-value>)",
          700: "rgb(var(--cyan-700) / <alpha-value>)",
          800: "rgb(var(--cyan-800) / <alpha-value>)",
          900: "rgb(var(--cyan-900) / <alpha-value>)",
          950: "rgb(var(--cyan-950) / <alpha-value>)",
        },
        blue: {
          50: "rgb(var(--blue-50) / <alpha-value>)",
          100: "rgb(var(--blue-100) / <alpha-value>)",
          200: "rgb(var(--blue-200) / <alpha-value>)",
          300: "rgb(var(--blue-300) / <alpha-value>)",
          400: "rgb(var(--blue-400) / <alpha-value>)",
          500: "rgb(var(--blue-500) / <alpha-value>)",
          600: "rgb(var(--blue-600) / <alpha-value>)",
          700: "rgb(var(--blue-700) / <alpha-value>)",
          800: "rgb(var(--blue-800) / <alpha-value>)",
          900: "rgb(var(--blue-900) / <alpha-value>)",
          950: "rgb(var(--blue-950) / <alpha-value>)",
        },
        violet: {
          50: "rgb(var(--violet-50) / <alpha-value>)",
          100: "rgb(var(--violet-100) / <alpha-value>)",
          200: "rgb(var(--violet-200) / <alpha-value>)",
          300: "rgb(var(--violet-300) / <alpha-value>)",
          400: "rgb(var(--violet-400) / <alpha-value>)",
          500: "rgb(var(--violet-500) / <alpha-value>)",
          600: "rgb(var(--violet-600) / <alpha-value>)",
          700: "rgb(var(--violet-700) / <alpha-value>)",
          800: "rgb(var(--violet-800) / <alpha-value>)",
          900: "rgb(var(--violet-900) / <alpha-value>)",
          950: "rgb(var(--violet-950) / <alpha-value>)",
        },
        green: {
          50: "rgb(var(--green-50) / <alpha-value>)",
          100: "rgb(var(--green-100) / <alpha-value>)",
          200: "rgb(var(--green-200) / <alpha-value>)",
          300: "rgb(var(--green-300) / <alpha-value>)",
          400: "rgb(var(--green-400) / <alpha-value>)",
          500: "rgb(var(--green-500) / <alpha-value>)",
          600: "rgb(var(--green-600) / <alpha-value>)",
          700: "rgb(var(--green-700) / <alpha-value>)",
          800: "rgb(var(--green-800) / <alpha-value>)",
          900: "rgb(var(--green-900) / <alpha-value>)",
          950: "rgb(var(--green-950) / <alpha-value>)",
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
        // Dedicated, non-mirrored tint for decorative red icons. `brand-500` is
        // the brand red used for buttons; icons looked too hot in both themes,
        // so they use this softer coral-red (a touch lighter, slightly
        // desaturated) — set per-theme in styles.css, NOT subject to the brand
        // ramp's light<->dark mirror, so it stays a soft red in both modes.
        brandicon: "rgb(var(--brand-icon) / <alpha-value>)",
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
