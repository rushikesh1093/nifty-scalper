# Requirements Document

## Introduction

This document specifies requirements for an automated regime detection and strategy orchestration system for intraday options trading. The system detects market conditions in real-time using technical indicators (ADX, ATR, VWAP, volume) and automatically selects appropriate trading strategies, adjusts position sizing, and controls trading status based on predefined regime rules. The goal is to maximize profitability by ensuring only the most suitable strategies execute under favorable market conditions while preventing trades during unfavorable regimes (high-volatility chop, regime transitions, expiry days).

The system integrates with 12 existing scalping strategies and operates on 1-minute candle data for NIFTY and BANKNIFTY index options during market hours (9:15-15:15 IST).

## Glossary

- **Regime**: A distinct market state characterized by specific combinations of technical indicators (trend, volatility, price-VWAP relationship) that determines which trading strategies should be active
- **Regime_Detector**: The component that analyzes market data and classifies the current market state into one of 9 defined regimes
- **Strategy_Orchestrator**: The component that manages strategy enable/disable states, position sizing, and execution based on the detected regime
- **ADX** (Average Directional Index): Trend strength indicator where values >22 indicate trending conditions and <18 indicate ranging conditions
- **ATR** (Average True Range): Volatility measure expressed as absolute points on the underlying instrument
- **ATR_Percentile**: The current ATR value expressed as a percentile of the 20-period ATR distribution (0-100 scale), used to classify volatility as low (<30), normal (30-70), or high (>70)
- **VWAP** (Volume-Weighted Average Price): Intraday benchmark price level that resets daily at market open
- **VWAP_Cross_Count**: Number of times price crosses VWAP within a rolling window (default 20 candles), used to detect choppy range-bound conditions
- **Regime_Flip**: A change from one regime to another within a configurable time window (default 2 candles)
- **Position_Size_Multiplier**: A scaling factor (0.0, 0.5, or 1.0) applied to the base position size based on regime risk assessment
- **Underlying**: The base instrument being traded (NIFTY or BANKNIFTY spot index)
- **Signal_Aggregator**: Existing component that prevents duplicate signals from multiple strategies for the same underlying and direction
- **Strategy_Runner**: Existing component that feeds candle data to strategies and manages the strategy execution loop
- **Paper_Engine**: Existing component that handles position opening, monitoring, and closing for paper trading
- **ORB** (Opening Range Breakout): The high and low range established during the first 15 minutes of trading (9:15-9:30 IST)
- **Expiry_Day**: The calendar day when weekly options contracts expire (typically Thursday for NIFTY, Wednesday for BANKNIFTY)

## Requirements

### Requirement 1: Regime Detection Engine

**User Story:** As a trading system, I want to continuously detect the current market regime based on technical indicators, so that I can dynamically adjust strategy selection and risk parameters.

#### Acceptance Criteria

1. WHEN new 1-minute candle data is available, THE Regime_Detector SHALL compute ADX, ATR, ATR_Percentile, VWAP, price-VWAP relationship, and VWAP_Cross_Count
2. WHEN all required indicators are computed, THE Regime_Detector SHALL classify the current state into exactly one of 9 defined regimes
3. THE Regime_Detector SHALL store the current regime classification with timestamp for regime transition detection
4. WHEN computing ATR_Percentile, THE Regime_Detector SHALL use a rolling 20-period window of ATR values and calculate the percentile rank of the current ATR
5. WHEN computing VWAP_Cross_Count, THE Regime_Detector SHALL count the number of candles in the last 20 periods where close crossed VWAP from the previous candle
6. THE Regime_Detector SHALL operate independently for each underlying (NIFTY and BANKNIFTY)
7. WHEN indicator computation fails for any reason, THE Regime_Detector SHALL return regime classification "UNKNOWN" and log the error

### Requirement 2: Bull Trend Normal Volatility Regime

**User Story:** As a trading system, I want to detect bull trend conditions with normal volatility, so that I can activate VWAP Pullback strategy for CE (Call) trades with full position sizing.

#### Acceptance Criteria

