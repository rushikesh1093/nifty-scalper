# Combined Trend-Following Options Strategy
## NIFTY and BANKNIFTY — Implementation Prompt and Specification

## Objective

Use the existing 12-strategy Python module and the current application architecture to build one unified intraday trend-following decision engine for **NIFTY** and **BANKNIFTY**.

The engine must classify the market as **UPTREND**, **DOWNTREND**, or **SIDEWAYS**, then produce one of these decisions:

- **BUY CE** when a bullish trend and valid entry are confirmed.
- **BUY PE** when a bearish trend and valid entry are confirmed.
- **NO TRADE** when the market is sideways, the setup is weak, signals conflict, or risk conditions are unsuitable.

The system should prefer fewer, higher-quality trend-following entries over frequent trades. A 20–30 point target is an objective only when the chosen instrument, volatility, and market structure support it; it is never guaranteed.

---

## 1. Work With the Existing Project

Use the supplied 12-strategy file and the existing application files, including:

- `app/strategies/base.py`
- `app/strategy_runner.py`

Preserve the existing project architecture and interfaces wherever practical. Before implementing changes, inspect how strategies are registered, how `SignalEvent` objects are created and published, how the signal aggregator handles conflicts/cooldowns, and how the paper-trading engine selects and manages option positions.

Do not create a separate competing runner or duplicate an existing risk-management system. Integrate the unified decision engine into the current application.

### Existing strategy families to consider

The uploaded strategy module contains these 12 approaches:

1. Opening Range Breakout (ORB), VWAP and volume
2. VWAP pullback
3. EMA/VWAP trend
4. Supertrend/VWAP
5. Previous-day high/low
6. VWAP reversal
7. Bollinger Band breakout
8. RSI/VWAP
9. Open-interest spike
10. Implied-volatility crush
11. CPR/VWAP
12. Gap/VWAP fill

Use these as sources of evidence, not as 12 independent signals that automatically trigger 12 separate trades. Trend-following evidence should have priority. Reversal-oriented or options-flow evidence must not override a confirmed market trend on its own.

---

## 2. Market Regime Classification

Classify each underlying independently: NIFTY and BANKNIFTY.

### UPTREND candidate

Require a meaningful combination of:

- Spot price above VWAP
- EMA 9 above EMA 21, with sufficient separation or positive slope
- Bullish market structure (higher highs/higher lows, or a confirmed bullish breakout)
- Entry confirmation from a pullback rejection or breakout/retest

### DOWNTREND candidate

Require a meaningful combination of:

- Spot price below VWAP
- EMA 9 below EMA 21, with sufficient separation or negative slope
- Bearish market structure (lower highs/lower lows, or a confirmed bearish breakdown)
- Entry confirmation from a pullback rejection or breakdown/retest

### SIDEWAYS / NO-TRADE conditions

Return **NO TRADE** when one or more important conditions indicate poor quality, including:

- Price repeatedly crossing VWAP with no sustained direction
- EMA 9 and EMA 21 tangled, flat, or frequently crossing
- Weak or conflicting market structure
- Low or compressed volatility that makes the target unrealistic
- Breakout without sufficient confirmation
- Excessive spread, poor liquidity, stale data, or missing required indicators
- Risk limits or daily loss limits reached

Use explicit, configurable thresholds and avoid relying on a single indicator. Do not force a directional classification when the evidence is ambiguous.

---

## 3. Entry Logic

Only enter in the direction of a confirmed trend.

Valid setup types:

1. **Trend pullback:** Price pulls back toward VWAP, EMA 9/21, or a defined trend level, then shows a confirmed rejection in the trend direction.
2. **Breakout/retest:** Price closes beyond a relevant range or key level, confirmation is present, and a retest holds.
3. **Continuation:** A trend resumes after a brief consolidation and a confirmed directional candle.

Do not chase an extended candle. Require a completed candle or another explicit confirmation rule. Prevent duplicate entries on repeated evaluations of the same candle and respect the existing cooldown and conflict-management logic.

If trend direction is clear but no valid entry trigger exists, return **NO TRADE**.

---

## 4. Signal Output

Produce one consolidated decision per underlying per valid evaluation. The signal should be compatible with the existing `SignalEvent` and `SignalBus` interfaces.

Required fields in the dashboard/log output:

- Underlying: NIFTY or BANKNIFTY
- Market trend: UPTREND / DOWNTREND / SIDEWAYS
- Trend strength: STRONG / MODERATE / WEAK
- Decision: BUY CE / BUY PE / NO TRADE
- Signal timestamp
- Spot price at signal time
- Entry trigger and entry zone
- Selected option contract/strike, if supported by the existing option selector
- Option-premium entry reference, when available
- Initial stop-loss
- Target 1 and Target 2, when appropriate
- Trailing stop-loss rule and current trailing stop, once a position is open
- Risk/reward estimate
- Confirmation/invalidation conditions
- Human-readable reason for the decision

Do not fabricate a live option price, strike, spread, or fill. If the application cannot obtain reliable option-chain or premium data, report that limitation and do not pretend the option trade is executable.

---

## 5. Stop-Loss, Targets, and Trailing Stop-Loss

### Initial stop-loss

- Define the stop using a logical underlying swing level or volatility measure, such as ATR, with a configurable buffer.
- Convert this into an option-premium stop only when the application has the necessary option data and a documented conversion/position-management rule.
- Never confuse underlying index points with option-premium points.
- Ensure stop and target values refer explicitly to the instrument they measure.

### Target

