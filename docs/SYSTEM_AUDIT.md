# System audit (1 Nov 2026)

Complete-system view. Detailed findings live in the linked reports, and nothing here is
restated from memory: each line points at code or a measured result.

## Lifecycle (data → decision → paper fill)

```
AngelOne websocket ─► feed.py ─► market_manager (ticks → Redis, 1-min candles, chain, tick_writer → Postgres ticks)
                                   │  data_health (new): duplicates, invalid OHLC, outliers, stale / crossed quotes, latency
strategy_runner.run_cycle (each candle close)
  ├─ readiness (session_readiness) · ATR / VIX gates · expiry check
  ├─ regime_detector (v1, SHADOW: regime_gating=False) · regime_v2 (shadow; recorded only)
  ├─ paper_engine.run_candle_exits (trails: ATR chandelier, Supertrend; VWAP / EMA exits)
  └─ strategies.evaluate → SignalEvent + ExitPlan
paper_engine.handle_signal
  ├─ risk_gate: paper mode · kill switch · EVIDENCE GATES (research block, paper auto-block,
  │   puts-only, low-vol skip) · blackout · cutoff · daily loss (incl. MTM) · daily cap
  │   (min(max_trades_per_day, max_option_buys_per_day)) · streak halt · groups · per-direction
  ├─ option_selector (DTE bands, hard filters, score) → sizing (risk cap incl. costs) → fills
  └─ every refusal → no_trade_decisions + SKIPPED strategy_signal (+ exit_plan, shadow outcome at 15:35)
monitor_once: EOD · premium SL · plan exits → fills.exit_fill (bid / ask) → net P&L, R
iron_fly (separate book; 200 and 300 wings) · tick_retention (archive → backup → delete)
```

## Component status

| Area | State | Evidence / doc |
|---|---|---|
| Market data | Live feed + Redis + Postgres ticks; 30-day retention with archive-before-delete | DATA_QUALITY_REPORT.md |
| Data validation | Readiness (missing minutes) + **new** data-health engine and endpoint | DATA_QUALITY_REPORT.md |
| Features | Strategy-local indicators (EMA, RSI, ATR, Supertrend, VWAP, ADX), levels incl. CPR / PDH / PDL / pivots, chain IV / delta from ticks | STRATEGY_AUDIT.md |
| Regime | v1 detected, **not used for trading**; v2 shadow | REGIME_REDESIGN.md |
| Strategies | 5 enabled; SUPERTREND blocked by evidence; ORB / PIVOT / FCB puts only | STRATEGY_AUDIT.md, EVIDENCE_GATES.md |
| Option selection | Direction-correct, DTE 2-7 / 8-14, quote / spread / OI / volume filters, scored | OPTION_EXECUTION_AUDIT.md |
| Execution (paper) | Ask in / bid out, statutory charges, gap-through stops, stale handling | QUANT_SYSTEM_AUDIT.md (F-1 fixed) |
| Risk | Independent of strategies; every refusal logged with a reason code | RISK_MANAGEMENT_AUDIT.md |
| Research | Two-stage harness on the live classes, walk-forward, pre-registration, Monte Carlo, Calmar | STRATEGY_PERFORMANCE_REPORT.md, WALK_FORWARD_REPORT.md |
| Evidence loop | Forward paper gates, shadow outcomes of refusals | DATA_AND_EVIDENCE_PIPELINE.md |
| Frontend | Next 16 / React 19, 0 audit findings, honest regime status, evidence and data-health panels | FRONTEND_AUDIT.md |
| Tests | 1,221+ backend tests | IMPLEMENTATION_CHANGELOG.md |

## Order safety (live mode)

The system is paper-only. The paper guard (startup check 1, gate check 1) refuses any mode
but PAPER. Duplicate entries are blocked by `DUPLICATE_OPEN_POSITION`, the advisory lock
around entry and the cap re-checks inside it (`CAPITAL_RACE_LOST`, `DAILY_TRADE_CAP_RACE`).
Broker order placement and reconciliation for **live** money is out of scope until a strategy
passes the evidence gates. Turning it on is a separate, reviewed project (IMPLEMENTATION_ROADMAP.md).
