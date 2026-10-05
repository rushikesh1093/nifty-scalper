# Implementation Plan: Regime-Based Strategy Automation

## Overview

Implement a real-time market regime detection and strategy orchestration system that sits between `StrategyRunner` and the 12 existing strategies. The plan is split into four phases: (1) data models and DB schema, (2) `RegimeDetector` with indicator computation, (3) `StrategyOrchestrator` with regime rules, and (4) integration into `StrategyRunner` and `PaperEngine`.

## Tasks

- [x] 1. Create database schema and SQLAlchemy models for regime persistence
  - [x] 1.1 Create Alembic migration for `regime_states`, `regime_performance`, and `regime_config` tables
    - Add `regime_states` table with columns: id, underlying, regime, timestamp, adx, atr, atr_percentile, vwap, price, vwap_cross_count, volume, avg_volume, previous_regime, transition_detected
    - Add `regime_performance` table with columns: id, regime, underlying, strategy_name, total_trades, winning_trades, losing_trades, total_pnl, avg_pnl, win_rate, avg_rr, first_trade_at, last_trade_at, updated_at
    - Add `regime_config` table with columns: id, underlying (nullable), adx_trend_threshold, adx_range_threshold, atr_low_percentile, atr_high_percentile, vwap_proximity_percent, vwap_cross_chop_threshold, volume_surge_multiplier, regime_transition_window, transition_cooldown_candles, is_active, created_at, updated_at; seed one global default row
    - Add `entry_regime VARCHAR(50)` column to `paper_positions` table with an index
    - _Requirements: 13.1, 13.2, 19.3, 12.1_

  - [x] 1.2 Create SQLAlchemy ORM models in `app/db/models/regime.py`
    - Define `RegimeState`, `RegimePerformance`, `RegimeConfig` ORM classes mapped to the tables above
    - Register models in `app/db/models/__init__.py` so Alembic auto-detects them
    - _Requirements: 13.1, 19.3_

- [x] 2. Implement `RegimeConfig` dataclass and configuration loading
  - [x] 2.1 Create `app/services/trading/regime_config.py` with `RegimeConfig` dataclass and loader
    - Define `RegimeConfig` dataclass with all threshold fields and default values (ADX_TREND_THRESHOLD=22.0, ADX_RANGE_THRESHOLD=18.0, ATR_LOW_PERCENTILE=30.0, ATR_HIGH_PERCENTILE=70.0, VWAP_PROXIMITY_PERCENT=0.2, VWAP_CROSS_CHOP_THRESHOLD=8, VOLUME_SURGE_MULTIPLIER=1.5, REGIME_TRANSITION_WINDOW=2, TRANSITION_COOLDOWN_CANDLES=3)
    - Implement `load_config(underlying: str) -> RegimeConfig` that reads from `regime_config` DB table (per-underlying row first, fall back to global default); validates all fields and substitutes safe defaults for invalid/missing values; logs all loaded parameters
    - Implement `reload_config_if_stale(underlying: str, max_age_seconds: int = 60)` to support hot-reload within 60 seconds without restart
    - _Requirements: 18.1, 18.2, 18.3, 18.4, 18.5, 18.6_

- [x] 3. Implement regime indicator computation functions
  - [x] 3.1 Create `app/services/trading/regime_indicators.py` with `RegimeIndicators` dataclass and indicator functions
    - Define `RegimeIndicators` dataclass (adx, atr, atr_percentile, vwap, price, vwap_cross_count, volume, avg_volume, orb_high, orb_low, pdh, pdl)
    - Implement `compute_adx(high, low, close, period=14) -> Optional[float]` using Wilder's smoothing — return `None` when fewer than 28 candles available
    - Implement `compute_atr_percentile(atr_current, candles, period=20) -> float` — return 50.0 when fewer than 34 candles available
    - Implement `compute_vwap_cross_count(candles, vwap, window=20) -> int` — count sign changes of `close - vwap` over the rolling window
    - Implement `build_regime_indicators(candles, indicators, config) -> RegimeIndicators` that assembles all fields reusing already-computed `indicators` dict values (atr, vwap) to avoid redundant work
    - _Requirements: 1.1, 1.4, 1.5, 14.1, 14.2, 14.3, 14.4, 14.5, 14.6, 14.7_

  - [ ]* 3.2 Write unit tests for indicator computation in `tests/test_regime_indicators.py`
    - Test `compute_adx` with a known synthetic dataset (flat, trending, and extreme-volatility inputs) and verify the output is within 5% of a reference value
    - Test `compute_adx` returns `None` when fewer than 28 candles are provided
    - Test `compute_atr_percentile` returns a value in [0, 100] for valid inputs and 50.0 for insufficient data
    - Test `compute_vwap_cross_count` counts zero, one, and multiple crosses correctly including window boundary conditions
    - _Requirements: 14.7, 1.4, 1.5_