- Aim for **20–30 points only when appropriate** for the selected instrument and setup.
- Explicitly configure whether this target is measured in the **underlying index** or the **option premium**.
- Do not silently mix the two units.
- If the target is unrealistic relative to volatility, nearby support/resistance, spread, or expected costs, skip the trade or use the configured risk-based target rules.
- Prefer a configurable minimum reward-to-risk ratio, with 1:2 as the initial backtesting hypothesis rather than a guaranteed outcome.

### Trailing stop-loss

Use a rule-based trailing stop-loss:

- For BUY CE, trail below confirmed higher lows or an appropriate ATR-based level.
- For BUY PE, trail above confirmed lower highs or an appropriate ATR-based level.
- Consider activating the trail only after a configurable favorable move or milestone.
- Update only when the new stop improves protection.
- **Never widen the stop-loss** after entry.
- Avoid a trail so tight that normal market noise routinely exits the position.
- Exit when the stop is hit, the trend structure is invalidated, or an existing risk-control rule requires an exit.

Make the trailing stop stateful and integrate it with the existing paper/live position manager. Do not implement a trailing-stop calculation that is displayed but never applied to positions.

---

## 6. Risk Management

Integrate with existing controls rather than bypassing them.

- Calculate quantity from the configured account-risk budget and actual stop distance, subject to lot-size and broker constraints.
- Enforce configurable maximum risk per trade, daily loss limit, and maximum concurrent positions.
- Include brokerage, taxes/fees where applicable, bid-ask spread, slippage, and liquidity checks in testing and performance reports.
- Never increase quantity to recover losses.
- Respect expiry-day rules and existing volatility/VIX/ATR gates, after reviewing how they currently interact.
- Do not allow conflicting signals to create simultaneous opposing positions for the same underlying unless the existing strategy explicitly supports that behavior.
- Ensure an unavailable or stale data feed produces NO TRADE rather than a guessed signal.

---

## 7. Suggested Decision Flow

1. Validate candle and indicator data.
2. Apply existing market-hours, data-freshness, volatility, and risk gates.
3. Classify market regime: UPTREND / DOWNTREND / SIDEWAYS.
4. Evaluate entry trigger and confirmation.
5. Check option liquidity/contract availability if required for execution.
6. Resolve signal conflicts and cooldowns.
7. Emit one consolidated BUY CE / BUY PE / NO TRADE decision.
8. If a position is opened, manage initial stop, targets, and trailing stop.
9. Log the decision, evidence, rejection reason, and resulting trade outcome.

Return explicit rejection reasons for NO TRADE, such as `SIDEWAYS_MARKET`, `NO_ENTRY_CONFIRMATION`, `CONFLICTING_SIGNALS`, `LOW_VOLATILITY`, `STALE_DATA`, `ILLIQUID_OPTION`, or `RISK_LIMIT_REACHED`.

---

## 8. Required Output Format

Use a consistent structured format, for example:

```text
Underlying: NIFTY
Market Trend: UPTREND
Trend Strength: MODERATE
Trade Signal: BUY CE
Entry Trigger: Pullback rejection above VWAP
Spot Price: [actual observed value]
Option Contract: [actual selected contract, if available]
Option Entry Premium: [actual observed value, if available]
Stop-Loss: [value + instrument/unit]
Target 1: [value + instrument/unit]
Target 2: [value + instrument/unit, if justified]
Trailing SL: Activate after [configured condition]; trail below confirmed higher lows / ATR level
Risk-to-Reward: [calculated value]
Confirmation: [required condition]
Invalidation: [condition that cancels the setup]
Reason: [evidence-based explanation]
```

For no-trade decisions, report the trend/regime, `NO TRADE`, and a clear reason. Do not invent prices or fill unavailable fields with misleading defaults.

---

## 9. Testing and Acceptance Criteria

Implement the system only after inspecting the actual interfaces. Add unit tests and run the project's existing test suite where available.

At minimum, test:

- Clear bullish trend produces BUY CE only after entry confirmation.
- Clear bearish trend produces BUY PE only after entry confirmation.
- Sideways/choppy market produces NO TRADE.
- Conflicting indicators produce NO TRADE.
- Missing, stale, or malformed data produces NO TRADE safely.
- Repeated evaluation of the same candle does not create duplicate signals.
- Cooldown and existing signal-conflict logic remain effective.
- Stop-loss is never widened by trailing logic.
- Trailing stop updates correctly for CE and PE positions.
- Targets and stops clearly identify whether they use index points or option-premium points.
- Risk sizing obeys configured per-trade and daily loss limits.
- Both NIFTY and BANKNIFTY work independently.
- Paper trading and dashboard outputs remain compatible with existing interfaces.

Backtest using historical data with realistic transaction costs and out-of-sample periods. Report trade count, win rate, average win/loss, expectancy after costs, maximum drawdown, profit factor, and results split by instrument and market regime. Do not claim profitability unless the results support it.

---

## 10. Implementation Constraints

- Prefer a small, maintainable unified decision engine over a large collection of overlapping independent triggers.
- Reuse existing indicator calculations and shared data structures.
- Keep thresholds configurable; avoid unexplained hard-coded values.
- Preserve existing application behavior outside the intended strategy integration.
- Do not remove existing safeguards to increase trade frequency.
- Do not claim that 20–30 points or any profit is guaranteed.
- Do not place live orders without the application's explicit execution configuration and user authorization.
- Provide a concise summary of files changed, design decisions, tests run, test results, and any assumptions or unresolved integration points.

## Final Goal

Build one disciplined trend-following system for NIFTY and BANKNIFTY that classifies the market, selects **BUY CE / BUY PE / NO TRADE**, waits for a confirmed entry, manages risk with a logical initial stop and a real trailing stop-loss, and targets 20–30 points only when the setup supports it. Prioritize capital protection, reliable execution, transparent reasons, and measurable backtest results over trade frequency or promised profits.
