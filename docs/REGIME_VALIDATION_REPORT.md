# Regime validation report

Code: `try12/backend/app/research/regime.py` (classifier),
`app/research/regime_validation.py` (this report). Results:
`research_results/regime_validation.json`. Design window 23 Oct 2023 – 30 Sep 2024
(226 sessions, 14,237 labelled 5-min closes). Post-design 1 Oct 2024 – 23 Oct 2026
(497 sessions, 31,183 closes).

## 1. The classifier

Causal, from completed 5-min bars and earlier sessions only (tested: warm-up returns
`INSUFFICIENT_DATA`, and appending future bars cannot change a past label because
the function only receives bars up to the decision time).

* **Volatility axis:** the last hour's mean 5-min range as a percentile of the previous
  20 sessions' mean range → LOW (< 20th) / NORMAL / HIGH (> 80th).
* **Structure axis:** ADX(14), EMA-20 slope over 30 min in ATR, and persistence (share
  of the last 12 closes on the slope's side) → TREND_UP/DOWN_STRONG (ADX ≥ 25, |slope| ≥
  0.5 ATR, persistence ≥ 0.75), WEAK_TREND_UP/DOWN (ADX ≥ 20, |slope| ≥ 0.25), else
  RANGE.
* **Combined label:** INSUFFICIENT_DATA, OPENING (≤ 09:45), the trend labels,
  HIGH_VOL_CHOP (HIGH vol, no trend, ≥ 3 VWAP crosses today), COMPRESSION (LOW vol, no
  trend), RANGE. `transition` marks a structure change versus 15 minutes earlier.
  Stale data is handled upstream (the loader drops short sessions; live has its own
  staleness gates).
* Thresholds are conventional round numbers fixed before any result. They are not
  fitted.

The live `RegimeDetector` (async, Redis/Postgres-backed, with a stabiliser) was not
re-run over three years. The research classifier is a separate, causal
re-implementation of the same ideas, so its validation says what the *features* can
do. It is not a test of the live detector's exact labels.

## 2. Stability

| period | label changes per hour | mean run (5-min bars) | transition share |
|---|---:|---:|---:|
| design | 1.56 | 6.85 (~34 min) | 26.7 % |
| post-design | 1.65 | 6.50 (~33 min) | 28.3 % |

Stable across periods. A label lasts about half an hour: not flickering, and not so
smoothed that it lags a session's real changes.

## 3. Does the label predict the next 30 minutes?

**Volatility: yes, and it holds out of sample.** Mean high-low range of the next 30 min:

| vol bucket | design (pts, ±se by session) | post-design |
|---|---:|---:|
| LOW | 34.9 ± 1.5 | 40.6 ± 1.2 |
| NORMAL | 48.2 ± 1.2 | 55.1 ± 1.0 |
| HIGH | 76.2 ± 2.7 | 72.6 ± 1.1 |

**Direction: no.** Mean signed move over the next 30 min, in ATR (± se by session):

| label | design | post-design |
|---|---:|---:|
| TREND_UP_STRONG | +0.140 ± 0.087 | +0.036 ± 0.064 |
| TREND_DOWN_STRONG | -0.110 ± 0.095 | -0.032 ± 0.056 |
| WEAK_TREND_UP | +0.016 ± 0.090 | -0.065 ± 0.058 |
| WEAK_TREND_DOWN | +0.071 ± 0.101 | -0.072 ± 0.066 |
| RANGE | +0.011 ± 0.087 | -0.125 ± 0.064 |
| COMPRESSION | +0.017 ± 0.117 | -0.122 ± 0.083 |
| HIGH_VOL_CHOP | +0.194 ± 0.132 | +0.049 ± 0.093 |

Trend labels point the right way, but by less than 1.6 standard errors in the design
window and less than 0.6 after it. Intraday NIFTY trend state, measured this way, has
no reliable continuation over 30 minutes. That is consistent with the entry strategies
having no edge: a trend-following entry cannot beat a market whose trend label does not
predict the next move.

## 4. Strategy × regime (the evidence a regime → strategy map needs)

Net expectancy per regime label at signal time, MEASURED costs. A mapping would need a
label that is positive in the design window **and** after it, with enough trades.

| strategy | labels with n ≥ 10 in both periods | positive in both? |
|---|---|---|
| FIRST_CANDLE_BREAKOUT | OPENING (133 / 318): -0.153 → -0.082; RANGE (12 / 9 n<10 post); TREND_DOWN_STRONG (10 / 10): -0.027 → +0.117; TREND_UP_STRONG (13 / 15): -0.353 → -0.392 | **none** |
| ORB_BREAKOUT | OPENING (15 / 30): -0.128 → +0.181; RANGE (17 / 31): -0.352 → +0.227; TREND_DOWN_STRONG (10 / 18): -0.158 → +0.175; TREND_UP_STRONG (16 / 21): -0.523 → -0.182; WEAK_TREND_UP (10 / 21): -0.073 → -0.015 | **none** (every positive post-design cell was negative in design) |
| SUPERTREND_EMA | all nine labels | **none**: negative in every label in both periods |
| EMA_RSI_PULLBACK_15M | OPENING (46 / 100): -0.208 → -0.063; RANGE (36 / 63): -0.407 → +0.009; HIGH_VOL_CHOP (26 / 28): -0.455 → +0.030; COMPRESSION (12 / 32): +0.087 → -0.348; WEAK_TREND_DOWN (13 / 17): -0.237 → +0.156 | **none** |
| PIVOT_BOUNCE | OPENING (42 / 92): +0.168 → -0.125; RANGE (29 / 60): -0.395 → -0.012; TREND_UP_STRONG (19 / 42): +0.713 → -0.016; WEAK_TREND_UP (16 / 29): -0.664 → +0.336; WEAK_TREND_DOWN (15 / 37): -0.502 → +0.315; TREND_DOWN_STRONG (14 / 31): -0.231 → -0.153 | **none** |

The walk-forward's learned regime filter confirms this from the other side: it passed
train and validation in only 6 of 125 strategy-months, and every one of those test
months lost money or had no trade (FCB -3.04R, ST -0.69R / -0.52R / -4.49R,
EMA_RSI -1.56R, PIVOT no trade; `walk_forward.json`).

## 5. Conclusions and what was changed

1. The **volatility** axis is validated (monotone and stable forward range). It is a
   sound input for sizing stops, cost-to-risk checks and no-trade rules on cost
   grounds. It is not a sound input for picking direction.
2. The **trend** axis does not predict direction out of sample.
3. **No regime → strategy mapping is supported by independent evidence.** The live
   `REGIME_RULES` map (trend strategies in trend regimes, PIVOT in range regimes) is
   assigned by strategy names. The data neither supports it nor offers a validated
   replacement: PIVOT in RANGE was -0.39R / -0.01R, and SUPERTREND_EMA is negative in
   every regime.
4. Nothing in the live regime code was changed: replacing one unvalidated map with
   another would be the same mistake. The research classifier and this report are the
   tools to validate a future map. The orchestrator already stands aside when no
   strategy is enabled; the walk-forward shows that standing aside is the correct
   default here.
5. Regime-level expectancy and drawdown per strategy are in `summary.json`
   (`breakdowns_post_design.regime`) and the performance report's breakdown tables.
