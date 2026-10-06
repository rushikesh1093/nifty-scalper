import { http } from "@/lib/http";
import type { CandleInterval } from "@/types/market";
import type { LevelsResponse } from "@/types/levels";

/**
 * Support/resistance levels for an index spot series (design §5.3). `lookback` is in bars at
 * `interval` (backend default 500); omitted unless supplied so the server default applies.
 */
export const getLevels = (
  underlying: "NIFTY" | "BANKNIFTY",
  interval: CandleInterval = "ONE_MINUTE",
  maxLevels = 12,
  lookback?: number,
) =>
  http
    .get<LevelsResponse>(`/market/levels/${underlying}`, {
      params: { interval, max_levels: maxLevels, ...(lookback !== undefined ? { lookback } : {}) },
    })
    .then((r) => r.data);
