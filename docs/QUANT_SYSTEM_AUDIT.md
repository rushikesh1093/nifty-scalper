# Quant system audit — NIFTY options scalper

Branch `feat/pivot-backtest-regime-readiness`, audited 30 Oct 2026. Every statement
below comes from the code on this branch (file references are relative to
`try12/backend/`) or from the research run described in
[STRATEGY_PERFORMANCE_REPORT.md](STRATEGY_PERFORMANCE_REPORT.md). Nothing here
was run against the broker, live Redis or the live Postgres.

## 1. Architecture map

```
AngelOne websocket ─► market_data/feed.py ─► tick_writer ─► Redis (ticks, 1-min candles, chain)
                                                     │
            strategy_runner.run_cycle (every candle close, per underlying)
              ├─ today's candles + last 500 session candles from Redis
              ├─ paper_engine.run_candle_exits  (EMA trail / VWAP / Supertrend flip)
              ├─ regime_detector.detect_regime ─► regime_stabilizer ─► regime_persistence (Postgres)
              ├─ strategy_orchestrator: regime_rules.REGIME_RULES → enabled strategies, directions
              └─ strategies.*.evaluate(candles, indicators) → SignalEvent + ExitPlan (spot levels)
                       │  SignalBus (wiring.py)
                       ▼
            paper_engine.handle_signal
              ├─ risk_gate phase 1: paper mode, kill switch, 11:30-12:15 blackout, 15:05 cutoff,
              │   daily loss (realised+MTM), trade caps, streak halt, strategy groups, Kelly
              ├─ option_selector: expiry (DTE bands 2-7/8-14, never 0-1 DTE), hard filters
              │   (quote, staleness, spread, OI, volume, premium band, R:R), 8-factor score
              ├─ risk_gate phase 2 sizing: fixed fraction / Kelly, max lots, risk-per-trade cap,
              │   capital, exposure cap
              └─ fills.entry_fill (ask + slippage)  → PaperPosition (Postgres)
            paper_engine.monitor_once (every tick pass)
              └─ EOD, premium SL, exit plan (spot SL / target / T1 / BE / time / session), ladder
                 → fills.exit_fill → net P&L (brokerage + statutory charges)
Replay: services/replay/engine.py drives the SAME functions under a replay context with
recorded ticks / chain snapshots (no interpolation; pessimistic open→low→high→close).
Research (new): app/research drives the SAME strategy classes over recorded spot history.
```

## 2. What is implemented, partial, disabled

| Area | State | Evidence |
|---|---|---|
| Five enabled strategies (FCB, ORB, SUPERTREND_EMA, EMA_RSI_PULLBACK_15M, PIVOT_BOUNCE) | implemented, paper | `app/strategies/strategies.py`; flags in `research/live_params.json` |
| VWAP_EMA_PULLBACK, MOMENTUM_BURST | implemented, disabled by flag | same |
| BANKNIFTY | feed, tokens, selector bands exist; **no strategy trades it** | `build_strategies` returns `[]` for anything but NIFTY (thresholds are NIFTY points) |
| Expiry-day iron fly | separate paper book | `services/trading/iron_fly.py` |
| Exit plans (spot stop/target/T1/BE/time/session + bar exits) | implemented | `services/trading/exit_plan.py`, `paper_engine._check_plan` |
| T1 "book half" | **a no-op at one lot** (stop to entry only) | `paper_engine._check_plan` → `_skip_partial` when lots < 2 |
| Regime → strategy map | implemented, **name-based, not validated** | `services/trading/regime_rules.py` (`_TREND`, `_BREAKOUT`, `_RANGE` sets) |
| Option delta | **estimated** (Black-Scholes from DTE and an IV input), not an observed Greek | `option_selector._estimate_delta` |
| Replay engine | implemented, faithful; data exists only for the recorded live window | `services/replay/engine.py` |
| Historical option prices | **11 recorded sessions** (14-30 Oct 2026), no OI/volume/IV in the store | `research/live_options.db` |
| Strategy-level evidence gate | **missing** before this work; added as research output | `app/research/walkforward.py` |

## 3. Live vs replay vs research consistency

* **Replay ≡ live** for decisions (same functions, frozen params, run isolation). It can
  only run where option ticks were recorded, so it cannot answer "is there an edge over
  three years".
* **`research/run_year.db` (24 Oct) used modelled option prices** (Black-Scholes on VIX)
  inside the engine. Its rupee P&L is a model result, not market evidence (finding D-1).
* **The new research harness** runs the live strategy classes and the live
  `exit_plan.tick_action` on recorded spot candles; differences from live are listed in
  `app/research/sim.py`'s docstring (portfolio caps, regime enablement and option
  selection are judged separately, not simulated).

## 4. Findings

Severity: **Critical** (invalidates results or loses money by design), **High**, **Medium**, **Low**, **Info**.

