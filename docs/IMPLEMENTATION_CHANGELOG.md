# Implementation changelog — quant research pass (30 Oct 2026)

Branch `feat/pivot-backtest-regime-readiness`. Nothing here changes a live
parameter, a strategy flag, `trading_config`, Postgres rows or Redis keys, and
nothing places an order. Live behaviour changed in exactly two places (F-1,
F-2), both making paper results more conservative.

## Live code (bug fixes, with regression tests)

| ID | Change | Files | Test |
|---|---|---|---|
| F-1 | A spot-plan TARGET (ORB, 09:15 breakout, EMA/RSI T2, pivot) now fills as a market exit at the bid. It filled at LTP × 0.999 through the premium-limit TARGET row of the fill table, above a valid bid. The premium-point target (Supertrend) keeps its limit fill. | `app/services/trading/paper_engine.py` (`_check_plan`, `_close_position(fill_reason=)`), `app/services/trading/exit_plan.py` (`SPOT_TARGET_FILL`) | `tests/test_exit_plan_engine.py::test_spot_target_fills_at_the_bid_not_the_last_trade` (fails on the old code), `::test_premium_point_target_still_fills_at_its_limit` |
| F-2 | The per-trade risk cap counts each lot's round-trip brokerage and charges, not only stop distance × lot size. It can only lower the lot count. | `app/services/trading/risk_gate.py` (`sizing_decision`) | `tests/test_quant_fixes.py::test_risk_cap_counts_the_round_trip_costs_of_a_stopped_lot` (2 lots before, 1 after) |

## New research package — `try12/backend/app/research/` (offline only)

| Module | Purpose |
|---|---|
| `data.py` | Read-only (`mode=ro`) loader for the candle store; sessions, futures volume, VIX, data-quality report, weekly-expiry calendar (Thu → Tue from 1 Sep 2025, holiday → previous session), historical lot-size schedule, dataset SHA-256 |
| `sim.py` | Experiment A: drives the **live strategy classes** minute by minute (the runner's 500-candle history), fills at the next open, steps positions on a pessimistic intrabar path through the live `exit_plan.tick_action` and the strategies' own `candle_exit`; same-candle stop+target = stop, flagged `ambiguous`; blackout, entry cutoff and 15:15 square-off as live. Excursions to 15:15 (MFE/MAE, time to 1R/2R/stop). Exit variants A-F on fixed entries |
| `variants.py` | 13 pre-declared entry variants of the five strategies (subclasses of the live classes, not registered in live) |
| `features.py` | Point-in-time features per signal (regime, ADX, ATR, VWAP distance, relative futures volume, bar shape, gap, 5-day move, VIX, expiry, pivot level / sweep depth) |
| `regime.py` | Causal regime classifier (volatility percentile vs prior 20 sessions × ADX/EMA-slope/persistence structure; warm-up, opening and transition explicit) |
| `options.py` | Experiment B on recorded quotes: execution-cost regression and trade replay (ask in, bid out, charges, lot by date; missing quote = UNAVAILABLE, never modelled) |
| `metrics.py` | PF, expectancy, Wilson CI, **session-cluster bootstrap** CI, drawdown, streaks, daily Sharpe/Sortino (methodology in docstring), breakdowns |
| `walkforward.py` | Cost models (ZERO / MEASURED / HARSH), 3-1-1 month walk-forward after a 12-month design window, selection on train, gate on validation, status rules (`INSUFFICIENT_EVIDENCE`, `DISABLED_FOR_RESEARCH`, `OVERFIT`, `ELIGIBLE_FOR_PAPER`) with configurable `PromotionCriteria` |
| `cli.py` | Full protocol; writes CSV/JSON + `manifest.json` (commit, dirty flag, dataset SHA-256s, date range, config hash, execution/cost model versions, bootstrap seed); `--analyse-only` re-analyses saved trades |
| `report.py`, `regime_validation.py` | Generated markdown tables; regime-classifier validation |

Tests: `tests/test_research.py` (21): intrabar ambiguity, gap fills, 15:15 square-off,
trailing monotonicity on random paths (all six exit variants, both sides), live-plan
ambiguity, harness fills at next open with the live 50-pt cap, **no future candle
changes an earlier signal**, determinism, regime warm-up/purity, statistics, cost model,
**walk-forward never scores design months and never uses the test month**, status rules,
lot-size schedule, expiry holiday shift, option replay (ask/bid, out-of-hours rows
dropped, missing quote → UNAVAILABLE).

## Corrections made during the work

* MEASURED cost model first charged expiry-day trades the 0-DTE decay (4.5 pts/h). The live
  selector never buys 0-1 DTE, so it was changed to the ordinary 1.5 pts/h before any
  report figure was written; the HARSH model still charges 4 pts/h everywhere.
* Regime validation standard errors are computed from per-session means (overlapping
  30-minute windows make bar-level errors too small).

## Not changed (deliberately)

* Strategy flags, thresholds, regime map, sizing parameters, capital, Kelly settings.
* No variant is registered in `build_strategies`; none passed the gates.
* Frontend: no change (the IS / validation / OOS / live-paper split is delivered as
  separate tables in the reports, not as a dashboard view; see the walk-forward report's
  limitations).

## Verification

* Backend: `python -m pytest -q` → **1,177 passed, 1 skipped** (8 min 15 s), including the
  21 new research tests and the 3 new regression tests.
* Frontend: not built. No frontend file changed, and `node_modules` is not installed in
  this checkout (the package has no test script).
* Research run: `docs/research_results/manifest.json` (simulation at `55521bf`, analysis at
  `04b03ef`, dataset SHA-256 `100c0580…cbee7`, config hash `a13f3a5d…`).

## New candidate strategies (follow-up)

* Pre-registered four candidates before testing (`docs/NEW_STRATEGY_PREREGISTRATION.md`):
  VOL_SQUEEZE_BREAKOUT, TREND_DAY_CONTINUATION, GAP_FADE, VWAP_EXTREME_REVERSION.
* Implemented as live-shaped classes in `app/research/new_strategies.py`, with
  `python -m app.research.cli --suite new`; tests in `tests/test_new_candidates.py`
  (one trade a day, valid plans, no look-ahead, monotone trail, gap-fade scenario).
* Result: **none passes**, so none is registered in `build_strategies`
  (`docs/NEW_STRATEGY_RESULTS.md`).
