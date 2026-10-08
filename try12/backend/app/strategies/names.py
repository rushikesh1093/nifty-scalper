"""
Canonical strategy names — the single source of truth (design §6.1).

Every consumer (strategy classes, the orchestrator's flag universe, the regime
map, seed scripts, diagnostics and the trading API) imports from here, so a
name that is not a member of ``StrategyName`` cannot be enabled, gated or
reported.
"""
from enum import Enum


class StrategyName(str, Enum):
    ORB_VWAP = "ORB_VWAP"
    VWAP_PULLBACK = "VWAP_PULLBACK"
    EMA_VWAP = "EMA_VWAP"
    SUPERTREND_VWAP = "SUPERTREND_VWAP"
    PDH_PDL = "PDH_PDL"
    VWAP_REVERSAL = "VWAP_REVERSAL"
    BOLLINGER_BREAKOUT = "BOLLINGER_BREAKOUT"
    RSI_VWAP = "RSI_VWAP"
    OI_SPIKE = "OI_SPIKE"
    IV_CRUSH = "IV_CRUSH"
    CPR_VWAP = "CPR_VWAP"
    GAP_VWAP_FILL = "GAP_VWAP_FILL"


ALL_STRATEGY_NAMES: tuple[str, ...] = tuple(s.value for s in StrategyName)
