export interface Tick {
  token:        string;
  exchange:     string;
  ltp:          number;
  open?:        number;
  high?:        number;
  low?:         number;
  close?:       number;
  volume?:      number;
  oi?:          number;
  best_bid?:    number;
  best_ask?:    number;
  best_bid_qty?: number;
  best_ask_qty?: number;
  ts?:          string;
}

export interface Candle {
  token:     string;
  symbol?:   string;
  interval:  string;
  timestamp: string;
  open:      number;
  high:      number;
  low:       number;
  close:     number;
  volume?:   number;
  oi?:       number;
}

export interface OptionStrike {
  token:       string;
  symbol:      string;
  strike:      number;
  option_type: "CE" | "PE";
  expiry:      string;
  ltp?:        number;
  iv?:         number;
  oi?:         number;
  volume?:     number;
  delta?:      number;
}

export interface OptionChain {
  underlying:     string;
  underlying_ltp: number;
  expiry:         string;
  strikes:        OptionStrike[];
  pcr?:           number;
  max_pain?:      number;
}

export interface Instrument {
  id:              number;
  token:           string;
  symbol:          string;
  name:            string;
  expiry?:         string;
  strike?:         number;
  option_type?:    string;
  instrument_type: string;
  lot_size:        number;
  tick_size:       number;
  exchange:        string;
  is_active:       boolean;
}

export interface SessionStatus {
  is_logged_in:    boolean;
  client_code?:    string;
  logged_in_at?:   string;
  feed_connected:  boolean;
  subscriptions:   number;
}

export interface FeedStatus {
  connected:     boolean;
  subscriptions: number;
}

/** Tick-feed connection state (store.feedStatus) — an enum, not a boolean. */
export type FeedStatusKind = "unknown" | "connecting" | "live" | "reconnecting" | "down";

/** Candle intervals accepted by /market/candles and /market/levels (backend Literal). */
export type CandleInterval = "ONE_MINUTE" | "THREE_MINUTE" | "FIVE_MINUTE" | "FIFTEEN_MINUTE";

/** GET /regime/current. A Redis failure is a 503 (ApiError), never "UNKNOWN". */
export interface RegimeStatus {
  regime:             string;
  since:              string | null;
  seconds_in_regime:  number;
  /** "absent" = key genuinely missing (no regime detected yet). */
  source:             "redis" | "absent";
}

/** GET /regime/v2 — shadow reading (gates nothing): volatility bucket and opening-range side. */
export interface RegimeV2 {
  underlying:    string;
  as_of:         string;
  regime_gating: boolean;
  v2: {
    label:          string;
    vol:            "LOW" | "NORMAL" | "HIGH" | "UNKNOWN";
    atr_percentile: number | null;
    or_side:        "ABOVE" | "INSIDE" | "BELOW" | "FORMING" | "UNKNOWN";
    or_high:        number | null;
    or_low:         number | null;
    data_ok:        boolean;
  };
}

/** GET /regime/history?collapse=true (default) — ascending collapsed spans. */
export interface RegimeSpan {
  regime:          string;
  from:            string;
  to:              string;
  duration_secs:   number;
  adx:             number | null;
  atr:             number | null;
  previous_regime: string | null;
  samples:         number;
  source:          "db" | "redis";
}

/** GET /regime/performance row. */
export interface RegimePerformanceRow {
  regime:        string;
  underlying:    string;
  strategy_name: string;
  total_trades:  number;
  win_rate:      number;
  total_pnl:     number;
  avg_pnl:       number | null;
}
