# Market Conditions Analysis Report

Generated: 2026-10-05 14:58:25

================================================================================

## 0. Database Content Check

- **Total Positions:** 0
- **Closed Positions:** 0
- **Total Signals:** 0
- **Regime States:** 0


⚠️ **NO CLOSED TRADES FOUND** - Cannot analyze performance

**The system may still be running in study phase or no trades have completed yet.**

## 1. Trade Performance by Time of Day

⚠️ **No closed trades found**

## 2. VIX Analysis

⚠️ **VIX data is not currently being stored with trades**

**Recommendation:** Add VIX value to paper_positions table at entry time

## 3. Trade Performance by Regime Classification

⚠️ **No regime data found in closed trades**

## 4. Strategy Performance by Regime

⚠️ **No strategy data found**

## 5. Trading Frequency Analysis

⚠️ **No trading history found**

## 6. Regime Distribution at Trade Entry

⚠️ **No regime data available**

## 7. Exit Reason Analysis

⚠️ **No exit data found**

## 8. Overall Performance Summary

- **Total Trades:** 0
- **Winning Trades:** None
- **Losing Trades:** None
- **Win Rate:** None%
- **Total P&L:** ₹0.00
- **Avg P&L:** ₹0.00
- **Avg Win:** ₹0.00
- **Avg Loss:** ₹0.00
- **Best Win:** ₹0.00
- **Worst Loss:** ₹0.00

## 9. Regime System Configuration Check

### Active Regime Configurations:

**None:**
- ADX Trend Threshold: 22.00
- ADX Range Threshold: 18.00
- ATR Low Percentile: 30.00
- ATR High Percentile: 70.00

## 10. Signals Skipped Due to Regime

⚠️ **No skipped signals found**


================================================================================

## 🚨 CRITICAL FINDINGS 🚨

### **ROOT CAUSE: NO TRADES ARE EXECUTING**

The database contains:
- **0 positions** (no trades opened)
- **0 signals** (no strategy signals fired)
- **0 regime states** (no regime detection running)

This means the trading system is either:
1. **Not running at all** (backend server down)
2. **Market data feed is not working** (no ticks arriving)
3. **Strategies are not generating signals** (conditions never met)
4. **Regime detection is blocking everything** (all trades suppressed)

---

## Code Analysis Findings

### ✅ Paper Engine (`paper_engine.py`)

The paper trading engine has **comprehensive risk management** in place:

1. **Regime-based filtering**
   - Checks `should_allow_signal()` before opening any position
   - Applies position size multiplier based on regime
   - Blocks trades during REGIME_TRANSITION cooldown (3 candles)

2. **Option momentum check**
   - Skips trades if option LTP falling > 2% in current candle
   - Prevents buying into weakness

3. **ATR vs SL distance validation**
   - Ensures spot ATR is sufficient to reach option target
   - Minimum ATR requirements: BankNifty 25pts, Nifty 7.5pts

4. **Multiple exit mechanisms**
   - Three-tier trailing SL (10%, 20%, 35% profit levels)
   - Max hold time (25min mean-reversion, 45min trend strategies)
   - Partial exit at +40% (closes 50% of position)
   - EOD square-off at 15:15 IST

---

### ⚠️ Regime Rules (`regime_rules.py`)

**8 out of 11 regimes block all trades** with `position_multiplier = 0.0`:

| Regime | Position Multiplier | Strategies Enabled |
|--------|---------------------|-------------------|
| **BULL_TREND_NORMAL_VOL** | 1.0 | VWAP_PULLBACK (BUY only) |
| **BEAR_TREND_NORMAL_VOL** | 1.0 | VWAP_PULLBACK (SELL only) |
| **TREND_HIGH_VOL** | 0.5 | VWAP_PULLBACK |
| **BREAKOUT_EVENT_BULLISH** | 1.0 (0.5 if ATR>70) | ORB_VWAP (BUY only) |
| **BREAKOUT_EVENT_BEARISH** | 1.0 (0.5 if ATR>70) | ORB_VWAP (SELL only) |
| **LOW_VOL_COMPRESSION** | 1.0 | BOLLINGER_BREAKOUT |
| **RANGE_NORMAL_VOL** | 1.0 | VWAP_REVERSAL |
| **HIGH_VOL_CHOP** | 🚫 0.0 | None |
| **REGIME_TRANSITION** | 🚫 0.0 | None |
| **EXPIRY_DAY** | 🚫 0.0 | None |
| **UNKNOWN** | 🚫 0.0 | None |

**Only 3 strategies are ever enabled:**
- **VWAP_PULLBACK** (trend regimes)
- **ORB_VWAP** (breakout regimes)
- **BOLLINGER_BREAKOUT** (compression regime)
- **VWAP_REVERSAL** (range regime)

**8 strategies are NEVER enabled:**
- EMA_VWAP
- SUPERTREND_VWAP
- PDH_PDL
- RSI_VWAP
- OI_SPIKE
- IV_CRUSH
- PCR_SIGNAL
- GAP_FILL

---

### 🔍 Strategy Orchestrator Analysis

**Transition cooldown logic:**
- When regime = `REGIME_TRANSITION`, all strategies blocked for 3 candles
- If regime classifier is too sensitive, this cooldown runs continuously
- Result: **zero trades ever execute**

**Regime classification priority:**
1. EXPIRY_DAY (blocks all trades)
2. *REGIME_TRANSITION handled externally*
3. HIGH_VOL_CHOP (blocks all trades)
4. BREAKOUT_EVENT_BULLISH
5. BREAKOUT_EVENT_BEARISH
6. BULL_TREND_NORMAL_VOL
7. BEAR_TREND_NORMAL_VOL
8. TREND_HIGH_VOL
9. LOW_VOL_COMPRESSION
10. RANGE_NORMAL_VOL
11. UNKNOWN (blocks all trades)

