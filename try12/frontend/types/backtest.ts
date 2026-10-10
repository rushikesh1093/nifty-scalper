// Backtest API payloads (design §4.6, §4.7), mirroring app/api/routes/backtest.py and
// app/services/backtest/{read,series,jobs}.py field for field. Nullable fields are always present
// (serialised as null, never omitted) unless marked optional.

// ── Shared enums ────────────────────────────────────────────────────────────

/** Provenance derived at the read layer (§4.9). There is no REAL state in v1. */
export type SyntheticState = "SYNTHETIC" | "UNVERIFIED";
export type SyntheticSource = "validation" | "dataset" | "label" | "none";
export type RunStatus = "COMPLETE" | "RUNNING" | "NO_DATA" | "FAILED";
export type ValidationSource = "db" | "artifact" | "none";
export type DataSource = "store" | "artifact";
export type Recommendation = "CONSIDER" | "NOT YET";
export type BreakdownDimension = "strategy" | "regime" | "strategy_regime" | "underlying" | "day";
export type BinWidth = 0.1 | 0.25 | 0.5;

// ── GET /backtest/datasets ──────────────────────────────────────────────────

export type DatasetKind = "replay_db" | "synthetic" | "artifacts";

export interface Dataset {
  dataset_id: string;
  kind:       DatasetKind;
  label:      string;
  synthetic:  boolean;
  exists:     boolean;
  /** null when the store could not be counted (`error: "unreachable"`). */
  runs:       number | null;
  error:      "unreachable" | null;
}

export interface DatasetsResponse {
  items:                   Dataset[];
  total:                   number;
  replay_store_configured: boolean;
  replay_store_error:      "not_configured" | "refused_main_db" | "unreachable" | null;
  /** The synthetic id replay-db aliases; null = no alias. */
  replay_db_alias_of:      string | null;
}

// ── GET /backtest/runs ──────────────────────────────────────────────────────

export interface RunSummary {
  run_id:               string;
  dataset:              string;
  status:               RunStatus;
  created_at:           string | null;
  created_at_source:    "row" | "file_mtime";
  label:                string | null;
  date_from:            string | null;
  date_to:              string | null;
  underlyings:          string[] | null;
  seed:                 number | null;
  /** null when the run row has no metrics (NO_DATA / RUNNING / FAILED). */
  trades:               number | null;
  sessions:             number;
  trades_per_day:       number | null;
  /** Fraction 0..1. */
  win_rate:             number | null;
  expectancy_r:         number | null;
  expectancy_inr:       number | null;
  /** Percent of capital. */
  max_drawdown_pct:     number | null;
  recommendation:       Recommendation | null;
  failing_criteria:     number[] | null;
  caps:                 string[] | null;
  synthetic_state:      SyntheticState;
  synthetic_source:     SyntheticSource;
  params_hash:          string | null;
  determinism_hash:     string | null;
  code_version:         string | null;
  per_trade_available:  boolean;
  metrics_available:    boolean;
  breakdowns_available: boolean;
  validation_source:    ValidationSource;
}

export interface RunsQuery {
  dataset?: string;
  limit?:   number;
  offset?:  number;
}

export interface RunsResponse {
  items:        RunSummary[];
  total:        number;
  /** Echo of an explicit `dataset` filter, else null. */
  dataset:      string | null;
  /** "artifacts_unavailable" | "store_unreachable:<dataset_id>" */
  warnings:     string[];
  truncated:    boolean;
  /** Only the tiers that hit their cap. */
  caps:         { store?: number; artifacts?: number };
  empty_reason: "no_runs" | "replay_store_not_initialised" | null;
}

// ── Validation report (sections typed only where the UI reads them) ────────

export interface Criterion {
  n:      number;
  name:   string;
  set:    string;
  passed: boolean;
  actual: string | number | null;
  rule:   string;
}

