// Display helpers shared by the backtest components. Null is always "—", never 0 / 0 %.
import type { BadgeIntent } from "@/components/ui/Badge";
import type { JobState, Recommendation, RunStatus } from "@/types/backtest";

const isNum = (v: unknown): v is number => typeof v === "number" && Number.isFinite(v);

/** A 0..1 fraction as a percentage. */
export const fmtFraction = (v: number | null | undefined, d = 2) => (isNum(v) ? `${(v * 100).toFixed(d)} %` : "—");
/** A value already in percent. */
export const fmtPct = (v: number | null | undefined, d = 2) => (isNum(v) ? `${v.toFixed(d)} %` : "—");
export const fmtR = (v: number | null | undefined, d = 3) =>
  (isNum(v) ? `${v > 0 ? "+" : v < 0 ? "−" : ""}${Math.abs(v).toFixed(d)}R` : "—");
export const fmtInt = (v: number | null | undefined) => (isNum(v) ? v.toLocaleString("en-IN") : "—");
export const fmtInr = (v: number | null | undefined, d = 0) =>
  (isNum(v) ? `₹${v.toLocaleString("en-IN", { minimumFractionDigits: d, maximumFractionDigits: d })}` : "—");

export const STATUS_INTENT: Record<RunStatus, BadgeIntent> = {
  COMPLETE: "positive", RUNNING: "info", NO_DATA: "warning", FAILED: "negative",
};

export const RECOMMENDATION_INTENT: Record<Recommendation, BadgeIntent> = {
  CONSIDER: "positive", "NOT YET": "warning",
};

/** The closed JobState enum (design §6.4): nine states, no gaps. */
export const JOB_STATE_INTENT: Record<JobState, BadgeIntent> = {
  RUNNING: "neutral", SUCCEEDED: "positive", NO_DATA: "warning",
  FAILED: "negative", INVALID: "negative", TIMEOUT: "negative",
  REFUSED: "negative", ERROR: "negative", CANCELLED: "negative",
};

export function runHref(runId: string, dataset: string): string {
  return `/backtest/${encodeURIComponent(runId)}?dataset=${encodeURIComponent(dataset)}`;
}
