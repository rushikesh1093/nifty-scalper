# New candidate strategies: results

Rules, windows and pass criteria were fixed in
[NEW_STRATEGY_PREREGISTRATION.md](NEW_STRATEGY_PREREGISTRATION.md) (commit `055c30e`)
before this run. Run: `python -m app.research.cli --suite new` at commit `6d528d5`, 728
sessions, the same harness, costs and statistics as the main report. Data:
`research_results/new_candidates/`.

## Verdict: none passes, so none is added to the live code

| Candidate | Test-window n | Net exp R, MEASURED (95 % CI) | PF | Without best session | Months + | Design-window net exp R | HARSH net exp R | Result |
|---|---:|---|---:|---:|---:|---:|---:|---|
| TREND_DAY_CONTINUATION | 114 | +0.053 (-0.261..+0.406) | 1.09 | -0.028 | 10 / 25 | +0.003 (n=44) | -0.095 | **fail**: CI includes 0, PF < 1.2, not positive without its best day, < 50 % months, HARSH < -0.05 |
| GAP_FADE | 62 | -0.146 (-0.381..+0.083) | 0.69 | -0.192 | – | -0.074 (n=46) | -0.214 | **fail** |
| VWAP_EXTREME_REVERSION | 106 | -0.227 (-0.441..+0.016) | 0.66 | -0.256 | – | -0.063 (n=49) | -0.331 | **fail** |
| VOL_SQUEEZE_BREAKOUT | 24 | -0.157 (-0.803..+0.668) | 0.79 | -0.488 | – | -1.089 (n=1) | -0.340 | **fail**: the setup is rare (25 trades in 3 years) |

The walk-forward selector never chose any of them for a test month at MEASURED cost (no
3-month train window had ≥ 20 trades with positive expectancy).

## What was learned

* **TREND_DAY_CONTINUATION has the best raw signal found so far.** Before costs it is
  +0.16R in the design year and +0.20R after it, the same sign in both periods, which no
  existing strategy managed. 30-min direction is right 56 % of the time. But each trade
  costs ~0.14R, which leaves +0.05R with an interval from -0.26 to +0.41. That is not
  evidence of profit. It is also lopsided: shorts +0.25R (n=64), longs -0.20R (n=50),
  and that split was not pre-registered, so it cannot be used.
* Other exits on the same trend-day entries (reported only, post-hoc): -0.02R to +0.09R
  after costs, every one with a sign or size change between periods. The exit is not
  what is missing.
* Mean reversion (gap fade, VWAP stretch) is negative before costs: NIFTY intraday
  stretches and medium gaps did not revert enough to pay for the stop.
* The squeeze condition, as specified, almost never occurs on a 500-candle reference
  window.

## Where that leaves "a strategy that makes profit"

Across this work, 22 directional option-buying rules (the 5 live rules, 13 variants and
these 4) have been tested on the same three years. None shows positive net expectancy
that survives out of sample. The consistent reasons:
1. Intraday NIFTY direction is close to unpredictable with these inputs. The
   regime-validation report found trend state does not predict the next 30 minutes.
2. A bought option pays ~2.6 points plus decay per round trip, which a near-zero edge
   cannot cover.

The one lead with positive evidence on both the old and the fresh years is on the
**selling** side: the expiry-day ATM iron fly (FINDINGS.md §6, §11). It is already
running in its own paper book (`/api/v1/trading/iron-fly`), with fixed go/no-go rules
before any real money (FINDINGS.md §12). Its forward paper record is the most useful
evidence to collect next.

TREND_DAY_CONTINUATION stays in `app/research/new_strategies.py` as a documented
hypothesis. It is not registered in `build_strategies`. If you want it paper-traded
anyway, it can be added as a disabled flag. It would then be a forward test of an
unproven idea, not a strategy with an edge.

---

# Round 2 results (1 Nov 2026): Bollinger squeeze breakout, CPR + VWAP

Rules fixed in NEW_STRATEGY_PREREGISTRATION.md, round 2 (commit `ddb16de`). Data in
`research_results/new_candidates_round2/`.