export interface ValidationReport {
  run_id?:              string;
  label?:               string | null;
  synthetic?:           boolean;
  banner?:              string | null;
  recommendation?:      Recommendation;
  recommendation_line?: string;
  failing_criteria?:    number[];
  caps?:                string[];
  sections?: {
    "1"?:  Record<string, unknown>;
    "2"?:  CoverageReport;
    "4"?:  Record<string, unknown>;
    "7"?:  { split_fraction?: number } & Record<string, unknown>;
    "8"?:  Record<string, unknown>;
    "9"?:  { declared?: string } & Record<string, unknown>;
    "11"?: { criteria: Criterion[]; caps: string[]; recommendation: Recommendation; line: string };
    [k: string]: unknown;
  };
  [k: string]: unknown;
}

export interface CoverageUnderlyingSummary {
  session_days: number;
  tradeable:    number;
  skipped:      Record<string, number>;
  mean_option_ticks_per_tradeable_day: number | null;
}

export interface CoverageDay {
  day:        string;
  underlying: string;
  status:     string;
  [k: string]: unknown;
}

export interface CoverageReport {
  summary?: Record<string, CoverageUnderlyingSummary>;
  days?:    CoverageDay[];
  [k: string]: unknown;
}

// ── GET /backtest/runs/{run_id} ─────────────────────────────────────────────

export interface RunDetail {
  run_id:               string;
  dataset:              string;
  status:               RunStatus;
  created_at:           string | null;
  created_at_source:    "row" | "file_mtime";
  label:                string | null;
  notes:                string | null;
  date_from:            string | null;
  date_to:              string | null;
  underlyings:          string[] | null;
  seed:                 number | null;
  oos_split:            number | null;
  code_version:         string | null;
  params_hash:          string | null;
  determinism_hash:     string | null;
  /** The run's frozen record ({cfg, params, label, …}). */
  params:               Record<string, unknown> | null;
  coverage:             CoverageReport | null;
  metrics_row:          Record<string, unknown> | null;
  per_trade_available:  boolean;
  metrics_available:    boolean;
  breakdowns_available: boolean;
  sessions:             number;
  validation:           ValidationReport | null;
  validation_source:    ValidationSource;
  synthetic_state:      SyntheticState;
  synthetic_source:     SyntheticSource;
  /** 0 when unknown; every _pct field is then null. */
  capital:              number;
  warnings:             string[];
}

// ── Metrics ─────────────────────────────────────────────────────────────────

export interface MetricSet {
  trades:                  number;
  wins:                    number;
  losses:                  number;
  flats:                   number;
  win_rate:                number | null;
  avg_win_inr:             number | null;
  avg_loss_inr:            number | null;
  win_loss_ratio_inr:      number | null;
  avg_win_r:               number | null;
  avg_loss_r:              number | null;
  win_loss_ratio_r:        number | null;
  r_flat_trades:           number;
  expectancy_r:            number | null;
  expectancy_r_net:        number | null;
  expectancy_inr:          number | null;
  profit_factor:           number | null;
  gross_profit:            number | null;
  gross_loss:              number | null;
  median_r:                number | null;
  stdev_r:                 number | null;
  max_equity_drawdown_inr: number | null;
  max_equity_drawdown_pct: number | null;
  worst_trade_loss:        number | null;
  longest_losing_streak:   number | null;
  avg_hold_minutes:        number | null;
  r_unavailable:           number;
  decided_r:               number;
  r_sample_size:           number;
  low_sample:              boolean;
  sample_size:             number;
}

export interface DailyMetricSet {
  mean_daily_pnl:     number | null;
  stdev_daily_pnl:    number | null;
  sharpe_like:        number | null;
  sortino_like:       number | null;
  trading_days:       number;
  positive_days:      number;
  assumptions:        string;
  sharpe_assumption:  string;
  sortino_assumption: string;
}

/** [lo, hi]; either bound null when undefined (e.g. n = 0). */
export type Interval = [number | null, number | null];

export interface RunMetrics {
  run_id:                      string;
  dataset:                     string;
  source:                      DataSource;
  capital:                     number;
  metrics:                     MetricSet;
  daily:                       DailyMetricSet | null;
  sessions:                    number;
  trades_per_day:              number | null;
  win_rate_wilson_95:          Interval;
  mean_cost_inr:               number | null;
  cost_sample_inr:             number;
  mean_cost_r:                 number | null;
  cost_sample_r:               number;
  expectancy_r_ci_day_block:   Interval | null;
  expectancy_inr_ci_day_block: Interval | null;
  ci_source:                   "validation" | "none";
  empty_reason:                string | null;
  warnings:                    string[];
}

