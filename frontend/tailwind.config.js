/** @type {import('tailwindcss').Config} */
export default {
  content: ["./index.html", "./src/**/*.{ts,tsx}"],
  theme: {
    extend: {
      colors: {
        // OpenClaw-inspired palette: warm crimson accents on warm grays.
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
        ink: {
          50: "#f9fafb",
          100: "#f3f4f6",
          200: "#e5e7eb",
          300: "#d1d5db",
          400: "#9ca3af",
          500: "#6b7280",
          600: "#4b5563",
          700: "#374151",
          800: "#1f2937",
          900: "#111827",
        },
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
