// Resolved colour literals for JS charting APIs (lightweight-charts, recharts), read from the same
// `--c-*` channel variables as the CSS (plan D2) — one palette source, nothing to drift.
// Client-only: call from effects / event handlers, never during SSR render.

export const THEME_TOKENS = [
  "bg", "surface", "surface-2", "surface-3", "border", "border-2", "accent",
  "green", "red", "yellow", "blue", "up", "down", "text-1", "text-2", "text-3", "text-4",
] as const;
export type ThemeToken = (typeof THEME_TOKENS)[number];

/** Neutral grey used only if a variable cannot be read (stylesheet missing). */
const MISSING_CHANNELS = "128 128 128";

function readChannels(token: ThemeToken): string {
  if (typeof window === "undefined") return MISSING_CHANNELS;
  const raw = getComputedStyle(document.documentElement).getPropertyValue(`--c-${token}`).trim();
  return /^\d{1,3}\s+\d{1,3}\s+\d{1,3}$/.test(raw) ? raw : MISSING_CHANNELS;
}

/** `rgb(r, g, b)` or `rgba(r, g, b, a)` for a token — the comma form every chart lib accepts. */
export function themeColor(token: ThemeToken, alpha?: number): string {
  const [r, g, b] = readChannels(token).split(/\s+/);
  return alpha === undefined ? `rgb(${r}, ${g}, ${b})` : `rgba(${r}, ${g}, ${b}, ${alpha})`;
}

export interface ChartTheme {
  color: (token: ThemeToken, alpha?: number) => string;
  fontFamily: string;
  /** Spread into lightweight-charts createChart() options (v4 shape). */
  lightweight: {
    layout: { background: { type: "solid"; color: string }; textColor: string; fontFamily: string; fontSize: number };
    grid: { vertLines: { color: string }; horzLines: { color: string } };
    rightPriceScale: { borderColor: string };
    timeScale: { borderColor: string };
    crosshair: { vertLine: { color: string; labelBackgroundColor: string }; horzLine: { color: string; labelBackgroundColor: string } };
  };
  /** Shared recharts styling. */
  recharts: {
    axis: { stroke: string; tick: { fill: string; fontSize: number }; tickLine: false; axisLine: { stroke: string } };
    grid: { stroke: string; strokeDasharray: string };
    tooltip: {
      contentStyle: { background: string; border: string; borderRadius: number; fontSize: number; color: string };
      labelStyle: { color: string };
      itemStyle: { color: string };
      cursor: { fill: string };
    };
  };
}

export function getChartTheme(): ChartTheme {
  const fontFamily =
    typeof window === "undefined"
      ? "monospace"
      : `${getComputedStyle(document.documentElement).getPropertyValue("--font-mono").trim() || "monospace"}, monospace`;
  const c = themeColor;
  return {
    color: c,
    fontFamily,
    lightweight: {
      layout: { background: { type: "solid", color: c("surface") }, textColor: c("text-2"), fontFamily, fontSize: 11 },
      grid: { vertLines: { color: c("border", 0.5) }, horzLines: { color: c("border", 0.5) } },
      rightPriceScale: { borderColor: c("border") },
      timeScale: { borderColor: c("border") },
      crosshair: {
        vertLine: { color: c("text-3", 0.6), labelBackgroundColor: c("surface-3") },
        horzLine: { color: c("text-3", 0.6), labelBackgroundColor: c("surface-3") },
      },
    },
    recharts: {
      axis: { stroke: c("border"), tick: { fill: c("text-3"), fontSize: 10 }, tickLine: false, axisLine: { stroke: c("border") } },
      grid: { stroke: c("border", 0.6), strokeDasharray: "3 3" },
      tooltip: {
        contentStyle: { background: c("surface-2"), border: `1px solid ${c("border-2")}`, borderRadius: 8, fontSize: 11, color: c("text-1") },
        labelStyle: { color: c("text-2") },
        itemStyle: { color: c("text-1") },
        cursor: { fill: c("surface-3", 0.5) },
      },
    },
  };
}