1. WHEN ADX is greater than 22 AND price is greater than VWAP AND ATR_Percentile is between 30 and 70 inclusive, THE Regime_Detector SHALL classify the regime as BULL_TREND_NORMAL_VOL
2. WHEN regime is BULL_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL enable the VWAP_PULLBACK strategy
3. WHEN regime is BULL_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 1.0
4. WHEN regime is BULL_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL restrict VWAP_PULLBACK signals to Direction BUY only (CE options)
5. WHEN regime is BULL_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 3: Bear Trend Normal Volatility Regime

**User Story:** As a trading system, I want to detect bear trend conditions with normal volatility, so that I can activate VWAP Pullback strategy for PE (Put) trades with full position sizing.

#### Acceptance Criteria

1. WHEN ADX is greater than 22 AND price is less than VWAP AND ATR_Percentile is between 30 and 70 inclusive, THE Regime_Detector SHALL classify the regime as BEAR_TREND_NORMAL_VOL
2. WHEN regime is BEAR_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL enable the VWAP_PULLBACK strategy
3. WHEN regime is BEAR_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 1.0
4. WHEN regime is BEAR_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL restrict VWAP_PULLBACK signals to Direction SELL only (PE options)
5. WHEN regime is BEAR_TREND_NORMAL_VOL, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 4: Trend High Volatility Regime

**User Story:** As a trading system, I want to detect trending conditions with elevated volatility, so that I can activate VWAP Pullback strategy with reduced position sizing to manage increased risk.

#### Acceptance Criteria

1. WHEN ADX is greater than 22 AND ATR_Percentile is greater than 70, THE Regime_Detector SHALL classify the regime as TREND_HIGH_VOL
2. WHEN regime is TREND_HIGH_VOL, THE Strategy_Orchestrator SHALL enable the VWAP_PULLBACK strategy
3. WHEN regime is TREND_HIGH_VOL, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 0.5
4. WHEN regime is TREND_HIGH_VOL AND price is greater than VWAP, THE Strategy_Orchestrator SHALL restrict VWAP_PULLBACK signals to Direction BUY
5. WHEN regime is TREND_HIGH_VOL AND price is less than VWAP, THE Strategy_Orchestrator SHALL restrict VWAP_PULLBACK signals to Direction SELL
6. WHEN regime is TREND_HIGH_VOL, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 5: Breakout Event Regime

**User Story:** As a trading system, I want to detect breakout conditions (opening range or previous day level breaks with volume confirmation), so that I can activate ORB+VWAP strategy with appropriate position sizing.

#### Acceptance Criteria

1. WHEN price breaks above ORB high OR price breaks above previous day high, AND volume is greater than or equal to 1.5 times average volume, AND price is greater than VWAP, THE Regime_Detector SHALL classify the regime as BREAKOUT_EVENT_BULLISH
2. WHEN price breaks below ORB low OR price breaks below previous day low, AND volume is greater than or equal to 1.5 times average volume, AND price is less than VWAP, THE Regime_Detector SHALL classify the regime as BREAKOUT_EVENT_BEARISH
3. WHEN regime is BREAKOUT_EVENT_BULLISH OR BREAKOUT_EVENT_BEARISH, THE Strategy_Orchestrator SHALL enable the ORB_VWAP strategy
4. WHEN regime is BREAKOUT_EVENT_BULLISH OR BREAKOUT_EVENT_BEARISH, AND ATR_Percentile is less than or equal to 70, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 1.0
5. WHEN regime is BREAKOUT_EVENT_BULLISH OR BREAKOUT_EVENT_BEARISH, AND ATR_Percentile is greater than 70, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 0.5
6. WHEN regime is BREAKOUT_EVENT_BULLISH, THE Strategy_Orchestrator SHALL restrict ORB_VWAP signals to Direction BUY
7. WHEN regime is BREAKOUT_EVENT_BEARISH, THE Strategy_Orchestrator SHALL restrict ORB_VWAP signals to Direction SELL
8. WHEN regime is BREAKOUT_EVENT_BULLISH OR BREAKOUT_EVENT_BEARISH, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 6: Low Volatility Compression Regime

**User Story:** As a trading system, I want to detect low-volatility compression conditions where price hugs VWAP, so that I can activate Bollinger Breakout strategy to capture imminent expansion moves.

#### Acceptance Criteria

