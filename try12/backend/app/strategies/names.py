"""
Canonical strategy names — the single source of truth (design §6.1).

Every consumer (strategy classes, the orchestrator's flag universe, the regime
map, seed scripts, diagnostics and the trading API) imports from here, so a
name that is not a member of ``StrategyName`` cannot be enabled, gated or
reported.

Seven strategies, each with its own entry AND exit rules (exit_plan.py):
  VWAP_EMA_PULLBACK      3-min trend pullback to EMA 9/21 above/below VWAP
  ORB_BREAKOUT           5-min close beyond the 09:15-09:30 opening range
  SUPERTREND_EMA         1-min Supertrend flip + EMA 5/13 cross, ADX filter
  FIRST_CANDLE_BREAKOUT  5-min close beyond the 09:15 candle +/- 10 pts
  EMA_RSI_PULLBACK_15M   15-min EMA 9/21 trend, RSI 40-50 / 50-60 pullback
  PIVOT_BOUNCE           3-min bounce / rejection at the daily PP, R1, S1
  MOMENTUM_BURST         5-min three-candle burst beyond EMA 20, ATR > 15
"""
from enum import Enum


class StrategyName(str, Enum):
    VWAP_EMA_PULLBACK = "VWAP_EMA_PULLBACK"
    ORB_BREAKOUT = "ORB_BREAKOUT"
    SUPERTREND_EMA = "SUPERTREND_EMA"
    FIRST_CANDLE_BREAKOUT = "FIRST_CANDLE_BREAKOUT"
    EMA_RSI_PULLBACK_15M = "EMA_RSI_PULLBACK_15M"
    PIVOT_BOUNCE = "PIVOT_BOUNCE"
    MOMENTUM_BURST = "MOMENTUM_BURST"


ALL_STRATEGY_NAMES: tuple[str, ...] = tuple(s.value for s in StrategyName)
