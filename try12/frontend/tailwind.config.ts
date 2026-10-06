import type { Config } from "tailwindcss";

// Colours resolve through the `--c-*` RGB channel variables in app/globals.css, so Tailwind's
// opacity modifiers work (`border-border/30`, `text-text-2/70`) — design §6.3 / plan D2.
const channel = (name: string) => `rgb(var(--c-${name}) / <alpha-value>)`;

const config: Config = {
  content: [
    "./app/**/*.{ts,tsx}",
    "./components/**/*.{ts,tsx}",
    "./hooks/**/*.{ts,tsx}",
    "./lib/**/*.{ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        bg:          channel("bg"),
        background:  channel("bg"),
        surface:     channel("surface"),
        "surface-2": channel("surface-2"),
        "surface-3": channel("surface-3"),
        border:      channel("border"),
        "border-2":  channel("border-2"),
        accent:      channel("accent"),
        green:       channel("green"),
        red:         channel("red"),
        yellow:      channel("yellow"),
        blue:        channel("blue"),
        up:          channel("up"),
        down:        channel("down"),
        muted:       channel("text-3"),
        "text-1":    channel("text-1"),
        "text-2":    channel("text-2"),
        "text-3":    channel("text-3"),
        // no "text-4": that token is retired for text (design §6.3); use it only via var(--text-4) for hairlines
      },
      fontFamily: {
        sans: ["var(--font-inter)", "system-ui", "sans-serif"],
        mono: ["var(--font-mono)", "monospace"],
      },
      // Type scale (design §6.3): micro labels, table body, body, panel titles, hero numbers.
      fontSize: {
        micro:   ["10px", { lineHeight: "14px", letterSpacing: "0.05em" }],
        table:   ["11px", { lineHeight: "16px" }],
        body:    ["13px", { lineHeight: "20px" }],
        title:   ["15px", { lineHeight: "22px" }],
        hero:    ["20px", { lineHeight: "28px" }],
        "hero-lg": ["28px", { lineHeight: "34px" }],
      },
      borderRadius: {
        "2xl": "16px",
        "xl":  "12px",
        "lg":  "8px",
        "md":  "6px",
      },
      boxShadow: {
        card:  "0 1px 3px 0 rgba(0,0,0,0.4), 0 0 0 1px rgba(255,255,255,0.03)",
        glow:  "0 0 16px 2px rgb(var(--c-accent) / 0.25)",
        "glow-green": "0 0 12px 2px rgb(var(--c-green) / 0.2)",
      },
    },
  },
  plugins: [],
};
export default config;
