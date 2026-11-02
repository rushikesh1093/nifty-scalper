# System edge scorecard (1 Nov 2026)

A quant review of the whole system as it stands on `feat/pivot-backtest-regime-readiness`,
scored from the evidence already in this repo (STRATEGY_PERFORMANCE_REPORT.md,
WALK_FORWARD_REPORT.md, NEW_STRATEGY_RESULTS.md rounds 1-3, research/FINDINGS.md). No score
below is an opinion about a rule's look; each rests on a tested number.

## Headline

| | Score | One line |
|---|---:|---|
| **Tradeable edge today** | **3 / 10** | One credible edge (expiry-day iron fly, modelled), unconfirmed on real fills; no directional edge |
| **Research and risk process** | **8.5 / 10** | Pre-registration, walk-forward, session bootstrap, cost models, evidence gates: better than most retail and many small-fund setups |

The system is not missing a clever entry. It has proved, honestly, that intraday direction
on NIFTY is a coin flip with these inputs, and that buying options pays ~2.6 points plus
decay per trade that a coin flip cannot cover. Its one edge is on the **selling** side, on
expiry day.

## Component scores

| Component | Score | Evidence |
|---|---:|---|
| Directional signal quality (before costs) | 2 | Every live entry within ±0.05R of zero before costs; MFE ≈ MAE (FINDINGS §8) |
| Directional net edge (option buying) | 1 | 26 rules tested, none survives costs out of sample; walk-forward stands aside 21-25 of 25 months |
| Premium selling (expiry iron fly) | 6 | +0.135R fresh / +0.139R seen, 8 of 9 quarters positive, ~Rs 600-750 per lot per expiry modelled; only 2 real expiries recorded |
| Validation rigour | 9 | Pre-registered rules, design/test split, cluster bootstrap, harsh-cost check, filters must separate |
| Risk controls | 8 | Kill switch, evidence auto-block, puts-only, 2 buys/day cap, defined-risk fly, go/no-go rules |
| Cost and execution realism | 7 | Costs measured from recorded quotes; fly priced by a VIX Black-Scholes model calibrated on ~2 weeks of ticks (0-DTE IV ≠ VIX is the main model risk) |
| Data | 7 | 3 years 1-min spot / VIX / futures; real option quotes only since Oct 2026 (archive now running) |
| Capital fit (Rs 21,000) | 4 | Fly max loss ~Rs 5,900/lot fits; broker expiry-day margin for a hedged lot is likely higher (check before go-live) |

## Keep / cut / add

| Strategy | Verdict | Why |
|---|---|---|
| EXPIRY_IRON_FLY (±200) | **Keep, core** | The only rule positive in both periods |
| EXPIRY_IRON_FLY_W300 | **Keep** (paper) | Most cost-robust variant (only one positive under harsh costs) |
| PIVOT_BOUNCE (puts) | Keep on paper | Best raw signal of the live five; puts positive every year; unproven |
| ORB_BREAKOUT (puts) | Keep on paper | Break-even after costs; auto-block will retire it if it stays ≤ 0 |
| FIRST_CANDLE_BREAKOUT (puts) | Keep on paper | Less bad every year with the chandelier exit; unproven |
| EMA_RSI_PULLBACK_15M | Keep on paper, low-vol skip | Improved every year after both changes; Y1 still negative |
| SUPERTREND_EMA | **Cut** (already blocked) | -0.147R over 2,450 trades, CI wholly below zero |
| CPR_VWAP, puts only | **Added 1 Nov as a paper forward test** (puts only, evidence record from 2 Nov) | +0.16R, PF 1.44, positive in all three years (+0.10 / +0.08 / +0.25R); fails the CI and months gates narrowly, and the put split was chosen after seeing the data |
| VRP / quiet-open filters on the fly | **Not added** | Round 3: VRP filter keeps the worse days in both periods |

### CPR_VWAP puts: the rule as it now runs on paper

* **Setup:** previous session's CPR: P = (H+L+C)/3, BC = (H+L)/2, TC = 2P - BC. Trade only
  when the CPR is narrow, |TC - BC| < 0.20 % of P (a narrow CPR marks a coiled market).
* **Entry (puts):** 5-min bars, 09:30-13:00: the first close below min(TC, BC) that is also
  below session VWAP. Buy the ATM put at the next bar.
* **Stop:** the other side of the CPR, max(TC, BC).
* **Exit:** trail the stop on the 5-min Supertrend(10, 3) once it agrees with the trade;
  exit on a 5-min close across it; square off 15:15. One trade a day.
* **Expectation:** Monte Carlo worst-5 % drawdown -16R, losing streak up to 12. Size so that
  16 losses in a row is survivable.

## Ranked improvements

1. **Get real-fill evidence for the fly.** The go/no-go needs 10 settled expiries on real
   prices; it has 2. Every missed Tuesday delays the one decision that matters. The backend
   must be running 09:25-15:05 on expiry days.
2. **Re-price the fly backtest on real archived quotes** once `options_archive.db` holds ~3
   months of expiries. That removes the main model risk (VIX used as 0-DTE IV).
3. **Stop adding directional option-buying rules.** The marginal one has a very low prior
   after 26 failures. If a directional idea is tested, test it as a defined-risk credit
   spread, where time decay works for it.
4. **CPR_VWAP puts is now forward-testing on paper** (above), judged by the existing paper
   evidence gates (`/api/v1/analytics/strategy-evidence`), not by the backtest.
5. **Next pre-registered fly study, on new data only:** fly management (take profit at 50 %
   of credit vs hold to 15:00, and a 14:30 exit). Do not run it on the 2023-26 data, which
   has now been searched for fly variants three times.
6. **Check capital before real money:** the broker's expiry-day margin for one hedged lot
   against the Rs 21,000 account, with ≥ 30 % to spare (go/no-go rule 5).
