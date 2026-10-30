# Option execution audit

What it costs to express a NIFTY signal through a bought option, measured on
**recorded** quotes only. No Black-Scholes premium appears anywhere in this
document. Code: `try12/backend/app/research/options.py`; machine-readable
output: `docs/research_results/option_execution.json`, `option_replay.csv`.

## 1. Data actually available

| Item | Available? | Notes |
|---|---|---|
| Historical NIFTY option OHLC | **No** | AngelOne history does not serve expired contracts |
| Recorded option bid/ask/LTP per minute | **11 sessions**, 14-30 Oct 2026 | `research/live_options.db`, 152,384 rows, 240 contracts, 4 weekly expiries, strikes 21,950-23,650 |
| Option volume / open interest | **No** (not stored in the snapshots) | the live selector reads them from the feed; history cannot |
| Delta / IV | **No observed values** | only the live selector's Black-Scholes estimate |
| Lot size on the trade date | schedule in `app/research/data.py` (25 → 75 on 20 Nov 2024 → 65 from 30 Dec 2025) | verify against NSE circulars before using rupees |
| Session coverage of the recordings | partial: 4-378 in-session minutes a day, several days with evening-only rows | out-of-hours rows (stale quotes after 15:30) are **dropped** by the loader |

**Conclusion:** a real-market options backtest over months or years is **not possible
with the data that exists**. Experiment B is therefore (a) a cost measurement on every
recorded minute and (b) a replay of the few Experiment A trades that fall on recorded
days. It is not, and is not presented as, a profitability test.

## 2. Measured execution cost (every recorded minute)

Method: at every in-session minute, the ATM and one-strike-OTM contract of the nearest
expiry in the trade's direction; buy at the ask, sell at the bid *h* minutes later.
Regress that P&L on the spot move: `P&L ≈ a + b·move`. `b` is the effective delta,
`-a/b` the round trip's cost in **spot points** (spread + decay over the hold), which a
signal has to out-run before charges.

Spread (ask − bid, rupees):

| contract | non-expiry median | p90 | expiry-day median | p90 |
|---|---:|---:|---:|---:|
| ATM | 0.30 | 0.40 | 0.15 | 0.20 |
| 1 OTM | 0.25 | 0.35 | 0.10 | 0.15 |

Round trip (non-expiry days, nearest weekly; n = 1,414-3,038 minute samples per row):

| hold | ATM eff. delta | ATM cost, spot pts | 1-OTM eff. delta | 1-OTM cost, spot pts |
|---|---:|---:|---:|---:|
| 5 min | 0.53 | 0.72 | 0.47 | 0.69 |
| 15 min | 0.54 | 0.94 | 0.48 | 0.94 |
| 30 min | 0.55 | 0.86 | 0.48 | 0.85 |
| 60 min | 0.53 | 0.40 | 0.47 | 0.31 |
| 120 min | 0.52 | 0.77 | 0.46 | 0.76 |

Expiry day, 0-DTE contract: 0.7 pts (5 min) rising to **4.3 pts (60 min) and 9.8 pts
(120 min)** for ATM, 7.2 / 14.9 pts for 1 OTM — the decay of the expiring series. The
live selector never buys it (DTE bands 2-7, 8-14), so this does not hit live trades.

Charges (brokerage ₹20 per order + STT 0.15 % sell + exchange 0.0355 % + SEBI + stamp + GST,
`fills.statutory_charges`): **₹62.6 per lot round trip at a ₹100 premium, lot 65 ≈ ₹0.96
per unit ≈ 1.8 spot points** at delta 0.53.

Reading:
* Spread and slippage are small (~0.7-0.9 spot points). The cost a scalper pays is
  dominated by **charges (~1.8 pts)**, plus decay that these 11 sessions cannot pin down:
  the non-expiry intercept does not grow with the hold, which means IV rose over the
  sample and offset theta, not that theta is zero.
* ATM vs 1 OTM: 1 OTM costs about the same in spot points with ~10 % less delta, so it
  needs proportionally more spot movement per rupee; there is no execution reason to
  prefer it. The selector's ATM/1-OTM preference is fine either way.
* Paper fills are realistic: the engine enters at ask × (1 + 0.05 %) and exits at
  bid × (1 − 0.05 %) when the quote is valid — the same prices used here.

Cost models used in the performance report (spot points per round trip):

| model | fixed | decay per hour held | basis |
|---|---:|---:|---|
| ZERO | 0 | 0 | signal quality only |
| **MEASURED** (primary) | 2.6 | 1.5 | 0.8 spread + 1.8 charges; decay rounded UP from the noisy measurement |
| HARSH | 4.0 | 4.0 | the earlier research's assumption |

## 3. Experiment B: the same trades on recorded quotes

See the table "Experiment B — recorded-quote option replay" in STRATEGY_PERFORMANCE_REPORT (`research_results/option_replay.csv`): every
baseline trade whose day has recorded quotes, entered at the ask of the fill minute and
exited at the bid of the exit minute, ATM and 1 OTM, with charges at the trade date's lot
size. A trade whose contract has no valid quote in either minute is reported
`UNAVAILABLE` with the reason — never priced from a model.

## 4. Where losses come from (A vs B)

Experiment A (STRATEGY_PERFORMANCE_REPORT, first table) shows the underlying signals are at or below zero R **before any
cost**. Option execution adds ~2.6 points per trade plus decay; on these strategies' stop
distances that is 0.05-0.3 R per trade. **Losses originate in signal quality; execution
cost converts a zero edge into a reliable loss.** Better execution (cheaper contracts,
fewer charges) can shrink the loss but cannot create an edge that the underlying signal
does not have.

## 5. Option selector audit (Part 7)

The live selector already does what Part 7 asks, verified in `option_selector.py`:
direction-correct CE/PE, correct underlying, DTE bands (never 0-1 DTE), premium band and
affordability, quote validity and staleness, spread cap, OI and volume floors (missing =
rejection, `-1` sentinel distinguished from zero), slippage cap, then a score. Gaps:
delta is estimated, not observed (finding O-1); there is no historical record of the
rejected candidates' later prices, so selection quality itself cannot be backtested.
Sizing remains ONE_LOT-capable via `max_lots_per_trade`; the only sizing change made is
counting round-trip costs in the risk cap (finding F-2).
