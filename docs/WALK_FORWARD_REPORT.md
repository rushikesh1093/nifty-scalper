# Walk-forward report

Code: `try12/backend/app/research/walkforward.py`. Results:
`research_results/walk_forward.json`, which holds every fold's train / validation /
test window, the candidate chosen, its train and validation expectancy, and the test
result. The full tables are in
[STRATEGY_PERFORMANCE_REPORT.md](STRATEGY_PERFORMANCE_REPORT.md) ("Walk-forward
out-of-sample …").

## 1. Protocol (fixed before the run, unchanged after)

* **Design window** 23 Oct 2023 – 30 Sep 2024: variant thresholds (medians of the
  baseline signals' trigger-bar features) come from here, and no design month is ever
  scored. Thresholds: FCB body ≥ 0.744 and close in the top 9.3 % of the bar; ORB body ≥
  0.398 × OR width; ORB futures relative volume ≥ 1.266; pivot sweep ≥ 2.85 pts.
* **Folds:** train 3 calendar months → validation 1 month → test 1 month, rolled by one
  month. That gives 25 test months, Oct 2024 – Oct 2026, ~490 sessions. No shuffling.
* **Candidates per strategy:** the live rule plus its pre-declared entry variants
  (FCB 3, ORB 5, ST 4, EMA_RSI 3, PIVOT 3), each with and without a regime filter that
  is *learned on the train months* (regimes with ≥ 10 trades and positive net
  expectancy). Exits are the live plans: entry and exit are not optimised together.
* **Selection:** the highest train net expectancy with ≥ 20 train trades and > 0. That
  candidate trades the test month only if its validation month is also > 0. Otherwise
  the strategy stands aside (`NO_TRADE`). The test month never influences a choice
  (`tests/test_research.py::test_walk_forward_never_scores_design_months_or_uses_the_test_month`).
* **Status gates** (`PromotionCriteria`, configurable): ≥ 100 OOS trades (200 preferred),
  ≥ 60 sessions, PF ≥ 1.20, bootstrap CI lower bound > 0, positive without the best
  session, max drawdown ≥ -25R, ≥ 50 % of traded test months positive. Statuses:
  `INSUFFICIENT_EVIDENCE` (too few trades, or positive but failing a gate),
  `DISABLED_FOR_RESEARCH` (negative with enough evidence), `OVERFIT` (positive in the
  design window, negative out of sample), `ELIGIBLE_FOR_PAPER`.

## 2. Results (MEASURED costs unless stated)

| Strategy | Months traded / 25 | OOS trades | OOS net exp R (CI95) | Months positive | Worst / best month R | Status |
|---|---:|---:|---|---:|---|---|
| FIRST_CANDLE_BREAKOUT | 4 | 47 | -0.304 (-0.666..+0.115) | 0 | -6.36 / -0.72 | INSUFFICIENT_EVIDENCE |
| ORB_BREAKOUT | 0 | 0 | – | – | – | INSUFFICIENT_EVIDENCE |
| SUPERTREND_EMA | 3 | 53 | -0.107 (-0.431..+0.223) | 0 | -4.49 / -0.52 | INSUFFICIENT_EVIDENCE |
| EMA_RSI_PULLBACK_15M | 4 | 47 | -0.282 (-0.568..+0.043) | 1 | -8.84 / +0.35 | INSUFFICIENT_EVIDENCE |
| PIVOT_BOUNCE | 3 | 34 | -0.126 (-0.748..+0.596) | 2 | -11.08 / +4.47 | INSUFFICIENT_EVIDENCE |

The same protocol at **zero cost** (signal quality only): SUPERTREND_EMA 704 OOS trades,
-0.036R → `DISABLED_FOR_RESEARCH`; EMA_RSI_PULLBACK_15M 120 trades, -0.187R →
`DISABLED_FOR_RESEARCH`; FCB 72 trades -0.213R; PIVOT 138 trades +0.086R (CI
-0.231..+0.433); ORB 12 trades. Even before any option cost, the walk-forward
selection finds nothing that holds up in the next month.

**Combined out-of-sample, all five, MEASURED:** 181 trades, every strategy negative. The
selection process stood aside in 21-25 of 25 months per strategy, because in most
3-month train windows no candidate was positive after costs. **That is the right
behaviour: the orchestrator should not trade when nothing qualifies.**

## 3. What the walk-forward does not show, and the fixed-rule view

Standing aside most months leaves the OOS samples small. The complementary view is
each rule run **unchanged** through all 24.7 post-design months (no selection, so no
selection bias, but also no out-of-sample *choice*). It is in the performance report's
cost table. Highlights at MEASURED cost:

| rule | n | net exp R | design-year exp R (same costs) |
|---|---:|---:|---:|
| ORB_VOLUME | 79 | +0.101 | -0.173 |
| ORB_DISPLACEMENT | 63 | +0.098 | -0.045 |
| PIVOT_BOUNCE (live) | 336 | +0.025 | -0.123 |
| ORB_BREAKOUT (live) | 155 | +0.016 | -0.269 |
| ORB_VWAP (identical trades to the live ORB) | 155 | +0.016 | -0.269 |
| everything else | | < 0 | |

Every rule that is positive after the design year was **negative in it**. The sign
flips between periods. That is what a zero-edge rule looks like across two samples,
and it is why none of these is promoted. The ORB displacement and volume filters are
recorded as pre-registered hypotheses for forward paper data. They are not
improvements.

**ORB and the 20-trade rule.** ORB trades ~6 times a month, and its filtered variants
~3, so a 3-month train window rarely has 20 trades and ORB is never selected. The rule
was fixed before the run and is not relaxed after seeing the result. Relaxing it is a
legitimate choice for a new, pre-declared run.

## 4. Sensitivity and overfitting controls

* Parameters are not grid-searched. Each variant has one threshold, set from the design
  window's median, so there is no "best parameter" to be fragile.
* Three cost models bracket execution. Conclusions are the same under ZERO (no
  strategy is reliably positive) and HARSH (all are worse).
* Fold-to-fold dispersion is large relative to the means: monthly R standard deviation
  at zero cost is 9.5R for SUPERTREND_EMA (16 traded months) against a total of -25R.
  Single months decide results, which is another reason not to trust any positive
  subset.
* 18 entry candidates × 2 filters × 3 cost models were evaluated. Any one positive cell
  should be read against that multiplicity.

## 5. Paper-trading promotion

**No strategy meets the promotion criteria.** None is `ELIGIBLE_FOR_PAPER`. The five
stay in paper (they are already paper-only); the recommendation is to switch off
SUPERTREND_EMA, EMA_RSI_PULLBACK_15M and FIRST_CANDLE_BREAKOUT and to keep ORB_BREAKOUT
and PIVOT_BOUNCE running unmodified, to accumulate forward evidence. The flags were
not changed here.

## 6. Limitations

* Spot-based R with a cost model, not option prices: real option history exists for 11
  sessions only (OPTION_EXECUTION_AUDIT).
* Strategies are evaluated one at a time. Portfolio rules (3 trades a day, 2 open
  positions, daily loss halt, regime enablement, the ORB/FCB exclusive group) are not
  applied, so the live portfolio sees fewer, differently selected trades.
* The dashboard does not yet show in-sample / validation / OOS / live-paper results as
  separate series. These reports keep them apart: design vs post-design columns, and
  walk-forward OOS. A dashboard view reading `walk_forward.json` is a follow-up.
* NIFTY only. BANKNIFTY has neither strategies nor history in the research store.