export interface GroupedMetrics {
  run_id:       string;
  dataset:      string;
  dimension:    BreakdownDimension;
  groups:       Record<string, MetricSet>;
  total:        number;
  source:       DataSource;
  empty_reason: string | null;
}

// ── No-trade decisions ──────────────────────────────────────────────────────

export interface NoTradeItem {
  stage: string;
  code:  string;
  count: number;
}

export interface NoTradeSummary {
  run_id:       string;
  dataset:      string;
  items:        NoTradeItem[];
  total:        number;
  by_stage:     { stage: string; count: number }[];
  source:       DataSource;
  empty_reason: string | null;
}

// ── Trades (tier 1 only) ────────────────────────────────────────────────────

export type TradeOutcome = "WIN" | "LOSS" | "FLAT";

export interface TradeRow {
  position_id:         number;
  run_id:              string;
  trading_day:         string;
  underlying:          string;
  strategy_name:       string;
  entry_regime:        string | null;
  direction:           string | null;
  strike:              number | null;
  option_type:         string | null;
  expiry:              string | null;
  dte_at_entry:        number | null;
  lots:                number;
  quantity:            number;
  actual_entry:        number;
  initial_stop_loss:   number | null;
  initial_risk:        number | null;
  target:              number | null;
  actual_exit:         number | null;
  exit_reason:         string | null;
  exit_value_weighted: number | null;
  gross_pnl:           number;
  brokerage:           number;
  statutory_charges:   number;
  net_pnl:             number;
  r_multiple:          number | null;
  r_multiple_net:      number | null;
  r_source:            string | null;
  entry_time:          string | null;
  exit_time:           string | null;
  hold_minutes:        number | null;
  entry_fill_source:   string | null;
  exit_fill_source:    string | null;
  sizing_method:       string | null;
  target_stretched:    boolean;
  partial_closed:      boolean;
  mode:                string;
  outcome:             TradeOutcome;
  r_valid:             boolean;
}

export interface TradesQuery {
  limit?:       number;
  offset?:      number;
  strategy?:    string;
  regime?:      string;
  outcome?:     TradeOutcome;
  exit_reason?: string;
}

export interface TradesPage {
  run_id:        string;
  dataset:       string;
  items:         TradeRow[];
  total:         number;
  /** Present only when total is 0. */
  empty_reason?: string;
}

// ── Series (tier 1 only) ────────────────────────────────────────────────────

export interface EquityPoint {
  i:            number;
  exit_time:    string | null;
  trading_day:  string | null;
  net_pnl:      number;
  cum_net_inr:  number;
  cum_net_pct:  number | null;
  drawdown_inr: number;
  drawdown_pct: number | null;
  r_multiple:   number | null;
  exit_reason:  string | null;
}

export interface ExitReasonBucket {
  exit_reason: string;
  count:       number;
  net_inr:     number;
  avg_r:       number | null;
  wins:        number;
  losses:      number;
}

export interface RHistogram {
  bin_width:     number;
  bins:          { lo: number; hi: number; count: number }[];
  r_valid:       number;
  r_unavailable: number;
  clipped_low:   number;
  clipped_high:  number;
}

export interface DailyPoint {
  trading_day: string;
  net_pnl:     number;
  trades:      number;
  cum_net_inr: number;
}

export interface SeriesPayload {
  run_id:           string;
  dataset:          string;
  capital:          number;
  trades:           number;
  downsampled:      boolean;
  equity:           EquityPoint[];
  max_drawdown_inr: number | null;
  max_drawdown_pct: number | null;
  exit_reasons:     ExitReasonBucket[];
  r_histogram:      RHistogram;
  daily:            DailyPoint[];
  empty_reason:     string | null;
}

// ── Launcher: GET /backtest/launch-options ──────────────────────────────────

export interface LaunchOptionDataset {
  dataset_id: string;                         // "replay-db" | "synthetic"
  kind:       "replay_db" | "synthetic_template";
  label:      string;
  synthetic:  boolean;
  alias_of:   string | null;                  // always present; null on the template
  exists:     boolean | null;                 // null on the template
  /** false ⇔ POST /jobs would 400 replay_store_not_initialised. */
  launchable: boolean;
}

