"""
Canonical strategy names — the single source of truth (design §6.1).

Every consumer (strategy classes, the orchestrator's flag universe, the regime
map, seed scripts, diagnostics and the trading API) imports from here, so a
name that is not a member of ``StrategyName`` cannot be enabled, gated or
reported.

Three strategies, each with its own entry AND exit rules (exit_plan.py):
  VWAP_EMA_PULLBACK  3-min trend pullback to EMA 9/21 above/below VWAP
  ORB_BREAKOUT       5-min close beyond the 09:15-09:30 opening range
  SUPERTREND_EMA     1-min Supertrend flip + EMA 5/13 cross, ADX filter
"""
from enum import Enum


class StrategyName(str, Enum):
    VWAP_EMA_PULLBACK = "VWAP_EMA_PULLBACK"
    ORB_BREAKOUT = "ORB_BREAKOUT"
    SUPERTREND_EMA = "SUPERTREND_EMA"


ALL_STRATEGY_NAMES: tuple[str, ...] = tuple(s.value for s in StrategyName)
