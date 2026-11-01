# Evidence gates and the current strategy set (31 Oct 2026; puts-only and buy cap added)

**What this does and does not do.** Nothing here makes a strategy profitable. No entry
or exit change survived testing on three years of NIFTY in every year, so none can be
claimed. What these gates do is stop the system from taking trades the evidence shows
losing, and let each strategy's own paper results switch it off automatically. That
raises *expected* results by cutting known losses, not by finding new edge.

## The gates (risk gate, step 2a, after the kill switch)

| Gate | Setting (TradingParams) | Default | Evidence |
|---|---|---|---|
| Research block | `evidence_blocked_strategies` | `("SUPERTREND_EMA",)` | Negative after costs in all 4 entry variants, all 7 exit engines and each of the 3 years: -0.147 R a trade over 2,450 trades (CI -0.176..-0.118) |
| Paper auto-block | `evidence_auto_block` | `True` | A strategy whose own closed paper trades since its `RULE_SINCE` date are `DISABLED_FOR_RESEARCH` (≥ 100 trades and negative, or a CI wholly below 0) is refused. Computed once a day per run. If the statistics fail to compute, nothing is blocked. |
| Low-volatility skip | `low_vol_skip_strategies` | `("EMA_RSI_PULLBACK_15M",)` | Regime v2 LOW volatility: skipping it improved EMA_RSI in every year (-0.31 → -0.29, -0.05 → +0.07, -0.08 → -0.08 R). For PIVOT, ORB, FCB and SUPERTREND it made at least one year worse, so they are not listed. |
| Puts only | `pe_only_strategies` | `("ORB_BREAKOUT", "PIVOT_BOUNCE", "FIRST_CANDLE_BREAKOUT")` | Bought calls lost in 14 of 15 strategy-years 2023-26; ORB calls -0.17 / -0.20 / -0.21 R. Puts did better in most years, and every year for PIVOT. Mechanism: NIFTY implied volatility falls as the index rallies and rises as it falls. **Found in the same data, so it is a forward hypothesis**, judged by paper evidence from 2 Nov. Call signals are refused as `DIRECTION_POLICY`, so FCB's calls remain visible ("watch only"). |
| Daily buy cap | `max_option_buys_per_day` | `2` | Fewer, larger trades: the ~2.6-point round-trip cost is paid per trade. The lower of this and your `max_trades_per_day` applies. `0` = no extra cap. |

Refusals are recorded as no-trade decisions (`EVIDENCE_BLOCKED`, `LOW_VOL_SKIP`, `DIRECTION_POLICY`, `DAILY_TRADE_CAP`) and show
in `/api/v1/analytics/no-trade-decisions`. Every gate is a setting: `()` / `False`
switches it off.

## The strategy set as it now runs (paper)

| Strategy | Entry | Exit | Gates | Evidence status |
|---|---|---|---|---|
| FIRST_CANDLE_BREAKOUT | 5-min close below the 09:15 candle - 10 pts (**puts only**; call breakouts are recorded, not traded), 09:20-10:00, stop at the other side (max 50 pts) | **ATR chandelier** (best price − 2 × 5-min ATR, every 1-min close), to 15:15 | paper auto-block | Less bad every year (+0.02 / +0.04 / -0.07 R); not proven |
| ORB_BREAKOUT | 5-min close below the 09:15-09:30 range (40-120 pts) with futures volume, by 11:00 (**puts only**) | Target = level ± range width, breakeven at 50 %, 90-min time stop | paper auto-block; ORB/FCB one bet a day | Break-even after costs; unproven |
| SUPERTREND_EMA | 1-min Supertrend flip + EMA 5/13 cross | 8-pt premium target, Supertrend trail, 10 min | **blocked** | Reliably negative |
| EMA_RSI_PULLBACK_15M | 15-min EMA 9/21 trend, RSI pullback, close back across EMA 9 | **5-min Supertrend(10, 3) trail + flip**, to 15:15 | **skips LOW volatility**; paper auto-block | Better every year after both changes; Y1 still lost |
| PIVOT_BOUNCE | 3-min rejection down from PP / R1 (**puts only**) | Stop 15 pts beyond the level, target next pivot, 14:45 | paper auto-block | Positive raw signal, wide CI; unproven |
| CPR_VWAP (added 1 Nov) | Yesterday's CPR narrow (< 0.20 % of P); 5-min close below the CPR and below session VWAP, 09:30-13:00 (**puts only**) | Stop at the top of the CPR; **5-min Supertrend(10, 3) trail + flip**, to 15:15; one a day | paper auto-block | Paper forward test: puts +0.10 / +0.08 / +0.25 R by year, +0.16R overall after costs, PF 1.44; fails the CI and months gates narrowly, put side chosen after seeing the split |
| EXPIRY_IRON_FLY (separate book) | Expiry day 09:30: sell ATM CE + PE, buy ± 200 wings | 15:00 | its own go/no-go rules (FINDINGS §12) | The only idea positive in both old and fresh years |
| Regime | v1 detected but **not gating** (`regime_gating=False`); v2 computed each candle | — | detector failure and the expiry-day policy still block | REGIME_REDESIGN.md |

## How a strategy earns its way back, or into real money

Paper evidence at `GET /api/v1/analytics/strategy-evidence` must reach `PASSES_GATES`:
≥ 100 trades on ≥ 60 sessions since its last rule change, net expectancy above 0 with the
95 % CI above 0, PF ≥ 1.2, still positive without its best day, drawdown ≥ -25 R, and at
least half of months positive. Until then: paper only.

## Portfolio shape after these changes

At most **2 option-buying trades a day**, mostly **puts**, every winner **trailed**:
FCB and EMA_RSI by trailing stops, ORB by breakeven + target, PIVOT to the next level.
SUPERTREND_EMA is off. Premium selling, the expiry-day iron fly, runs in its own book and
is the intended core once its go/no-go rules pass.