export interface NumericBound {
  min:     number;
  max:     number;
  default: number;
}

export interface LaunchLimits {
  max_range_days:        number;
  max_launch_range_days: number;
  synthetic_min_days:    number;
  synthetic_max_days:    number;
  job_timeout_s:         number;
  bootstrap_iterations:  NumericBound;
  oos_split:             NumericBound;
}

/** The twelve launcher knobs, pre-filled from TradingParams() defaults (§6.4). */
export interface OverrideDefaults {
  capital:                     number;
  fixed_fraction_pct:          number;
  kelly_fraction_cap_pct:      number;
  max_lots_per_trade:          number;
  partial_exit_enabled:        boolean;
  max_open_positions:          number;
  max_daily_loss:              number;
  max_daily_loss_pct:          number;
  max_trades_per_day:          number;
  open_exposure_cap_pct:       number;
  max_hold_minutes:            Record<string, number>;
  transition_cooldown_candles: number;
}

export interface LaunchOptions {
  admin_key_configured: boolean;
  datasets:             LaunchOptionDataset[];
  limits:               LaunchLimits;
  defaults:             { seed: number; overrides: OverrideDefaults };
  underlyings:          string[];
  running_job_id:       string | null;
  max_concurrent_jobs:  number;
}

// ── Launcher: POST /backtest/params/validate, POST /backtest/jobs ───────────

export interface ValidateResponse {
  valid:      boolean;
  violations: string[];
}

export type DatasetSpec =
  | { kind: "replay_db" }
  | { kind: "synthetic"; days: number; seed: number; start: string };

/** POST /jobs body. There is no database_url field: the server picks the target (extra keys → 422). */
export interface LaunchRequest {
  dataset:               DatasetSpec;
  date_from:             string;
  date_to:               string;
  underlyings:           string[];
  seed?:                 number;
  oos_split?:            number;
  bootstrap_iterations?: number;
  label?:                string;
  notes?:                string;
  overrides?:            Record<string, unknown>;
}

// ── Jobs ────────────────────────────────────────────────────────────────────

/** Closed enum; no QUEUED — RUNNING is the whole live set, everything else is terminal. */
export type JobState =
  | "RUNNING" | "SUCCEEDED" | "NO_DATA" | "FAILED" | "INVALID"
  | "TIMEOUT" | "REFUSED" | "ERROR" | "CANCELLED";

export type JobStep = "dataset" | "replay";

export interface JobRecord {
  job_id:                 string;
  state:                  JobState;
  /** The step currently running, or the one that ended the job. */
  step:                   JobStep | null;
  /** The planned steps; ["replay"] when the dataset is reused or replay-db. */
  steps:                  JobStep[];
  /** The resolved (post-alias) dataset id. */
  dataset:                string;
  dataset_reused:         boolean;
  label:                  string;
  notes:                  string;
  date_from:              string;
  date_to:                string;
  underlyings:            string[];
  seed:                   number;
  oos_split:              number;
  overrides:              Record<string, unknown>;
  requested_by:           string;
  created_at:             string;
  started_at:             string | null;
  finished_at:            string | null;
  duration_s:             number | null;
  exit_code:              number | null;
  run_id:                 string | null;
  run_id_source:          "stdout" | "store_scan" | null;
  stdout_tail:            string[];
  stderr_tail:            string[];
  error:                  string | null;
  timed_out:              boolean;
  warnings:               "overlapping_prior_runs"[];
  prior_overlapping_runs: number;
}

export type LaunchResponse = Pick<JobRecord,
  "job_id" | "state" | "dataset" | "dataset_reused" | "label" | "created_at" |
  "steps" | "warnings" | "prior_overlapping_runs">;

export interface JobsResponse {
  items:               JobRecord[];
  total:               number;
  running_job_id:      string | null;
  max_concurrent_jobs: number;
}

/** 409 body detail of POST /jobs. */
export interface JobRunningDetail {
  code:   "JOB_RUNNING";
  job_id: string;
}
