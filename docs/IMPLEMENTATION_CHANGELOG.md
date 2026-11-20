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

## Exit changes to two live strategies (follow-up, at the user's request)

Only changes that improved the live rule **in each of the three years** after costs
(MEASURED) were made. Selection was among six exit engines already reported, on data
already seen, so these are "less bad" changes, not evidence of profit.

| Strategy | Change | Net R per year, old exit (Y1 / Y2 / Y3) | Net R per year, new live code via harness |
|---|---|---|---|
| FIRST_CANDLE_BREAKOUT | 1.5 × range target and 14:45 exit → ATR chandelier: stop trails the best price since entry minus 2 × 5-min ATR(14) at the signal, on every 1-min close; held to 15:15 | -0.147 / +0.003 / -0.196 | +0.020 / +0.040 / -0.070 (post-design -0.011, CI -0.150..+0.143) |
| EMA_RSI_PULLBACK_15M | T1 1R + T2 2R + 14:45 exit → stop trails the 5-min Supertrend(10, 3) line; a 5-min close with the Supertrend against the trade exits; held to 15:15 | -0.311 / -0.045 / -0.083 | -0.092 / +0.120 / +0.042 (post-design +0.083, CI -0.062..+0.246, PF 1.26) |

* Files: `app/strategies/strategies.py` (`TRAIL_EXIT` class flag on both; `False` restores the
  old exits), `app/services/trading/exit_plan.py` (optional `trail_atr`, `trail_extreme`;
  `chandelier_update`). Stored plans without the new fields load unchanged. EMA_RSI plans
  made under the old exit keep their T1/T2 exits.
* Tests: `tests/test_new_strategies.py` (new plans, the old plans with `TRAIL_EXIT = False`,
  chandelier monotonic for both sides), `tests/test_stop_sizing_groups.py`.
* Parity check: `docs/research_results/parity_live_exits.json`. The FCB figures match the
  research exit variant exactly. EMA_RSI trades 9-16 more times a year, because trades end
  at different times and the one-position rule frees up later entries.
* Not changed: ORB_BREAKOUT, PIVOT_BOUNCE (no exit beat the live one in every year),
  SUPERTREND_EMA (every exit stays negative every year; recommended off).

## Efficiency test and forward-evidence check (follow-up)

**Cost-to-risk entry filter: tested, not adopted.** Costs are ~2.6 spot points a trade,
so tight-stop trades pay the most in R. Skipping trades whose fixed cost exceeds
0.06-0.13R of the plan risk was tested on the baseline trades by year (MEASURED costs).
It improves no strategy in every year except SUPERTREND_EMA, which stays negative
(best: -0.03 / -0.09 / -0.02R on ~130 trades a year). It hurts PIVOT_BOUNCE (wider stops do
worse there) and is mixed for ORB. There is no general efficiency rule here that the data
supports, so nothing was changed.

**Forward evidence on paper trades: added.** `GET /api/v1/analytics/strategy-evidence`
(`app/services/metrics/strategy_evidence.py`) applies the research promotion gates
(`PromotionCriteria`: ≥ 100 trades on ≥ 60 sessions, net-R expectancy > 0 with the
session-bootstrap CI above 0, PF ≥ 1.2, positive without the best session, drawdown ≥ -25R,
≥ half of months positive) to the run's closed paper trades. Trades entered before the
strategy's last rule change (`RULE_SINCE`: FCB and EMA_RSI from 31 Oct 2026) do not count,
and trades without a recorded R are excluded and counted. Statuses:
`INSUFFICIENT_EVIDENCE`, `DISABLED_FOR_RESEARCH`, `PASSES_GATES`. It advises; it never
switches a flag. The trade log rows (`/strategy-report`) now include `r_gross` / `r_net`.
Tests: `tests/test_strategy_evidence.py`.

## Regime off for trading, regime v2 in shadow, option archive (31 Oct 2026)

At the user's request ("if regime is not working keep it off rather than in trade, and
redesign"). Details: [REGIME_REDESIGN.md](REGIME_REDESIGN.md), [DATA_FIXES.md](DATA_FIXES.md).

* `TradingParams.regime_gating = False` (new, default): the regime is detected and
  persisted but does not enable or disable strategies, restrict direction, size, or create
  no-trade candles. A detector failure and the `trade_on_expiry_day = False` policy are
  still enforced. `True` restores the old map.
* `regime_atr_pct_lookback` 20 → 486: the 20-value rank had no relation to the next 30
  minutes' range (Spearman ≈ 0 every year). The 486-value rank predicts it every year.
* `services/trading/regime_v2.py` + `GET /api/v1/regime/v2`: the volatility bucket and
  opening-range side. Gates nothing.
* `app/research/live_regime_eval.py`: replays the live classifier and map over history
  (allowed vs blocked by year, label predictiveness, v2 by strategy).
* `app/research/option_archive.py`: permanent per-minute NIFTY/BANKNIFTY option archive
  (bid/ask/OI/volume) from Postgres (read-only) and tick backups. First build: 19 sessions.
  `options.Quotes` reads it.
