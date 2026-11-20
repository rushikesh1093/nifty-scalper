# Strategy audit (1 Nov 2026)

Every strategy below was run through the same harness (the live classes on 3 years of NIFTY,
next-open fills, pessimistic intrabar path, measured option costs). Net R per trade, years
Y1 / Y2 / Y3 from October.

## The brief's strategy list

| Strategy (brief) | Tested as | Result | Status |
|---|---|---|---|
| ORB + VWAP | ORB_BREAKOUT, ORB_VWAP filter | Break-even; **calls -0.17 / -0.20 / -0.21R every year**; VWAP filter removes no trade | Live, **puts only** |
| VWAP Pullback | VWAP_EMA_PULLBACK | Negative (earlier work) | Disabled by flag |
| EMA 9/21 + VWAP | ST_EMA921 variant, VWAP_EMA_PULLBACK | -0.175R | Not used |
| Supertrend + VWAP | SUPERTREND_EMA + variants | -0.147R, CI -0.18..-0.12, every exit and year | **Blocked by evidence** |
| RSI + VWAP | EMA_RSI_PULLBACK_15M | Better after the trail exit and low-vol skip; Y1 still negative | Live, skips LOW vol |
| PDH/PDL breakout | PDH_PDL (FINDINGS §4) | -0.37R on fresh data | Not used |
| Bollinger breakout | BOLLINGER_SQUEEZE_BREAKOUT (round 2) | Untestable as registered; post-hoc -0.24R | Not added |
| **CPR + VWAP** | CPR_VWAP (round 2) | Both sides +0.04R (fails); **puts +0.10 / +0.08 / +0.25R**, 7 of 9 gates | **Forward-test candidate** |
| Momentum burst | MOMENTUM_BURST | Negative (earlier work) | Disabled by flag |
| EMA / VWAP pullback | VWAP_EMA_PULLBACK, ER variants | Negative | Not used |

Others tested: 09:15 candle breakout (live, puts only, chandelier exit), pivot bounce (live,
puts only), trend-day continuation (+0.05R, fails), gap fade, VWAP reversion, volatility
squeeze (all fail).

## Cross-cutting findings

1. **Bought calls lose** in 14 of 15 strategy-years, and in CPR_VWAP too. NIFTY implied
   volatility falls in rallies, which hurts the call buyer.
2. Entries are close to a coin flip on direction. Costs (~2.6 pts + decay) decide the sign.
3. Trailing exits beat fixed targets wherever a difference showed up.
4. Low volatility hurts option buyers: small moves can't pay fixed costs.

## What the brief asked that the evidence rejects

* **A regime-based strategy selector, and regime confidence**: no regime label made any strategy
  profitable in every year, and trend labels did not predict direction. At the user's
  instruction the regime is **not used** for trading (shadow only).
* **A weighted signal score**: no feature combination survived out-of-sample; a score would rank
  noise. The evidence gates use measured results instead.
* **Duplicate / correlated-signal filter**: already in place (ORB / FCB exclusive group,
  `max_open_per_direction = 1`, aggregator same-direction filter).

Entry / exit / stop / cooldown definitions per live strategy: EVIDENCE_GATES.md ("The
strategy set as it now runs") and the class docstrings in `app/strategies/strategies.py`.
