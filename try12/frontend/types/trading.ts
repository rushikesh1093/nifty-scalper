export interface Signal {
  id:           number;
  strategy:     string;
  underlying:   string;
  direction:    "BUY" | "SELL";
  status:       string;
  spot:         number;
  vwap?:        number;
  strike?:      number;
  symbol?:      string;
  /** Option token the signal suggested (null for SKIPPED rows). */
  suggested_token?: string | null;
  entry_ref?:   number;
  sl_ref?:      number;
  target_ref?:  number;
  rr?:          number;
  mode:         string;
  time:         string;
  indicators?:  Record<string, number>;
  skip_reason?: string;
}

export interface Position {
  id:           number;
  strategy:     string;
  /** Instrument token of the traded contract (chart overlay / live P&L). */
  token:        string;
  symbol:       string;
  underlying:   string;
  strike?:      number;
  option_type?: string;
  expiry?:      string;
  direction:    string;
  lots:         number;
  lot_size?:    number;
  qty:          number;
  entry:        number;
  sl?:          number;
  target?:      number;
  /** Stop at entry; never moved by trailing (backend `initial_stop_loss`). */
  initial_sl?:  number | null;
  trailing_sl?: number;
  exit?:        number;
  exit_reason?: string;
  gross_pnl?:   number;
  /** Statutory charges deducted from gross (backend `statutory_charges`). */
  charges?:     number | null;
  net_pnl?:     number;
  capital_used?: number;
  is_open:      boolean;
  mode:         string;
  partial_closed?:     boolean | null;
  partial_exit_price?: number | null;
  partial_exit_qty?:   number | null;
  partial_pnl?:        number | null;
  remaining_qty?:      number | null;
  entry_time:   string;
  exit_time?:   string;
  /** TREND_ENGINE only: the index-point stop/target behind the premium sl/target. */
  engine_levels?: EngineLevels | null;
}

/** How a TREND_ENGINE trade's INDEX-point stop/target became PREMIUM points (× delta). */
export interface EngineLevels {
  units?:              string;
  delta:               number;
  index_stop_pts:      number;
  index_target_pts:    number;
  premium_stop_pts:    number;
  premium_target_pts:  number;
  sl_pct_bounded?:     boolean;
}

/** GET /analytics/risk-state — today's risk budget (subset the desk uses). */
export interface RiskState {
  trading_day:        string;
  capital:            number;
  trade_count:        number;
  max_trades_per_day: number;
  entry_cutoff_ist:   string;
  square_off_ist:     string;
  daily_loss: {
    combined:      number;
    realised:      number;
    unrealised:    number;
    threshold_abs: number;
    threshold_pct: number;
    breached:      boolean;
  };
  streak: {
    consecutive_losses:         number;
    halt_n:                     number;
    cooldown_minutes_remaining: number | null;
    global_halt:                boolean;
    halted_strategies:          string[];
  };
  kill_switch: { halted: boolean; reason: string | null };
  halts:       unknown[];
}

export type TrendLabel = "UPTREND" | "DOWNTREND" | "SIDEWAYS";
export type TrendStrength = "STRONG" | "MODERATE" | "WEAK";
export type TrendDecisionValue = "BUY CE" | "BUY PE" | "NO TRADE";

export interface TrendDecision {
  underlying:     string;
  candle_time:    string;
  evaluated_at:   string;
  trend:          TrendLabel;
  strength:       TrendStrength;
  decision:       TrendDecisionValue;
  reason_code:    string | null;
  reason:         string;
  spot:           number | null;
  entry_trigger:  string | null;
  entry_zone:     [number, number] | null;
  stop_level:     number | null;
  stop_points:    number | null;
  target1_level:  number | null;
  target1_points: number | null;
  target2_level:  number | null;
  target2_points: number | null;
  risk_reward:    number | null;
  trailing_rule:  string | null;
  confirmation:   string | null;
  invalidation:   string | null;
  evidence:       string[];
  units:          string;
}

export interface TrendDaySummary {
  evaluated:   number;
  buy_ce:      number;
  buy_pe:      number;
  no_trade:    number;
  top_reasons: [string, number][];
}

export interface TrendDecisionsResponse {
  decision_engine: "UNIFIED" | "LEGACY" | (string & {});
  trading_day?:    string;
  latest:          Record<string, TrendDecision | null>;
  summary?:        Record<string, TrendDaySummary>;
  items:           TrendDecision[];
}

export interface DailyStats {
  date:            string;
  mode:            string;
  total_pnl:       number;
  total_trades:    number;
  win_rate:        number;
  open_positions:  number;
  loss_limit:      number;
  capital:         number;
  trade_amount:    number;
  used_capital:    number;
  available:       number;
  loss_pct:        number;
  circuit_breaker_triggered: boolean;
  circuit_reason:  string;
  strategy_breakdown: StrategyBreakdown[];
}

export interface StrategyBreakdown {
  strategy: string;
  trades:   number;
  wins:     number;
  net_pnl:  number;
  win_rate: number;
}

export interface PnLHistory {
  date:            string;
  net_pnl:         number;
  trades:          number;
  wins:            number;
  cumulative_pnl:  number;
}

export interface OptionCandidate {
  rank:        number;
  token:       string;
  symbol:      string;
  strike:      number;
  option_type: string;
  expiry:      string;
  ltp:         number;
  bid:         number;
  ask:         number;
  spread_pct:  number;
  oi:          number;
  volume:      number;
  delta:       number;
  iv:          number;
  score:       number;
  lot_size:    number;
}

export interface TradingConfig {
  mode:                string;
  max_daily_loss:      number;
  max_daily_loss_pct:  number;
  max_open_positions:  number;
  max_lots_per_trade:  number;
  capital:             number;
  trade_amount:        number;
  used_capital:        number;
  available_capital:   number;
  slippage_pct:        number;
  square_off_time:     string;
  strategy_flags:      Record<string, boolean>;
  live_mode_confirmed: boolean;
  study_phase:         boolean;
  max_trades_per_day:  number;
  daily_trades_count:  number;
  /** Persisted kill-switch halt (GET /trading/kill-switch for the reason). */
  trading_halted?:     boolean;
  /** Effective TradingParams (subset): which engine decides trades. */
  params?: { resolved?: { decision_engine?: "UNIFIED" | "LEGACY" | (string & {}) } };
}

export interface CapitalSummary {
  capital:            number;
  used_capital:       number;
  available_capital:  number;
  trade_amount:       number;
  realised_pnl:       number;
  net_capital:        number;
  utilisation_pct:    number;
  max_daily_loss:     number;
  max_daily_loss_pct: number;
  daily_trades:       number;
  max_trades_per_day: number;
  open_positions:     number;
  max_open_positions: number;
  circuit_breaker:    boolean;
  circuit_reason:     string;
  mode:               string;
}

export interface TradePreviewRow {
  ltp:            number;
  lot_size:       number;
  lots:           number;
  capital_used:   number;
  capital_needed: number;
}