If conditions don't match any regime → falls to UNKNOWN → **all trades blocked**.

---

## 🎯 Recommended Actions

### Immediate Diagnostic Steps

1. **Check if backend server is running**
   ```bash
   # Check process
   Get-Process python
   
   # Check backend logs
   tail -f try12\backend\logs\app.log
   ```

2. **Verify market data feed is working**
   ```sql
   -- Check if ticks are being stored in Redis
   -- Run from backend container/terminal
   redis-cli
   > KEYS tick:*
   > GET tick:26000  # Nifty spot token
   ```

3. **Check regime detection status**
   ```sql
   SELECT * FROM regime_states 
   ORDER BY timestamp DESC 
   LIMIT 10;
   ```
   
   If empty → regime detector not running or not persisting data

4. **Check strategy signals table**
   ```sql
   SELECT 
       strategy_name,
       status,
       skip_reason,
       COUNT(*) as count
   FROM strategy_signals
   WHERE signal_time > CURRENT_DATE
   GROUP BY strategy_name, status, skip_reason
   ORDER BY count DESC;
   ```
   
   If empty → strategies not firing at all

5. **Check trading config**
   ```sql
   SELECT 
       mode,
       study_phase,
       max_daily_loss,
       capital,
       used_capital,
       daily_trades_count,
       max_trades_per_day,
       strategy_flags
   FROM trading_config;
   ```

### Configuration Changes to Enable Trading

If the system is running but not trading due to regime filters:

**Option A: Temporarily disable regime filtering** (for testing)
```python
# In paper_engine.py handle_signal() method
# Comment out regime filter (line ~120):
# allowed, regime_reason = strategy_orchestrator.should_allow_signal(signal, regime_state)
# if not allowed:
#     ...

# Force allow all signals temporarily
allowed = True
regime_reason = "Regime check disabled for testing"
```

**Option B: Make regime rules less restrictive**

Edit `regime_rules.py`:
```python
# Add more strategies to each regime
"BULL_TREND_NORMAL_VOL": RegimeRule(
    enabled_strategies=frozenset({
        "VWAP_PULLBACK", 
        "EMA_VWAP",          # ADD
        "SUPERTREND_VWAP",   # ADD
        "ORB_VWAP"           # ADD
    }),
    allowed_directions=frozenset({"BUY"}),
    position_multiplier=1.0,
    notes="Confirmed uptrend with normal volatility",
),

# Enable trades in HIGH_VOL_CHOP (reduced size)
"HIGH_VOL_CHOP": RegimeRule(
    enabled_strategies=frozenset({"VWAP_PULLBACK"}),  # ADD STRATEGIES
    allowed_directions=frozenset({"BUY", "SELL"}),    # ADD DIRECTIONS
    position_multiplier=0.5,  # CHANGE FROM 0.0 → 0.5
    notes="High volatility — reduced position size trades allowed",
),

# Enable trades in UNKNOWN regime (testing mode)
"UNKNOWN": RegimeRule(
    enabled_strategies=frozenset({
        "VWAP_PULLBACK", "ORB_VWAP", "EMA_VWAP"
    }),
    allowed_directions=frozenset({"BUY", "SELL"}),
    position_multiplier=0.5,  # CHANGE FROM 0.0
    notes="Unknown regime — conservative trading allowed",
),
```

**Option C: Add fallback logic in strategy_orchestrator**

If no regime data exists, allow trading with reduced size:
```python
# In strategy_orchestrator.py should_allow_signal()
if regime_state is None:
    # Current: passes through
    # Better: allow with warning
    logger.warning(
        "[StrategyOrchestrator] No regime data for %s — "
        "allowing signal with 0.5x position size",
        signal.underlying
    )
    return True, "No regime data - proceeding with caution"
```

---

## Why All Trades Are Losing (IF trades were executing)

Based on the code analysis, if trades WERE executing and losing, the causes would be:

1. **Over-filtering creates adverse selection**
   - Option momentum check skips 2%+ falling options
   - ATR validation requires high volatility
   - Result: only trades in extreme conditions (highest risk)

2. **Entry slippage accumulates losses**
   - Slippage = 0.2% of LTP added to entry
   - Brokerage = ₹20 per lot round-trip
   - On a ₹100 option: pay ₹100.20 + ₹20 brokerage
   - Must rise > 0.4% just to break even

3. **SL too tight in volatile markets**
   - SL set from signal, but option Greeks amplify spot moves
   - Spot moves 10pts → option moves 20-30pts (depending on delta)
   - Tight SL gets hit before target

4. **Max hold time exits premature winners**
   - Mean-reversion: 25min limit
   - Trend: 45min limit
   - Intraday moves often take 60-90min to develop

---

## Summary

**Current Status:** System has executed ZERO trades (0 positions, 0 signals, 0 regime states)

**Most Likely Cause:** 
1. Backend server not running, OR
2. Market data feed not connected, OR
3. Regime detector not running/persisting data, OR
4. Regime classification always returning UNKNOWN or HIGH_VOL_CHOP (both block all trades)

**Recommended Next Step:**
1. Verify backend is running and check logs
2. Run diagnostic SQL queries above
3. If regime = UNKNOWN or HIGH_VOL_CHOP constantly, modify `regime_rules.py` to allow trades
4. Monitor `strategy_signals` table to see if signals are being generated but skipped