- [x] 4. Implement regime classification logic
  - [x] 4.1 Create `app/services/trading/regime_rules.py` with `RegimeRule`, `REGIME_RULES`, and `classify_regime()`
    - Define `RegimeRule` NamedTuple (enabled_strategies, allowed_directions, position_multiplier, notes)
    - Define `REGIME_RULES` dict mapping all 11 regime names to their `RegimeRule` instances (BULL_TREND_NORMAL_VOL, BEAR_TREND_NORMAL_VOL, TREND_HIGH_VOL, BREAKOUT_EVENT_BULLISH, BREAKOUT_EVENT_BEARISH, LOW_VOL_COMPRESSION, RANGE_NORMAL_VOL, HIGH_VOL_CHOP, REGIME_TRANSITION, EXPIRY_DAY, UNKNOWN)
    - Implement `get_regime_rule(regime, atr_percentile) -> RegimeRule` applying the dynamic breakout ATR adjustment (multiplier → 0.5 when ATR_percentile > 70)
    - Implement `classify_regime(indicators, underlying, is_expiry_day, previous_regime, config) -> str` applying the 9-priority order from the design; log when priority resolution is applied
    - _Requirements: 2.1, 3.1, 4.1, 5.1, 5.2, 6.1, 7.1, 8.1, 17.1, 17.2, 17.3, 17.4, 17.5, 17.6, 17.7_

  - [ ]* 4.2 Write unit tests for regime classification in `tests/test_regime_classification.py`
    - Test each of the 9 regime conditions independently with exact-threshold boundary inputs (at, above, below)
    - Test priority resolution: construct indicator sets that satisfy multiple conditions simultaneously and assert the higher-priority regime wins
    - Test EXPIRY_DAY overrides all other conditions
    - Test fallback to UNKNOWN when no conditions are satisfied
    - _Requirements: 17.1–17.7_

- [x] 5. Implement `RegimeDetector`
  - [x] 5.1 Create `app/services/trading/regime_detector.py` with `RegimeState` dataclass and `RegimeDetector` class
    - Define `RegimeState` dataclass (regime, underlying, timestamp, indicators, previous_regime, transition_cooldown_remaining)
    - Implement `RegimeDetector.__init__` loading config and initialising in-memory history dicts
    - Implement `detect_regime(candles, indicators, underlying, is_expiry_day) -> RegimeState` — compute indicators, detect transition, classify regime, log result; catch `InsufficientDataError` and all exceptions and return `_fallback_regime(underlying, "UNKNOWN")`
    - Implement `check_regime_transition(current_regime, regime_history, transition_window) -> bool`
    - Implement `get_current_regime(underlying) -> Optional[RegimeState]` reading from Redis cache first
    - Implement `get_regime_history(underlying, limit=5) -> list[RegimeState]`
    - Log regime at every call and additionally log unchanged regime every 5 minutes per Requirement 15.6
    - _Requirements: 1.1, 1.2, 1.3, 1.6, 1.7, 9.1, 9.5, 15.1, 15.6, 15.7_

  - [ ]* 5.2 Write unit tests for `RegimeDetector` in `tests/test_regime_detector.py`
    - Test `check_regime_transition` returns True when regime changed within window and False when stable
    - Test `detect_regime` returns UNKNOWN and logs when `compute_adx` raises an exception
    - Test cooldown countdown: verify `transition_cooldown_remaining` decrements each cycle and reaches 0 after `TRANSITION_COOLDOWN_CANDLES` calls
    - _Requirements: 1.7, 9.1, 9.5, 20.1_

