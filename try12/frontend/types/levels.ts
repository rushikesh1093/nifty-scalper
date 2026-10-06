// GET /market/levels/{underlying} (backend app/schemas/levels.py). Levels are produced only by the
// backend (app/services/market_data/levels.py); the frontend renders them and never computes one.
import type { CandleInterval } from "@/types/market";

export type LevelKind = "SUPPORT" | "RESISTANCE" | "PIVOT";

/** Structural sources plus the derived swing cluster. Open-ended so a new backend source still types. */
export type LevelSource =
  | "SWING"
  | "PDH" | "PDL" | "PDC"
  | "CPR_PIVOT" | "CPR_BC" | "CPR_TC" | "CPR_R1" | "CPR_R2" | "CPR_S1" | "CPR_S2"
  | "ORB_HIGH" | "ORB_LOW"
  | "DAY_HIGH" | "DAY_LOW"
  | (string & {});

export interface PriceLevel {
  price:         number;
  kind:          LevelKind;
  source:        LevelSource;
  /** 1 (weakest) … 5 (strongest). */
  strength:      number;
  touches:       number;
  label:         string;
  last_touch_at: string | null;
}

export interface LevelsMeta {
  candle_source:     "redis" | "db" | "merged" | (string & {});
  /** One-minute rows loaded. */
  candles_used:      number;
  /** Bars at the requested interval that the min-candles guard saw. */
  resampled_bars:    number;
  span_days:         number;
  sources_available: { swing?: boolean; cpr?: boolean; orb?: boolean; prev_day?: boolean; [k: string]: boolean | undefined };
  /** e.g. insufficient_candles, no_prev_day_snapshot, spot_from_candle_close, malformed_candles, no_today_candles */
  warnings:          string[];
  skipped_rows:      number;
  pivot_count:       number;
  cluster_tol:       number;
  prev_day_date:     string | null;
  session_date:      string;
}

export interface LevelsResponse {
  underlying: "NIFTY" | "BANKNIFTY" | (string & {});
  /** Spot index token the levels were computed on (26000 / 26009). */
  token:      string;
  interval:   CandleInterval;
  spot:       number;
  as_of:      string;
  /** Price-descending. */
  levels:     PriceLevel[];
  meta:       LevelsMeta;
}

/** Backend compute_levels min_candles default — only used to phrase "N of 40 bars". */
export const LEVELS_MIN_BARS = 40;
