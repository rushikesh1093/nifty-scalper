// Shared regime presentation: one swatch class per regime (literal Tailwind classes so JIT keeps
// them), a readable label, and a duration formatter. Colour is never the only carrier — every
// segment and legend entry also carries the regime name as text.

const SWATCH: Record<string, string> = {
  BULL_TREND_NORMAL_VOL:  "bg-green",
  BREAKOUT_EVENT_BULLISH: "bg-green/60",
  BEAR_TREND_NORMAL_VOL:  "bg-red",
  BREAKOUT_EVENT_BEARISH: "bg-red/60",
  HIGH_VOL_CHOP:          "bg-yellow/50",
  TREND_HIGH_VOL:         "bg-yellow",
  RANGE_NORMAL_VOL:       "bg-blue",
  LOW_VOL_COMPRESSION:    "bg-blue/50",
  REGIME_TRANSITION:      "bg-accent",
  EXPIRY_DAY:             "bg-text-3",
  UNKNOWN:                "bg-surface-3",
};

const TONE: Record<string, string> = {
  BULL_TREND_NORMAL_VOL:  "text-green",
  BREAKOUT_EVENT_BULLISH: "text-green",
  BEAR_TREND_NORMAL_VOL:  "text-red",
  BREAKOUT_EVENT_BEARISH: "text-red",
  HIGH_VOL_CHOP:          "text-yellow",
  TREND_HIGH_VOL:         "text-yellow",
  RANGE_NORMAL_VOL:       "text-blue",
  LOW_VOL_COMPRESSION:    "text-blue",
  REGIME_TRANSITION:      "text-accent",
};

export const regimeSwatch = (regime: string) => SWATCH[regime] ?? "bg-text-3/50";
export const regimeTone = (regime: string) => TONE[regime] ?? "text-text-2";
/** The detector's error/insufficient-data fallback (regime_detector._fallback_regime). Never shown bare. */
export const UNDETERMINED = "UNKNOWN";
export const regimeLabel = (regime: string | null | undefined) =>
  !regime ? "—" : regime === UNDETERMINED ? "Undetermined (detector fallback)" : regime.replace(/_/g, " ");

/** 45 → "45s", 754 → "12m 34s", 7260 → "2h 01m". */
export function formatDurationSecs(seconds: number | null | undefined): string {
  if (seconds == null || !Number.isFinite(seconds)) return "—";
  const s = Math.max(0, Math.floor(seconds));
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ${(s % 60).toString().padStart(2, "0")}s`;
  const h = Math.floor(m / 60);
  return `${h}h ${(m % 60).toString().padStart(2, "0")}m`;
}