| ID | Severity | Finding | Files | Evidence | Action |
|---|---|---|---|---|---|
| S-1 | Critical | The five entry rules have no measurable directional edge on the underlying before costs; option costs (~2.6 spot pts + decay) turn that into a steady loss. | `strategies/strategies.py` | Experiment A: 30-min directional hit 0.47-0.52, pre-cost expectancy CIs all include 0 (STRATEGY_PERFORMANCE_REPORT) | Keep all five paper-only; DISABLED/INSUFFICIENT statuses below. Not fixable by exits (exit-variant table). |
| F-1 | High | A spot-plan **TARGET filled at LTP × 0.999**, not the bid: `_check_plan` passed reason `TARGET` to `fills.exit_fill`, whose TARGET row is meant for the premium **limit** target. With LTP above the bid this overstated every spot-target exit. | `paper_engine.py`, `fills.py`, `exit_plan.py` | regression test `test_spot_target_fills_at_the_bid_not_the_last_trade` fails on the old code (`TRIGGER_SLIPPED` ≠ `BID`) | **Fixed**: spot TARGET fills as a market exit (`SPOT_TARGET_FILL`); premium target unchanged. |
| F-2 | Medium | The per-trade **risk cap ignored costs**: lots = budget ÷ (stop distance × lot); a stopped lot also pays ~₹60 brokerage + charges, so a cap-fitting trade could exceed `max_risk_per_trade_pct`. | `risk_gate.sizing_decision` | `test_risk_cap_counts_the_round_trip_costs_of_a_stopped_lot` (2 lots before, 1 after) | **Fixed**: adds `fills.expected_round_trip_cost` per lot. Can only lower lots. |
| D-1 | High | Synthetic option premiums were used as evidence in earlier year-long engine runs. | `research/build_year_replay_db.py`, `run_year.db` | FINDINGS.md §1 "modelled options" | New reports separate spot (A), recorded quotes (B) and cost models; no BS premium is called real. |
| D-2 | High | **Real option history is 11 sessions.** No historical OI, volume, IV or delta. Experiment B on real quotes is therefore descriptive only. | data | `option_execution.json` | Recorded store must keep accumulating; promotion gates require forward paper evidence. |
| D-3 | Medium | Recorded option snapshots include **out-of-hours minutes** (stale quotes up to 23:24). | `live_options.db` | minute ranges per day in OPTION_EXECUTION_AUDIT | Research loader drops minutes outside 09:15-15:30. |
| D-4 | Medium | Futures volume history is **stitched from two tokens**; token 61471 is labelled `NIFTY23NOV26FUT` but spans Oct 2023-Oct 2026 with a gap Sep 2025-Sep 2026 where it is a far month (volume ~5/min). | `nifty_history.db` | basis/volume check in this audit | Loader takes the highest-volume futures series per day; relative volume is a within-day ratio, so level shifts do not matter. ORB volume rule results flagged as lower confidence. |
| R-1 | Medium | Regime → strategy map is assigned by **strategy names** ("trend" strategies in trend regimes), never validated on outcomes. | `regime_rules.py` | REGIME_VALIDATION_REPORT | Research classifier + per-regime expectancy added; live map unchanged (no validated replacement exists). |
| R-2 | Low | ORB volume rule **fails open** when futures volume is missing (logs once a day, then trades). | `strategies.ORBBreakoutStrategy._volume_ok` | code | Left as is (live behaviour); research variant `ORB_VOLUME` requires the data. |
| E-1 | Medium | SUPERTREND_EMA pairs a Supertrend-line stop (median 22 spot pts) with an **8-premium-point target (~16 spot pts ≈ 0.73R)** and a 10-min time stop (5-min average hold): before costs it needs a > 58 % hit rate, and ~0.13R of cost per trade on top. | `strategies.SupertrendEMAStrategy` | 2,450 post-design trades, -0.147R net (CI -0.176..-0.118) | Exit variants tested; none rescues it. Recommend disabling. |
| E-2 | Info | With ONE lot the plans' T1 partial books nothing; only the stop moves to entry, which turns the 1R/2R plan into "breakeven after 1R". | `paper_engine._check_plan` | code | Research simulates the one-lot reality; partial-booking exit variant reported as hypothetical (needs ≥ 2 lots). |
| O-1 | Medium | Delta used for selection and the premium-stop mapping is a **Black-Scholes estimate**, not an observed Greek; the recorded quotes give an effective delta of ~0.53 (ATM) / ~0.47 (1 OTM). | `option_selector._estimate_delta`, `fills.plan_sl_pct` | OPTION_EXECUTION_AUDIT §2 | Documented; the estimate is close to observed for ATM, so no change. |
| X-1 | Info | Index VWAP is a session mean of typical price (spot volume is 0). | `strategies/indicators.vwap` | docstring | Research VWAP features weight by futures volume where available. |

## 5. Components verified correct — keep unchanged

* `fills.py` decision table (ask-in / bid-out, gap-through on SL, tick rounding, charges).
* Replay intrabar order `PESSIMISTIC_LOW_FIRST`, no price interpolation, paper-only guards,
  run isolation (`run_id` on every row), startup assertions.
* `exit_plan.tighten_stop` / `move_stop_to_entry` only ever tighten (tested here with
  random paths, `test_trailing_never_loosens_and_never_beats_the_path`).
* Option-selector hard filters: missing quote / OI / volume is a rejection, never a skipped
  filter; never buys 0-1 DTE.
* Risk gate: blackout, entry cutoff, daily loss incl. MTM, streak halt,
  `exclusive_strategy_groups = (ORB_BREAKOUT, FIRST_CANDLE_BREAKOUT)` (already prevents the
  two opening-break strategies from taking the same move twice), `max_open_per_direction = 1`.
* Kelly: refuses a strategy whose decided-R statistics show no edge (`KELLY_NO_EDGE`) and is
  capped at the fixed fraction — it never scales up to recover losses.

## 6. Recommended changes (beyond the two fixes made)

1. Keep the five buying strategies **paper-only**; none meets the promotion gates
   (WALK_FORWARD_REPORT). Do not raise size or leverage on any of them.
2. Keep recording option quotes (with OI/volume when the feed provides them) so
   Experiment B can grow from 11 sessions to a usable sample; re-run
   `python -m app.research.cli` monthly.
3. Replace the name-based regime map only when a per-regime expectancy is positive in the
   design window AND in later months (REGIME_VALIDATION_REPORT gives the table). None
   qualifies today, so the map is left as is.
4. Any new entry rule goes through `app/research` first: pre-declared variant, DESIGN-window
   thresholds, walk-forward status `ELIGIBLE_FOR_PAPER` before it is coded into live.
