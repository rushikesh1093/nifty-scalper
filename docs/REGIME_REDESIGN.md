# Regime redesign: the live map is off, regime v2 runs in shadow

31 Oct 2026, at the user's request: "if regime is not working then keep it off rather
than in trade, and redesign". Evidence below comes from three years of NIFTY 1-min
history (Oct 2023 – Oct 2026, years Y1 / Y2 / Y3 from October) with the **live**
classifier replayed exactly (`app/research/live_regime_eval.py`: compute_all, futures
volume, `build_regime_indicators`, `classify_regime`, live defaults).

## 1. What was wrong with the live regime

| Test | Y1 | Y2 | Y3 | Verdict |
|---|---|---|---|---|
| Volatility axis (ATR percentile, live 20-candle window) vs next 30 min's range, Spearman | -0.02 | -0.01 | +0.01 | **No information.** It ranked the ATR against the last 20 minutes, so "HIGH" meant "a bit more than 20 minutes ago". |
| Same with a 486-candle window | +0.35 | +0.37 | +0.24 | Works every year: HIGH → 64-72 pt range, LOW → 41-48 pts |
| Trend labels, next 30 min signed move (BULL_TREND / BEAR_TREND, pts) | -0.4 / -3.0 | 0.0 / +0.4 | +2.5 / -0.7 | Sign changes between years: no direction information |
| Breakout labels (BREAKOUT_BULLISH / BEARISH, pts) | +2.7 / -3.7 | +2.4 / -3.4 | +3.4 / -2.5 | Consistent, but ~3 pts against ~2.6 pts of round-trip cost |
| Map: net R of trades it ALLOWED vs BLOCKED, FIRST_CANDLE_BREAKOUT | -0.16 vs -0.10 | -0.05 vs +0.10 | -0.22 vs -0.14 | It blocked the **better** trades every year |
| Same, PIVOT_BOUNCE (70 % of its trades blocked) | -0.39 vs -0.02 | +0.13 vs -0.08 | -0.31 vs +0.20 | Worse in 2 of 3 years |
| Same, ORB / SUPERTREND_EMA / EMA_RSI | mixed | mixed | mixed | No consistent separation |

Label mix on the trades: TREND_HIGH_VOL 26 %, LOW_VOL_COMPRESSION 18 %, UNCLASSIFIED 16 %:
mostly produced by the broken volatility percentile.

## 2. What changed in the live code

* **`TradingParams.regime_gating = False` (new, default).** The regime is still detected,
  logged and persisted every candle, but in this mode it does not:
  - enable or disable strategies (your strategy flags decide),
  - restrict CE/PE direction,
  - scale position size (multiplier 1.0),
  - turn a candle into a no-trade candle.

  Two regimes **still block** (`ENFORCED_REGIMES`): a detector failure (`DETECTOR_ERROR`,
  or no regime state), because a regime that cannot be computed means broken data; and
  `EXPIRY_DAY`, which only exists when you set `trade_on_expiry_day = False`, so it is
  your policy rather than a regime opinion. `regime_gating = True` restores the old map
  exactly. Files: `core/trading_params.py`, `services/trading/strategy_orchestrator.py`
  (`on_regime_detected`, `get_position_multiplier`, `should_allow_signal`),
  `services/trading/strategy_runner.py` (`_queue_no_trade_regime`).
* **`regime_atr_pct_lookback` 20 → 486** (an existing, validated range). This fixes the v1
  volatility axis for anyone who turns gating back on, and for the persisted labels.
* **Regime v2** (`services/trading/regime_v2.py`, pure, gates nothing):
  - `vol` LOW / NORMAL / HIGH from the 486-candle ATR percentile (< 30 / > 70),
  - `or_side` ABOVE / INSIDE / BELOW today's 09:15-09:30 range (FORMING before 09:30),
  - `INSUFFICIENT_DATA` when there is no candle today or too little history.
  - Trend labels and the name-based strategy map are dropped.
  - Read it at `GET /api/v1/regime/v2?underlying=NIFTY` (also reports whether gating is on).
* Tests: `tests/test_regime_shadow.py`, covering shadow as default, all strategies enabled
  in a no-trade regime, both directions allowed, multiplier 1.0, detector failure still
  blocking, the old map with gating on, and v2 labels. Older tests of the map run with
  `regime_gating=True` (the `v1_regime` fixture), plus
  a test that the expiry-day policy is still enforced in shadow mode.

## 3. Regime v2 against the strategies (MEASURED costs, net R per trade)

| Strategy | LOW vol (Y1 / Y2 / Y3) | NORMAL | HIGH |
|---|---|---|---|
| SUPERTREND_EMA | -0.29 / -0.23 / -0.12 | -0.23 / -0.17 / -0.14 | -0.13 / -0.13 / -0.14 |
| EMA_RSI_PULLBACK_15M | -0.46 / -0.48 / -0.12 | -0.33 / +0.03 / +0.20 | -0.26 / +0.08 / -0.17 |
| PIVOT_BOUNCE | -0.09 / -0.11 / -0.34 | -0.00 / +0.23 / -0.05 | -0.19 / -0.09 / +0.24 |
| FIRST_CANDLE_BREAKOUT | (≤ 3 trades a year) | -0.46 / -0.21 / -0.32 | -0.09 / +0.02 / -0.18 |
| ORB_BREAKOUT | -0.57 / +0.43 / -0.23 | -0.29 / +0.07 / -0.19 | -0.21 / -0.08 / +0.23 |

* **No v2 bucket is profitable for any strategy in all three years**, so there is no
  regime in which to "switch a strategy on".
* **Candidate rule (not enabled): no new entries in LOW volatility.** It was worse every
  year for three strategies, and the reason is mechanical (small moves cannot pay a fixed
  ~2.6-point cost). It was found by looking at these tables, so it needs forward
  confirmation before use.

## 4. How regime gating comes back (if ever)

A v2 rule may gate trades only when, for that strategy, it passes the same gates as a
strategy: chosen before looking at the test data, positive and better than the
strategy's other trades in each year of the history, then confirmed on forward paper
trades (`/api/v1/analytics/strategy-evidence`). Re-run the evidence with
`python -m app.research.live_regime_eval` as the archive and history grow.