1. WHEN ATR_Percentile is less than 30 AND the absolute difference between price and VWAP is less than 0.2 percent of VWAP, THE Regime_Detector SHALL classify the regime as LOW_VOL_COMPRESSION
2. WHEN regime is LOW_VOL_COMPRESSION, THE Strategy_Orchestrator SHALL enable the BOLLINGER_BREAKOUT strategy
3. WHEN regime is LOW_VOL_COMPRESSION, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 1.0
4. WHEN regime is LOW_VOL_COMPRESSION, THE Strategy_Orchestrator SHALL allow BOLLINGER_BREAKOUT signals in both Direction BUY and Direction SELL
5. WHEN regime is LOW_VOL_COMPRESSION, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 7: Range Normal Volatility Regime

**User Story:** As a trading system, I want to detect ranging market conditions with normal volatility, so that I can activate VWAP Reversal strategy or disable trading based on performance metrics.

#### Acceptance Criteria

1. WHEN ADX is less than 18 AND ATR_Percentile is between 30 and 70 inclusive, THE Regime_Detector SHALL classify the regime as RANGE_NORMAL_VOL
2. WHEN regime is RANGE_NORMAL_VOL, THE Strategy_Orchestrator SHALL enable the VWAP_REVERSAL strategy
3. WHEN regime is RANGE_NORMAL_VOL, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 1.0
4. WHEN regime is RANGE_NORMAL_VOL, THE Strategy_Orchestrator SHALL allow VWAP_REVERSAL signals in both Direction BUY and Direction SELL
5. WHEN regime is RANGE_NORMAL_VOL, THE Strategy_Orchestrator SHALL configure option selection to target ATM strikes

### Requirement 8: High Volatility Chop Regime

**User Story:** As a trading system, I want to detect high-volatility choppy conditions with frequent VWAP crosses, so that I can disable all trading to avoid whipsaws and false signals.

#### Acceptance Criteria

1. WHEN ATR_Percentile is greater than 70 AND VWAP_Cross_Count is greater than or equal to 8 in the last 20 candles, THE Regime_Detector SHALL classify the regime as HIGH_VOL_CHOP
2. WHEN regime is HIGH_VOL_CHOP, THE Strategy_Orchestrator SHALL disable all strategies
3. WHEN regime is HIGH_VOL_CHOP, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 0.0
4. WHEN regime is HIGH_VOL_CHOP, THE Strategy_Orchestrator SHALL prevent any new position openings
5. WHEN regime is HIGH_VOL_CHOP AND open positions exist, THE Strategy_Orchestrator SHALL allow existing positions to reach their stop-loss or target levels normally

### Requirement 9: Regime Transition Detection

**User Story:** As a trading system, I want to detect when the regime changes rapidly, so that I can pause trading during unstable transition periods to avoid entering positions in conflicting market states.

#### Acceptance Criteria

1. WHEN the current regime differs from the regime of 2 candles ago, THE Regime_Detector SHALL classify the regime as REGIME_TRANSITION
2. WHEN regime is REGIME_TRANSITION, THE Strategy_Orchestrator SHALL disable all strategies
3. WHEN regime is REGIME_TRANSITION, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 0.0
4. WHEN regime is REGIME_TRANSITION, THE Strategy_Orchestrator SHALL prevent any new position openings
5. WHEN a regime has been stable for at least 3 consecutive candles, THE Regime_Detector SHALL clear the REGIME_TRANSITION flag and classify according to current indicators
6. WHEN regime is REGIME_TRANSITION AND open positions exist, THE Strategy_Orchestrator SHALL allow existing positions to reach their stop-loss or target levels normally

### Requirement 10: Expiry Day Regime

**User Story:** As a trading system, I want to detect expiry days for NIFTY and BANKNIFTY options, so that I can disable all regime-based strategies to avoid erratic price action and elevated gamma risk.

#### Acceptance Criteria