* Tests: `tests/test_regime_shadow.py`, archive test in `tests/test_research.py`; the
  tests that specify the old map run under the new `v1_regime` fixture with every
  assertion unchanged (one default assertion updated, 20 → 486).

## Evidence gates (31 Oct 2026)

Details: [EVIDENCE_GATES.md](EVIDENCE_GATES.md). In the risk gate, after the kill switch:
`evidence_blocked_strategies` (default SUPERTREND_EMA), `evidence_auto_block` (refuse a
strategy whose own paper trades are DISABLED_FOR_RESEARCH; computed once a day),
`low_vol_skip_strategies` (default EMA_RSI_PULLBACK_15M while regime v2 reads LOW volatility,
the only strategy improved in every year). New reason codes `EVIDENCE_BLOCKED`, `LOW_VOL_SKIP`.
The runner records regime v2 each candle for the gate. EMA_RSI's evidence clock restarts on
2 Nov 2026. Tests: `tests/test_evidence_gates.py`. Scripted replays and the tests of other
gates that use SUPERTREND_EMA run with the evidence gates off (`no_evidence_gates`), with
assertions unchanged. Full suite: 1,205 passed, 1 skipped.

## Puts only and a daily option-buy cap (31 Oct 2026)

`pe_only_strategies` (default ORB_BREAKOUT, PIVOT_BOUNCE, FIRST_CANDLE_BREAKOUT; call signals
refused as `DIRECTION_POLICY`) and `max_option_buys_per_day` (default 2; the lower of it and
`max_trades_per_day` applies, at both cap checks). Evidence: bought calls lost in 14 of 15
strategy-years; a forward hypothesis, since it was found in the same data. Evidence clocks
(`RULE_SINCE`) for FCB, ORB and PIVOT restart on 2 Nov 2026. Tests in
`tests/test_evidence_gates.py`; scripted replays and the `no_evidence_gates` fixture switch
these off.

## Frontend check and fixes (1 Nov 2026)

Details: [FRONTEND_AUDIT.md](FRONTEND_AUDIT.md). Typecheck, lint and build were clean; every page
handles a missing backend. Fixed: the diagnostics reported regime blocks the shadow-mode engine
no longer applies (now `strategy_orchestrator.regime_permits_trading` plus `regime_gating` in the
probe; the UI labels shadow mode); new Strategy evidence panel (policy + per-strategy status);
regime v2 on the regime card; Next 14.2.3 → 14.2.35 and `npm audit fix`, with
`images.unoptimized`, `next dev -H 127.0.0.1` and Docker bound to 127.0.0.1:3000 against the
advisories only Next 15.5+ fixes. Backend suite: 1,208 passed, 1 skipped.

Next 14.2.35 → 16.4.3 with React 19.3.1 (15.5.28 still failed `npm audit` via its bundled
postcss); async `params` on the run page, ESLint 9 flat config replacing `next lint`, nav bar
scrolls in its own strip at 375 px. `npm audit --omit=dev`: 0 vulnerabilities. Details in
FRONTEND_AUDIT.md, Security.

## Data and evidence pipeline (1 Nov 2026)

Details: [DATA_AND_EVIDENCE_PIPELINE.md](DATA_AND_EVIDENCE_PIPELINE.md).
* `tick_retention.archive_options`: the option archive runs in a child process before every
  retention pass (`OPTION_ARCHIVE_PATH`, default `backups/options_archive.db`).
* Shadow outcomes: migration **0017** adds `strategy_signals.exit_plan`, `shadow_r_net`,
  `shadow_exit` and `shadow_hold_min`. `shadow_outcomes.evaluate_day` runs at 15:35 IST from the
  runner's EOD loop. `/analytics/strategy-evidence` returns `shadow`, and the Strategy evidence
  panel shows it.
* Iron fly: `IronFlyPaper(strategy, wing)`; a 300-point-wing book `EXPIRY_IRON_FLY_W300` runs
  beside the 200 one; `/trading/iron-fly?wing=300`.
* Startup check 8: the database must be at `SCHEMA_HEAD` (0017); otherwise startup stops with
  `python -m alembic upgrade head`.
* Tests: `tests/test_data_and_shadow.py`.

## Master-brief pass (1 Nov 2026)

Documents: SYSTEM_AUDIT, DATA_QUALITY_REPORT, STRATEGY_AUDIT, RISK_MANAGEMENT_AUDIT,
IMPLEMENTATION_ROADMAP. Live data-health engine (`market_data/data_health.py`,
`GET /api/v1/market/data-health`, Market page panel). Round 2 research: Bollinger squeeze
(untestable as registered; post-hoc -0.24R) and CPR + VWAP (puts +0.16R, 7 of 9 gates, not
added). Research metrics gain Calmar and a session-order Monte Carlo. The regime is not used
for trading (user instruction). `pytest-timeout` (300 s per test) in `requirements-dev.txt` /
`pytest.ini`. Suite: 1,231 passed, 1 skipped.
