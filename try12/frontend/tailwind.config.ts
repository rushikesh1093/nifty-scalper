import type { Config } from "tailwindcss";

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg:        "var(--bg)",
        background:"var(--bg)",
        surface:   "var(--surface)",
        "surface-2":"var(--surface-2)",
        "surface-3":"var(--surface-3)",
        border:    "var(--border)",
        "border-2":"var(--border-2)",
        accent:    "var(--accent)",
        green:     "var(--green)",
        red:       "var(--red)",
        yellow:    "var(--yellow)",
        blue:      "var(--blue)",
        muted:     "var(--text-3)",
        "text-1":  "var(--text-1)",
        "text-2":  "var(--text-2)",
        "text-3":  "var(--text-3)",
      },
      fontFamily: {
        sans: ["Inter", "system-ui", "sans-serif"],
        mono: ["'JetBrains Mono'", "monospace"],
      },
      borderRadius: {
        "2xl": "16px",
        "xl":  "12px",
        "lg":  "8px",
        "md":  "6px",
      },
      boxShadow: {
        card:  "0 1px 3px 0 rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.03)",
        glow:  "0 0 16px 2px rgba(99,102,241,0.25)",
        "glow-green": "0 0 12px 2px rgba(34,211,160,0.2)",
      },
    },
  },
  plugins: [],
};

export default config;