1. WHEN the current calendar date matches a NIFTY weekly expiry date (typically Thursday), THE Regime_Detector SHALL classify the NIFTY regime as EXPIRY_DAY
2. WHEN the current calendar date matches a BANKNIFTY weekly expiry date (typically Wednesday), THE Regime_Detector SHALL classify the BANKNIFTY regime as EXPIRY_DAY
3. WHEN regime is EXPIRY_DAY, THE Strategy_Orchestrator SHALL disable all strategies
4. WHEN regime is EXPIRY_DAY, THE Strategy_Orchestrator SHALL set Position_Size_Multiplier to 0.0
5. WHEN regime is EXPIRY_DAY, THE Strategy_Orchestrator SHALL prevent any new position openings for the entire trading session
6. WHEN regime is EXPIRY_DAY AND open positions exist, THE Strategy_Orchestrator SHALL allow existing positions to reach their stop-loss or target levels normally

### Requirement 11: Strategy Orchestrator Integration

**User Story:** As a trading system, I want to integrate the regime-based strategy orchestrator with the existing strategy runner, so that strategy enable/disable states and position sizing are controlled automatically without manual intervention.

#### Acceptance Criteria

1. WHEN Strategy_Runner begins a candle evaluation cycle, THE Strategy_Orchestrator SHALL query Regime_Detector for the current regime before invoking any strategy
2. WHEN Strategy_Orchestrator receives regime classification, THE Strategy_Orchestrator SHALL update strategy enable flags in the strategy_flags configuration
3. WHEN a strategy is disabled by regime rules, THE Strategy_Orchestrator SHALL set the strategy enabled flag to False in strategy_flags
4. WHEN a strategy is enabled by regime rules, THE Strategy_Orchestrator SHALL set the strategy enabled flag to True in strategy_flags
5. WHEN Strategy_Runner evaluates a strategy, THE Strategy_Runner SHALL respect the strategy enabled flag set by Strategy_Orchestrator
6. THE Strategy_Orchestrator SHALL log all regime changes and strategy state transitions with timestamps for audit purposes
7. WHEN Strategy_Orchestrator modifies strategy states, THE Strategy_Orchestrator SHALL complete all updates before Strategy_Runner proceeds with signal generation

### Requirement 12: Position Sizing Integration

**User Story:** As a trading system, I want to apply regime-based position size multipliers to new positions, so that risk exposure is automatically adjusted based on market conditions.

#### Acceptance Criteria

1. WHEN Paper_Engine receives a signal from an enabled strategy, THE Paper_Engine SHALL query Strategy_Orchestrator for the current Position_Size_Multiplier
2. WHEN Position_Size_Multiplier is 0.0, THE Paper_Engine SHALL reject the signal and not open a position
3. WHEN Position_Size_Multiplier is 0.5, THE Paper_Engine SHALL calculate lot size as base_lot_size multiplied by 0.5 rounded to the nearest integer minimum 1
4. WHEN Position_Size_Multiplier is 1.0, THE Paper_Engine SHALL calculate lot size as base_lot_size
5. THE Paper_Engine SHALL log the applied Position_Size_Multiplier and calculated lot size for each position opened
6. WHEN Position_Size_Multiplier changes after a position is opened, THE Paper_Engine SHALL not modify the lot size of existing open positions

### Requirement 13: Regime Persistence and State Management

**User Story:** As a trading system, I want to persist regime state across system restarts, so that regime detection resumes correctly without misclassifying the initial candles of a new session.

#### Acceptance Criteria

1. WHEN Regime_Detector classifies a new regime, THE Regime_Detector SHALL store the regime classification, timestamp, and supporting indicator values in Redis cache
2. THE Regime_Detector SHALL use Redis key pattern "regime:{underlying}:current" for regime state storage
3. WHEN Strategy_Runner restarts, THE Regime_Detector SHALL retrieve the last known regime from Redis cache
4. WHEN retrieved regime timestamp is older than 10 minutes, THE Regime_Detector SHALL discard the cached regime and wait for fresh indicator data
5. WHEN Regime_Detector initializes and no cached regime exists, THE Regime_Detector SHALL classify regime as UNKNOWN until at least 25 candles of data are available
6. THE Regime_Detector SHALL store regime history for the last 5 regime changes to support regime transition detection

### Requirement 14: Regime Indicator Computation

**User Story:** As a trading system, I want to compute ADX indicator for trend strength measurement, so that regime detection can distinguish between trending and ranging markets.

#### Acceptance Criteria

