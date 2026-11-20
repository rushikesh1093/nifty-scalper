# Risk management audit (1 Nov 2026)

All checks live in `app/services/trading/risk_gate.py`, separate from the strategies. No
strategy can place a position except through `paper_engine.handle_signal`, which runs the
gate first. Every refusal writes a `no_trade_decisions` row (stage, reason code, context) and
a SKIPPED signal. That is the auditable risk decision log (`/api/v1/analytics/no-trade-decisions`).

## Controls, in gate order

| # | Control | Setting | State |
|---|---|---|---|
| 0 | R:R floor on the signal | `min_rr` 2.0 | unchanged |
| 1 | Paper mode only | startup + gate | unchanged |
| 2 | Kill switch | admin key | unchanged |
| 2a | **Evidence gates** | `evidence_blocked_strategies`, `evidence_auto_block`, `pe_only_strategies`, `low_vol_skip_strategies` | new (EVIDENCE_GATES.md) |
| 2b | Entry blackout 11:30-12:15 | `entry_blackout_windows` | unchanged |
| 3 | Entry cutoff (square-off − 10 min) | `entry_cutoff_buffer_min` | unchanged |
| 4 | Daily loss, realised + MTM, ratcheted | `max_daily_loss`, `_pct` | unchanged |
| 5-7 | Losing-streak halt, post-loss cooldown, per-strategy halt | `losing_streak_halt_n` | unchanged |
| 8 | Daily trade cap | **min(`max_trades_per_day`, `max_option_buys_per_day` = 2)** | tightened |
| 9 | Max open positions, max per direction (correlated exposure) | `max_open_positions`, `max_open_per_direction` 1 | unchanged |
| 10 | One bet a day per correlated group (ORB / FCB) | `exclusive_strategy_groups` | unchanged |
| 11 | Kelly refuses no-edge strategies; never above the fixed fraction | `kelly_*` | unchanged |
| sizing | Lots capped by risk at the stop **plus round-trip costs** | `max_risk_per_trade_pct` 12.5 | fixed (F-2) |
| entry | Capital / cap / duplicate re-checks under the advisory lock | | unchanged |

ONE_LOT policy, capital and `max_lots_per_trade` are unchanged. Nothing increases size after
losses, and no automatic capital scaling exists.

## Measured sizing risk

For the best candidate found (CPR_VWAP puts), a Monte Carlo over session orders gives a
worst-5 % drawdown of **-16R** and losing streaks of **12**. With ₹21,000 and one lot risking
roughly 5-12 % of capital at the stop, -16R would exceed the account. The daily-loss halt and
the 2-loss streak halt cut sequences short, but they do not change the arithmetic. **Real
money needs either more capital or smaller risk per trade** than one NIFTY lot allows today.
That is a capital question, not a code change.

## Gaps (not changed)

* Volatility-based size adjustment: not added. No volatility bucket made a strategy reliably
  profitable, so scaling size by it would be unfounded.
* Live-order reconciliation and broker-side duplicate protection: paper-only today (SYSTEM_AUDIT.md).
