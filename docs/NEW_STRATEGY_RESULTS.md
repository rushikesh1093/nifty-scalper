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