1. THE Regime_Detector SHALL compute ADX using a 14-period window on 1-minute candle data
2. WHEN computing ADX, THE Regime_Detector SHALL calculate positive directional movement (+DM), negative directional movement (-DM), and true range (TR) for each candle
3. WHEN computing ADX, THE Regime_Detector SHALL smooth +DM, -DM, and TR using 14-period exponential moving averages
4. WHEN computing ADX, THE Regime_Detector SHALL calculate directional indicators +DI and -DI from smoothed +DM, -DM, and TR
5. WHEN computing ADX, THE Regime_Detector SHALL calculate DX as the absolute difference between +DI and -DI divided by the sum of +DI and -DI, multiplied by 100
6. WHEN computing ADX, THE Regime_Detector SHALL apply a 14-period exponential moving average to DX to produce the final ADX value
7. WHEN fewer than 28 candles are available, THE Regime_Detector SHALL return ADX as None (ADX requires 2x period for smoothing)

### Requirement 15: Regime Monitoring and Logging

**User Story:** As a trading system operator, I want comprehensive logging of regime changes and strategy state transitions, so that I can audit regime-based decisions and diagnose unexpected behavior.

#### Acceptance Criteria

1. WHEN regime changes from one state to another, THE Regime_Detector SHALL log the previous regime, new regime, timestamp, and all indicator values used in classification
2. WHEN Strategy_Orchestrator enables a strategy, THE Strategy_Orchestrator SHALL log the strategy name, regime, underlying, and timestamp
3. WHEN Strategy_Orchestrator disables a strategy, THE Strategy_Orchestrator SHALL log the strategy name, regime, underlying, reason, and timestamp
4. WHEN Position_Size_Multiplier changes, THE Strategy_Orchestrator SHALL log the old multiplier, new multiplier, regime, and timestamp
5. WHEN Paper_Engine rejects a signal due to Position_Size_Multiplier equal to 0.0, THE Paper_Engine SHALL log the rejection with strategy name, underlying, direction, and regime
6. THE Regime_Detector SHALL log regime classification every 5 minutes during market hours even when regime is unchanged, including current indicator values
7. WHEN regime classification fails due to insufficient data or errors, THE Regime_Detector SHALL log the error with context including available candle count and failed indicator names

### Requirement 16: Regime-Based Strategy Filtering

**User Story:** As a trading system, I want to filter strategy signals based on regime-specific direction constraints, so that only appropriate trade directions are allowed in each regime.

#### Acceptance Criteria

1. WHEN a strategy generates a signal, THE Strategy_Orchestrator SHALL verify the signal direction is permitted by the current regime rules
2. WHEN regime is BULL_TREND_NORMAL_VOL AND signal direction is SELL, THE Strategy_Orchestrator SHALL suppress the signal
3. WHEN regime is BEAR_TREND_NORMAL_VOL AND signal direction is BUY, THE Strategy_Orchestrator SHALL suppress the signal
4. WHEN regime is BREAKOUT_EVENT_BULLISH AND signal direction is SELL, THE Strategy_Orchestrator SHALL suppress the signal
5. WHEN regime is BREAKOUT_EVENT_BEARISH AND signal direction is BUY, THE Strategy_Orchestrator SHALL suppress the signal
6. WHEN a signal is suppressed due to regime direction constraints, THE Strategy_Orchestrator SHALL log the suppression with strategy name, direction, regime, and timestamp
7. WHEN regime allows both BUY and SELL directions, THE Strategy_Orchestrator SHALL pass signals of both directions to Paper_Engine

### Requirement 17: Regime Conflict Resolution

**User Story:** As a trading system, I want to handle edge cases where multiple regime conditions overlap, so that regime classification is deterministic and unambiguous.

#### Acceptance Criteria

