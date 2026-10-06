// Mirrors backend app/schemas/diagnostics.py (GET /trading/signals/diagnostics).
// Counts are nullable: null means "unknown" (probe failed / DB down), never zero.

export type BlockerCode =
  | "MARKET_CLOSED"
  | "DB_UNAVAILABLE"
  | "RUNNER_STOPPED"
  | "FEED_DOWN"
  | "STUDY_PHASE"
  | "VIX_OUT_OF_RANGE"
  | "NO_CANDLES_TODAY"
  | "INSUFFICIENT_CANDLES"
  | "ATR_BELOW_MIN"
  | "NO_PREV_DAY_SNAPSHOT"
  | "REGIME_BLOCKS_ALL"
  | "REGIME_TRANSITION_COOLDOWN"
  | "CAPITAL_BELOW_LOT_COST"
  | "ALL_STRATEGIES_DISABLED"
  | "DAILY_TRADE_CAP_REACHED"
  | "MAX_OPEN_POSITIONS_REACHED"
  | "CIRCUIT_BREAKER"
  // Probe-failure codes (severity "warning", message "probe unavailable"), e.g. FEED_PROBE,
  // REDIS_UNAVAILABLE, VIX_PROBE, CANDLES_PROBE_NIFTY, REGIME_PROBE_NIFTY, CONFIG_PROBE, LOT_COST_PROBE_NIFTY.
  | (string & {});

export interface Blocker {
  code:        BlockerCode;
  severity:    "blocking" | "warning";
  message:     string;
  underlying?: string | null;
  detail?:     string | null;
}

export interface FeedProbe {
  connected:     boolean | null;
  subscriptions: number | null;
  session_valid: boolean | null;
}

export interface SignalsProbe {
  today_count:          number | null;
  by_status:            Record<string, number>;
  last_signal_at:       string | null;
  last_signal_age_secs: number | null;
}

export interface PositionsProbe {
  open:        number | null;
  today_total: number | null;
}

export interface UnderlyingProbe {
  underlying:            string;
  spot:                  number | null;
  candles_today:         number | null;
  candles_required:      number;
  atr:                   number | null;
  atr_min:               number;
  last_candle_at:        string | null;
  regime:                string | null;
  regime_allows_trading: boolean | null;
  prev_day_snapshot:     string | null;
}

export interface VixProbe {
  ltp:  number | null;
  min:  number;
  max:  number;
  gate: "PASS" | "BLOCKED" | "BYPASSED";
}

export interface CapitalProbe {
  capital:            number | null;
  available:          number | null;
  trade_amount:       number | null;
  daily_trades:       number | null;
  max_trades_per_day: number | null;
  open_positions:     number | null;
  max_open_positions: number | null;
  circuit_breaker:    boolean | null;
  circuit_reason:     string | null;
}

export interface StrategiesProbe {
  total:    number | null;
  enabled:  number | null;
  disabled: string[];
}

export interface SignalDiagnostics {
  as_of:               string;
  /** Clock-based: IST weekday within the session window. Exchange holidays are not detected. */
  session_window_open: boolean;
  /** e.g. "09:15-15:25 IST" — render this, not a local constant (finding 13). */
  session_window:      string;
  session_window_note: string;
  runner_running:      boolean | null;
  mode:                string | null;
  study_phase:         boolean | null;
  feed:                FeedProbe;
  signals:             SignalsProbe;
  positions:           PositionsProbe;
  underlyings:         UnderlyingProbe[];
  vix:                 VixProbe;
  capital:             CapitalProbe;
  strategies:          StrategiesProbe;
  blockers:            Blocker[];
}