- [x] 6. Implement `RegimePersistence`
  - [x] 6.1 Create `app/services/trading/regime_persistence.py` with `RegimePersistence` class
    - Implement `save_regime_state(state: RegimeState)` — serialize to JSON and write to Redis key `regime:{underlying}:current` with TTL=600; catch `RedisError` and log without raising
    - Implement `load_regime_state(underlying) -> Optional[RegimeState]` — read and deserialize from Redis; discard if timestamp is older than 10 minutes; return None on any error
    - Implement `save_regime_history(underlying, history: list[RegimeState])` — write last-5 history to Redis key `regime:{underlying}:history` with TTL=28800
    - Implement `load_regime_history(underlying) -> list[RegimeState]` — read from Redis; return empty list on failure
    - Implement `persist_to_db(state: RegimeState)` — insert row into `regime_states` table; catch `DatabaseError` and log without blocking
    - _Requirements: 13.1, 13.2, 13.3, 13.4, 13.5, 13.6_

- [x] 7. Implement `StrategyOrchestrator`
  - [x] 7.1 Create `app/services/trading/strategy_orchestrator.py` with `StrategyOrchestrator` class
    - Implement `on_regime_detected(regime_state) -> dict[str, bool]` — derive enabled flags from `REGIME_RULES`, enforce transition cooldown, persist flags to DB, log all enables/disables with regime/underlying/timestamp per Req 15.2, 15.3
    - Implement `get_position_multiplier(underlying) -> float` — load current regime from persistence layer and return the rule's `position_multiplier`; return 0.0 on failure
    - Implement `should_allow_signal(signal, regime_state) -> tuple[bool, str]` — check signal direction against `allowed_directions` from `get_regime_rule`; log suppressions per Req 16.6
    - Implement `is_in_cooldown(underlying) -> bool` — check `_transition_cooldown` counter
    - Implement `record_trade_result(regime, underlying, strategy_name, pnl, rr)` — upsert `regime_performance` row; calculate win_rate and avg_pnl; catch `DatabaseError` and log without raising
    - Expose `get_current_regime(underlying) -> Optional[RegimeState]` as thin wrapper over `RegimePersistence.load_regime_state`
    - _Requirements: 11.2, 11.3, 11.4, 11.6, 11.7, 12.2, 16.1, 16.2, 16.3, 16.4, 16.5, 16.6, 16.7, 19.1, 19.2, 19.5, 19.6, 20.1, 20.2, 20.3, 20.4, 20.5_

  - [ ]* 7.2 Write unit tests for `StrategyOrchestrator` in `tests/test_strategy_orchestrator.py`
    - Test `on_regime_detected` for BULL_TREND_NORMAL_VOL enables only VWAP_PULLBACK and disables all others
    - Test `on_regime_detected` for HIGH_VOL_CHOP disables all strategies and sets multiplier to 0.0
    - Test `should_allow_signal` blocks a SELL signal in BULL_TREND_NORMAL_VOL regime and allows BUY
    - Test `is_in_cooldown` returns True for 3 calls after a REGIME_TRANSITION and False on the 4th
    - Test `record_trade_result` correctly increments winning_trades and total_pnl on a positive P&L
    - _Requirements: 11.2, 11.3, 11.4, 20.2, 20.3, 19.2_

- [x] 8. Checkpoint — Core components complete
  - Ensure all tests pass, ask the user if questions arise.

- [x] 9. Integrate `RegimeDetector` and `StrategyOrchestrator` into `StrategyRunner`
  - [x] 9.1 Modify `app/services/trading/strategy_runner.py` to call regime detection before the strategy loop
    - Instantiate `RegimeDetector` and `StrategyOrchestrator` as module-level singletons alongside `strategy_runner`
    - In `_run_underlying`: after VIX gate and before `_inject_oi_iv`, call `regime_detector.detect_regime(candles, indicators, underlying, is_expiry_day)` → `regime_state`
    - Call `strategy_orchestrator.on_regime_detected(regime_state)` → `updated_flags`
    - Merge `updated_flags` with user-configured `flags` (user-disable takes precedence: `final_flags[name] = updated_flags.get(name, True) and flags.get(name, True)`)
    - In the strategy loop: skip strategy if `strategy_orchestrator.is_in_cooldown(underlying)` is True; log each skipped cycle with cooldown remaining per Req 20.5
    - Use `final_flags` instead of raw `flags` for the `if not flags.get(strat.name, True)` guard
    - _Requirements: 11.1, 11.5, 11.6, 11.7, 20.2, 20.5_

