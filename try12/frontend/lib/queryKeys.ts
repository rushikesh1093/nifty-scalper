// Single source of React Query keys (design §6.4). Invalidate with the `*All` prefixes so a
// mutation can never "invalidate" a key nobody reads.
import type { CandleInterval } from "@/types/market";

export interface SignalsKeyParams {
  strategy?:   string;
  underlying?: string;
  status?:     string;
  limit?:      number;
  today_only?: boolean;
}

export interface PositionsKeyParams {
  is_open?: boolean;
  limit?:   number;
}

export const queryKeys = {
  // Trading desk
  signalsAll:   ["signals"] as const,
  signals:      (p: SignalsKeyParams = {}) => ["signals", p] as const,
  diagnostics:  ["signals-diagnostics"] as const,
  positionsAll: ["positions"] as const,
  positions:    (p: PositionsKeyParams = {}) => ["positions", p] as const,
  capital:      ["capital"] as const,
  config:       ["config"] as const,
  stats:        ["daily-stats"] as const,
  pnlToday:     ["pnl-today"] as const,
  pnlHistoryAll: ["pnl-history"] as const,
  pnlHistory:   (days: number) => ["pnl-history", days] as const,
  mode:         ["mode"] as const,

  // Market
  optionChain:  (underlying: string, expiry: string | null) => ["option-chain", underlying, expiry] as const,
  expiries:     (underlying: string) => ["expiries", underlying] as const,
  tick:         (token: string) => ["tick", token] as const,
  candlesAll:   ["candles"] as const,
  candles:      (token: string, interval: CandleInterval, count: number) => ["candles", token, interval, count] as const,
  levelsAll:    ["levels"] as const,
  levels:       (underlying: string, interval: CandleInterval, maxLevels: number) =>
                  ["levels", underlying, interval, maxLevels] as const,

  // Regime
  regimeCurrent:     (underlying: string) => ["regime-current", underlying] as const,
  regimeHistory:     (underlying: string, hours: number) => ["regime-history", underlying, hours] as const,
  regimePerformance: (underlying?: string) => ["regime-performance", underlying ?? "ALL"] as const,

  // Shell
  session: ["session-status"] as const,
  feed:    ["feed-status"] as const,
  health:  ["health"] as const,

  // Market study
  studyAll:      ["study"] as const,
  studyIntraday: (underlying: string, date?: string) => ["study", "intraday", underlying, date ?? "today"] as const,
  studyDaily:    (underlying: string, days: number) => ["study", "daily", underlying, days] as const,
} as const;