1. WHEN multiple regime conditions are satisfied simultaneously, THE Regime_Detector SHALL apply a priority order: EXPIRY_DAY highest, then REGIME_TRANSITION, then HIGH_VOL_CHOP, then specific trend/breakout regimes, then LOW_VOL_COMPRESSION, then RANGE_NORMAL_VOL lowest
2. WHEN EXPIRY_DAY condition is true for the underlying, THE Regime_Detector SHALL classify as EXPIRY_DAY regardless of other indicator values
3. WHEN REGIME_TRANSITION condition is true, THE Regime_Detector SHALL classify as REGIME_TRANSITION regardless of current indicator-based regime
4. WHEN both BREAKOUT_EVENT and TREND_HIGH_VOL conditions are satisfied, THE Regime_Detector SHALL prioritize BREAKOUT_EVENT
5. WHEN both LOW_VOL_COMPRESSION and RANGE_NORMAL_VOL conditions are satisfied, THE Regime_Detector SHALL prioritize LOW_VOL_COMPRESSION
6. WHEN no regime conditions are satisfied, THE Regime_Detector SHALL classify as UNKNOWN and disable all strategies
7. THE Regime_Detector SHALL log when priority rules are applied to resolve overlapping regime conditions

### Requirement 18: Regime Configuration and Tunability

**User Story:** As a trading system operator, I want to configure regime detection thresholds and parameters, so that I can tune regime sensitivity without code changes.

#### Acceptance Criteria

1. THE Regime_Detector SHALL read regime configuration parameters from a configuration file or database on initialization
2. THE Regime_Detector SHALL support configurable thresholds for ADX_TREND_THRESHOLD (default 22), ADX_RANGE_THRESHOLD (default 18), ATR_LOW_PERCENTILE (default 30), ATR_HIGH_PERCENTILE (default 70), VWAP_PROXIMITY_PERCENT (default 0.2), VWAP_CROSS_CHOP_THRESHOLD (default 8), and VOLUME_SURGE_MULTIPLIER (default 1.5)
3. WHEN configuration parameters are updated, THE Regime_Detector SHALL reload parameters within 60 seconds without requiring a system restart
4. THE Regime_Detector SHALL validate configuration parameters on load and use safe defaults if any parameter is invalid or missing
5. THE Regime_Detector SHALL log all configuration parameters at initialization and whenever parameters are reloaded
6. THE Regime_Detector SHALL support per-underlying configuration overrides where NIFTY and BANKNIFTY can have different thresholds

### Requirement 19: Regime-Based Performance Tracking

**User Story:** As a trading system operator, I want to track profit/loss and win rate per regime, so that I can evaluate regime classification effectiveness and adjust rules to maximize profitability.

#### Acceptance Criteria

1. WHEN Paper_Engine closes a position, THE Strategy_Orchestrator SHALL record the regime that was active when the position was opened
2. THE Strategy_Orchestrator SHALL maintain per-regime statistics including total trades, winning trades, losing trades, total profit/loss, and average profit/loss per trade
3. THE Strategy_Orchestrator SHALL store regime performance data in the database with fields: regime_name, underlying, total_trades, winning_trades, losing_trades, total_pnl, avg_pnl, updated_at
4. THE Strategy_Orchestrator SHALL expose an API endpoint that returns regime performance statistics filtered by date range and underlying
5. WHEN regime performance data is requested, THE Strategy_Orchestrator SHALL calculate win_rate as winning_trades divided by total_trades, expressed as a percentage
6. THE Strategy_Orchestrator SHALL update regime performance statistics within 5 seconds of each position close

### Requirement 20: Regime Transition Cooldown

**User Story:** As a trading system, I want to enforce a cooldown period after exiting REGIME_TRANSITION state, so that strategies do not immediately fire on the first candle of a new regime before conditions stabilize.

#### Acceptance Criteria

1. WHEN regime transitions from REGIME_TRANSITION to any other regime, THE Strategy_Orchestrator SHALL start a 3-candle cooldown timer
2. WHILE cooldown timer is active, THE Strategy_Orchestrator SHALL prevent all strategies from generating signals even if they are enabled by regime rules
3. WHEN cooldown timer expires after 3 candles in the new regime, THE Strategy_Orchestrator SHALL allow strategies to generate signals according to regime rules
4. WHEN regime changes during the cooldown period, THE Strategy_Orchestrator SHALL reset the cooldown timer to 3 candles for the newest regime
5. WHEN cooldown is active, THE Strategy_Orchestrator SHALL log each candle cycle with message indicating cooldown status and remaining candles
6. WHEN regime is EXPIRY_DAY or HIGH_VOL_CHOP, THE Strategy_Orchestrator SHALL not apply cooldown logic since no strategies are enabled