- [x] 10. Integrate regime signal filtering and position sizing into `PaperEngine`
  - [x] 10.1 Modify `app/services/trading/paper_engine.py` to apply regime signal filtering and position size multiplier
    - After the `study_phase` check and before `pre_trade_check`, call `strategy_orchestrator.get_current_regime(signal.underlying)` → `regime_state`
    - Call `strategy_orchestrator.should_allow_signal(signal, regime_state)` → `(allowed, reason)`; if not allowed, call `_save_signal(signal, SignalStatus.SKIPPED, reason=reason)` and `return False` per Req 16.1
    - After lot sizing (`_calc_lots`), call `strategy_orchestrator.get_position_multiplier(signal.underlying)` → `position_multiplier`
    - If `position_multiplier == 0.0`, skip the signal with reason including regime name per Req 12.2, 15.5
    - Compute `regime_adjusted_amount = trade_amount * position_multiplier` and pass to `_calc_lots`; log the multiplier, base amount, adjusted amount, and lots per Req 12.5
    - When creating `PaperPosition`, set `entry_regime = regime_state.regime` (new column)
    - _Requirements: 12.1, 12.2, 12.3, 12.4, 12.5, 12.6, 15.5_

  - [x] 10.2 Modify `PaperEngine._close_position` to record regime performance
    - After computing `total_net` and before releasing capital, read `entry_regime` and `strategy_name` from the closed position record
    - Call `strategy_orchestrator.record_trade_result(regime=entry_regime, underlying=pos.underlying, strategy_name=pos.strategy_name, pnl=total_net, rr=trade_rr)` if `entry_regime` is set
    - Catch all exceptions from `record_trade_result` and log; do not block the close flow
    - _Requirements: 19.1, 19.6_

- [x] 11. Add regime performance API endpoint
  - [x] 11.1 Create `app/api/routes/regime.py` with a GET endpoint for regime performance statistics
    - Implement `GET /api/regime/performance` accepting optional query params `underlying` and `date_from`, `date_to`
    - Query `regime_performance` table and return a list of records with fields: regime, underlying, strategy_name, total_trades, win_rate, total_pnl, avg_pnl, avg_rr, last_trade_at
    - Calculate `win_rate` as `winning_trades / total_trades * 100` (return 0 when total_trades is 0)
    - Register router in `app/main.py` under prefix `/api/regime`
    - _Requirements: 19.4, 19.5_

- [x] 12. Final checkpoint — Full integration complete
  - Ensure all tests pass, ask the user if questions arise.

## Notes

- Tasks marked with `*` are optional and can be skipped for faster MVP delivery
- Each task references specific requirements for traceability
- The integration tasks (9, 10) are designed to be minimally invasive — existing strategy and engine logic is unchanged; new regime calls are inserted at clearly bounded points
- The `entry_regime` column on `paper_positions` is additive and nullable — existing rows are unaffected
- `StrategyOrchestrator` and `RegimeDetector` are singletons instantiated in `strategy_runner.py` to share the same state across calls
- Checkpoints ensure that DB schema and core computation are validated before integration

## Task Dependency Graph

```json
{
  "waves": [
    { "id": 0, "tasks": ["1.1", "1.2"] },
    { "id": 1, "tasks": ["2.1", "3.1"] },
    { "id": 2, "tasks": ["3.2", "4.1", "6.1"] },
    { "id": 3, "tasks": ["4.2", "5.1"] },
    { "id": 4, "tasks": ["5.2", "7.1"] },
    { "id": 5, "tasks": ["7.2", "9.1"] },
    { "id": 6, "tasks": ["10.1"] },
    { "id": 7, "tasks": ["10.2", "11.1"] }
  ]
}
```
