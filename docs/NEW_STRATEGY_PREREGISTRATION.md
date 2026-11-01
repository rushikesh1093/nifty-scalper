# Pre-registration: new candidate strategies (30 Oct 2026, before any result)

Written and committed before the candidates were run. Rules, parameters, windows
and pass criteria below are final for this test. Anything changed after the run
is reported as post-hoc and cannot pass.

## Why these four

The previous run ([STRATEGY_PERFORMANCE_REPORT.md](STRATEGY_PERFORMANCE_REPORT.md))
found:
1. Entries were a coin flip on direction.
2. Costs (~2.6 spot pts + 1.5 pts/h) cost 0.10-0.25R a trade on tight structural stops.
3. The **volatility** state predicts the next 30 minutes' range out of sample. The trend
   state does not predict direction.
4. Trend-following exits (5-min Supertrend trail) were the least bad, because longer
   holds dilute a fixed cost.

So every candidate trades **at most once a day**, uses **wide structural stops**, and
targets **large moves** (a trailing exit, or a far fixed target), so cost is a small
fraction of R. Each tests one market hypothesis that is not one of the five existing
entries.

All are NIFTY, 5-min bars, built from what the live runner provides (today's candles
and the last 500 1-min candles), and use only completed bars. Common exit unless stated:
a stop that only tightens, trailing the 5-min Supertrend(10, 3) line; exit on a 5-min
close across that line; square-off 15:15. One trade per day per strategy. The live
11:30-12:15 blackout and 15:05 cutoff apply.

| ID | Hypothesis | Entry | Stop | Exit |
|---|---|---|---|---|
| **VOL_SQUEEZE_BREAKOUT** | Compression is followed by expansion; the break of a quiet box starts a move bigger than costs | 09:45-14:00. The mean 5-min range of the last 12 bars is at or below the 25th percentile of the rolling 12-bar mean ranges over the previous 88 bars. A 5-min close beyond the high (low) of those 12 bars, on a bar whose range is ≥ 1.5× their mean range | the box's midpoint | common trail |
| **TREND_DAY_CONTINUATION** | On days already trending strongly by late morning, the trend persists into the close (intraday momentum) | 11:00-13:30. The close is ≥ 0.5 % from today's 09:15 open; the last 6 5-min closes are on that side of session VWAP. A pullback bar's low (high) comes within 0.25 ATR(14, 5-min) of the 5-min EMA 20, then a bar closes beyond the previous bar's high (low) in the day's direction | pullback extreme ∓ 0.25 ATR | common trail |
| **GAP_FADE** | Medium opening gaps that fail to extend in the first 15 minutes tend to fill | Gap \|open − previous close\| between 0.3 % and 1.0 %. 09:30-10:30: a 5-min close beyond the 09:15-09:30 range on the gap-filling side | the other side of the 09:15-09:30 range | target the previous close; exit 14:45 |
| **VWAP_EXTREME_REVERSION** | In trendless high-volatility tape, stretches far from VWAP revert | 10:00-14:30. ADX(14, 5-min) < 20; the previous bar closed ≥ 2.5 ATR from session VWAP; the trigger bar closes back toward VWAP beyond the previous bar's midpoint | the stretch extreme ± 0.5 ATR | target the session VWAP at entry; exit 14:45 |

## Data, costs, windows

* `research/nifty_history.db`, 728 sessions, 23 Oct 2023 – 23 Oct 2026, the same harness
  (`app/research/sim.py`): live-shaped classes, fill at the next open, pessimistic
  intrabar path, same-candle stop+target = stop.
* Costs: MEASURED (2.6 pts + 1.5 pts/h) is primary; ZERO and HARSH (4 + 4/h) are
  reported.
* Design window 23 Oct 2023 – 30 Sep 2024; test window 1 Oct 2024 – 23 Oct 2026. No
  parameter is tuned on either.

## Pass criteria (all, MEASURED costs)

1. Test window: n ≥ 100, net expectancy > 0 with the session-bootstrap 95 % CI lower bound
   > 0, profit factor ≥ 1.20, still positive without the best session, ≥ 50 % of
   months with trades positive, max drawdown ≥ -25R.
2. Design window: net expectancy > 0 (the sign must not flip between periods).
3. HARSH costs, test window: expectancy > -0.05R (survives pessimistic costs).

A candidate that passes is added to the live code as a **disabled** strategy for
forward paper trading only. A candidate that fails is not added.

**Honest caveat:** the test window has been looked at in aggregate for the five
existing strategies, so it is not perfectly fresh for ideas formed after that look.
Forward paper results are the only clean test of any candidate that passes.

---

# Round 2 (1 Nov 2026, before any result): Bollinger breakout and CPR + VWAP

The two strategies in the master brief that have not been tested. Same data, harness,
costs, windows, common 5-min Supertrend(10, 3) trail, one trade a day per strategy, and
**the same pass criteria** as round 1 above. Parameters are the standard published ones,
not fitted.

| ID | Entry | Stop | Exit |
|---|---|---|---|
| **BOLLINGER_SQUEEZE_BREAKOUT** | 5-min bars, 09:45-14:00. Bollinger(20, 2) on 5-min closes. The band width (upper - lower) / middle of the previous bar is at or below the 25th percentile of the previous 88 bars' widths (a squeeze), and the bar closes outside the band | the middle band (SMA 20) at entry | common trail |
| **CPR_VWAP** | Previous session's CPR: pivot P = (H+L+C)/3, BC = (H+L)/2, TC = 2P - BC. Only when the CPR is narrow: \|TC - BC\| < 0.20 % of P. 5-min bars, 09:30-13:00: the first close above max(TC, BC) that is also above session VWAP (long), or below min(TC, BC) and below VWAP (short) | the other side of the CPR | common trail |

Both are reported for both directions, and by side (CE / PE), because round 1 found calls
negative almost everywhere.
