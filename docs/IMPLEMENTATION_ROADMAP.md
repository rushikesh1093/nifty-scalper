# Implementation roadmap (1 Nov 2026)

The master brief's 16 sections against the system as it is now. ✅ done, ◐ partial,
⛔ rejected by evidence, ⏳ waits on forward data.

| § | Brief section | Status | Where |
|---|---|---|---|
| 1 | Codebase audit + 5 docs | ✅ | SYSTEM_AUDIT, DATA_QUALITY_REPORT, STRATEGY_AUDIT, RISK_MANAGEMENT_AUDIT, this file |
| 2 | Market data engine + validation | ◐ | Archive, data-health engine, readiness. No breadth (no source) |
| 3 | Feature engineering | ◐ | Strategy-local, causal (tested no-look-ahead). No central feature store: no feature showed out-of-sample value to justify one |
| 4 | Regime engine + calibrated confidence | ⛔ | Tested; does not predict. **Not used for trading** (user instruction); v2 shadow only |
| 5 | Strategy engine, selector, scoring | ◐ / ⛔ | All 10 strategies evaluated (STRATEGY_AUDIT). Regime selector and score rejected. Evidence gates instead |
| 6 | Options selector | ✅ | Existing selector already meets the brief; delta estimated, not observed |
| 7 | Entry / exit / trailing | ✅ | Chandelier and Supertrend trails, never-widen, gap handling, premium SL |
| 8 | Risk | ✅ | RISK_MANAGEMENT_AUDIT |
| 9 | Backtesting + validation | ✅ | Harness, walk-forward, pre-registration, Monte Carlo, Calmar, CIs |
| 10 | Trade analytics + learning | ✅ | Trade log, strategy evidence, shadow outcomes |
| 11 | DB / Redis | ◐ | Migrations 0017, schema startup check, idempotent archive. Redis unchanged |
| 12 | Dashboard | ◐ | Evidence, data health, regime v2. No backtest lab UI changes |
| 13 | Reliability / security | ◐ | Next 16, localhost binding, 0 audit findings. Live-order reconciliation: paper only |
| 14 | Rules | ✅ | All followed: no fabricated data, no live orders, configurable switches |
| 15-16 | Deliverables / acceptance | ◐ | Out-of-sample reports done. **No strategy proven profitable** |

## Next steps, in order

1. **Collect forward evidence (2-3 months, no code).** Strategy evidence panel: puts-only ORB /
   PIVOT / FCB, refused-signal shadows, both iron flies.
2. **Forward-test CPR_VWAP puts (decision for you).** It passes 7 of 9 gates. Adding it to the
   live engine as a paper strategy needs its class moved into `app/strategies/strategies.py`, a
   flag and a `RULE_SINCE` date.
3. **Option archive at ~120 sessions (about 6 months):** rerun Experiment B on real quotes.
4. **Real money: only** when a strategy shows `PASSES_GATES` on forward paper trades, or the
   iron fly meets its go/no-go rules. Before that: broker order placement, reconciliation and
   duplicate-order protection, as a separate reviewed project, and a capital decision
   (RISK_MANAGEMENT_AUDIT).

Not planned, because the evidence argues against them: more indicators, a central feature
store, a regime-based selector, signal scores, HMM regimes.