**BOLLINGER_SQUEEZE_BREAKOUT: not testable as registered.** It needs 20 + 88 + 1 = 109
five-minute bars of history; the live runner keeps 500 one-minute candles (about 100), so it
never fired. A **post-hoc** run with a 70-bar lookback (which can never pass) is clearly
negative: **-0.24R** after costs (n = 322 after the design year, CI -0.41..-0.07, PF 0.66).
Not added.

**CPR_VWAP (both directions): fails.** Net +0.04R (CI -0.10..+0.19, PF 1.10), design year
-0.11R, harsh -0.07R. By side it is two different rules:

| side | Y1 | Y2 | Y3 (net R, MEASURED) |
|---|---:|---:|---:|
| calls (CE) | -0.22 (97) | -0.10 (61) | -0.09 (80) |
| **puts (PE)** | **+0.10 (50)** | **+0.08 (81)** | **+0.25 (77)** |

**CPR_VWAP puts only**, under the round-1 gates (selecting the put side follows round 1's
"calls lose" finding, but it was chosen after seeing this split, so it is not independent proof):

| gate | result | |
|---|---|---|
| n / sessions after the design year | 158 / 158 | ✅ |
| net expectancy | +0.16R | ✅ |
| design year | +0.10R (n = 50) | ✅ |
| profit factor | 1.44 | ✅ |
| without the best day | +0.10R | ✅ |
| max drawdown | -10.3R | ✅ |
| harsh costs | +0.05R | ✅ |
| 95 % CI lower bound > 0 | -0.05 | ❌ |
| ≥ 50 % months positive | 12 / 25 | ❌ |

Calmar 3.9, Sharpe(daily) 1.7, Sortino 4.5. Monte Carlo over 2,000 session orders: max drawdown
-10R typical, **-16R in the worst 5 %**, losing streak 8 typical, **12 in the worst 5 %**.

**Verdict: the most promising rule found so far, and still not proven.** It fails two gates
narrowly. It is not added to live trading under the pre-registered rule. It is a
candidate for a forward paper test.

---

# Round 3 results (1 Nov 2026): volatility-risk-premium filters on the iron fly

Rules fixed in NEW_STRATEGY_PREREGISTRATION.md, round 3 (commit `d109266`), before this run.
Run: `python -B research/protocol8_screen.py research/nifty_history.db research/live_options.db`
(647 sessions with the 60-session trailing window). "Real" costs unless stated.

| Rule | Fresh kept n | Fresh kept R (95 % CI) | Fresh skipped R | Seen kept R | Seen skipped R | Harsh kept R | Result |
|---|---:|---|---:|---:|---:|---:|---|
| IF0_VRP | 40 | +0.068 (-0.14..+0.27) | **+0.190** | +0.088 | **+0.177** | -0.086 | **fail**: the filter keeps the *worse* days in both periods |
| IF0_QUIET | 47 | +0.123 (-0.07..+0.29) | +0.148 | +0.171 | +0.105 | -0.017 | **fail**: separates in one period only, CI includes 0 |
| IF0_W300_VRP | 40 | +0.037 (-0.12..+0.18) | +0.139 | +0.068 | +0.152 | -0.033 | **fail**: same inversion as IF0_VRP |
| NX_FLY_BOTH | 83 | -0.118 (-0.15..-0.09) | -0.170 | -0.145 | -0.159 | -0.493 | **fail**: non-expiry flies lose even on the best-filtered days |

Unfiltered expiry fly on the same days: **fresh +0.135R, seen +0.139R**.

## What was learned

* **Do not filter the expiry fly on VIX vs realised volatility.** Days when VIX is high
  against the last 10 sessions' realised moves were the *worse* fly days in both periods
  (+0.07 / +0.09R kept vs +0.19 / +0.18R skipped). A likely reason: India VIX is a 30-day
  measure, so a VIX that is high against recent calm is the market pricing a coming move,
  not rich 0-DTE premium. The textbook VRP filter is the wrong instrument here.
* A quiet opening range does not reliably pick better fly days.
* Non-expiry intraday flies lose (about -0.15R) and no filter tested rescues them: the
  edge is specific to expiry-day theta.
* The unfiltered expiry fly is the same in both periods (+0.135 / +0.139R), which is the
  most stable number in all of this research. Nothing is changed in the live fly.
