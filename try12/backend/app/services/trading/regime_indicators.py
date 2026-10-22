"""
Regime indicator computation functions.

Provides the RegimeIndicators dataclass and functions to compute ADX,
ATR percentile, and VWAP cross count for regime classification.
Reuses the existing indicators module for base ATR/VWAP values.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

import numpy as np

from app.strategies.indicators import atr as compute_atr_array

if TYPE_CHECKING:
    pass


# ── Dataclass ─────────────────────────────────────────────────────────────────

@dataclass
class RegimeIndicators:
    """Computed indicators used for regime classification."""
    adx: Optional[float]
    atr: float
    atr_percentile: float
    vwap: float
    price: float
    vwap_cross_count: int
    volume: float
    avg_volume: float
    orb_high: Optional[float]
    orb_low: Optional[float]
    pdh: Optional[float]
    pdl: Optional[float]


# ── ADX ───────────────────────────────────────────────────────────────────────

def compute_adx(
    high: np.ndarray,
    low: np.ndarray,
    close: np.ndarray,
    period: int = 14,
) -> Optional[float]:
    """
    Compute ADX using Wilder's smoothing method.

    Requires at least ``period * 2`` candles (default 28 for period=14).
    Returns None when there is insufficient data.

    Algorithm:
        1. True Range = max(high-low, |high-prev_close|, |low-prev_close|)
        2. +DM = high_diff when high_diff > low_diff AND high_diff > 0, else 0
        3. -DM = low_diff when low_diff > high_diff AND low_diff > 0, else 0
        4. Smooth TR, +DM, -DM with Wilder EMA (alpha = 1/period)
        5. +DI = 100 * smoothed_+DM / smoothed_TR
        6. -DI = 100 * smoothed_-DM / smoothed_TR
        7. DX  = 100 * |+DI - -DI| / (+DI + -DI)  (0 when denominator is 0)
        8. ADX = Wilder EMA of DX

    Returns:
        Latest ADX float, or None if insufficient data.
    """
    h = np.asarray(high, dtype=np.float64)
    l = np.asarray(low, dtype=np.float64)
    c = np.asarray(close, dtype=np.float64)

    if len(h) < period * 2:
        return None

    # Step 1: True Range
    prev_c = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(
        h - l,
        np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)),
    )

    # Step 2: Directional Movement
    high_diff = np.diff(h, prepend=h[0])
    low_diff = -np.diff(l, prepend=l[0])

    plus_dm = np.where((high_diff > low_diff) & (high_diff > 0), high_diff, 0.0)
    minus_dm = np.where((low_diff > high_diff) & (low_diff > 0), low_diff, 0.0)

    # Step 3: Wilder EMA helper (alpha = 1/n)
    def _wilder_ema(arr: np.ndarray, n: int) -> np.ndarray:
        alpha = 1.0 / n
        result = np.empty_like(arr)
        result[:] = np.nan
        if len(arr) < n:
            return result
        result[n - 1] = np.mean(arr[:n])
        for i in range(n, len(arr)):
            result[i] = alpha * arr[i] + (1.0 - alpha) * result[i - 1]
        return result

    smoothed_tr = _wilder_ema(tr, period)
    smoothed_plus_dm = _wilder_ema(plus_dm, period)
    smoothed_minus_dm = _wilder_ema(minus_dm, period)

    # Steps 4 & 5: Directional Indicators
    with np.errstate(divide="ignore", invalid="ignore"):
        plus_di = 100.0 * smoothed_plus_dm / smoothed_tr
        minus_di = 100.0 * smoothed_minus_dm / smoothed_tr

    # Step 6: DX (avoid division by zero)
    di_sum = plus_di + minus_di
    di_diff = np.abs(plus_di - minus_di)
    with np.errstate(divide="ignore", invalid="ignore"):
        dx = np.where(di_sum > 0, 100.0 * di_diff / di_sum, 0.0)

    # Step 7: ADX = Wilder EMA of DX
    adx_line = _wilder_ema(dx, period)

    valid = adx_line[~np.isnan(adx_line)]
    return float(valid[-1]) if len(valid) > 0 else None


# ── ATR Percentile ────────────────────────────────────────────────────────────

def compute_atr_percentile(
    atr_current: float,
    candles: list[dict],
    period: int = 20,
) -> float:
    """
    Calculate ATR percentile rank within the last ``period`` candles.

    Returns 50.0 when fewer than ``period + 14`` candles are available
    (14 is the minimum warm-up needed for a 14-period ATR).

    Algorithm:
        1. Compute ATR for each of the last ``period`` candles using the
           existing ``atr()`` function from the indicators module.
        2. Gather those ``period`` ATR values.
        3. Percentile = (number of values <= atr_current) / period * 100

    Returns:
        Percentile in range [0, 100].
    """
    min_candles = period + 14  # ATR needs 14-bar warm-up
    if len(candles) < min_candles:
        return 50.0

    highs = np.array([cd["high"] for cd in candles], dtype=np.float64)
    lows = np.array([cd["low"] for cd in candles], dtype=np.float64)
    closes = np.array([cd["close"] for cd in candles], dtype=np.float64)

    # Compute ATR series for the full candle range
    atr_series = compute_atr_array(highs, lows, closes, 14)

    # Take the last ``period`` valid ATR values
    valid_atrs = atr_series[~np.isnan(atr_series)]
    if len(valid_atrs) < period:
        return 50.0

    window_atrs = valid_atrs[-period:]
    rank = float(np.sum(window_atrs <= atr_current))
    return (rank / len(window_atrs)) * 100.0


# ── VWAP Cross Count ──────────────────────────────────────────────────────────

def compute_vwap_cross_count(
    candles: list[dict],
    vwap: float,
    window: int = 20,
) -> int:
    """
    Count the number of times price crossed VWAP over the last ``window+1``
    candles.

    A cross is detected whenever consecutive candles are on opposite sides of
    VWAP (sign change in ``close - vwap``).

    Returns 0 when fewer than ``window + 1`` candles are available.
    """
    if len(candles) < window + 1:
        return 0

    recent = candles[-(window + 1):]
    closes = np.array([cd["close"] for cd in recent], dtype=np.float64)
    diff = closes - vwap

    # Sign array: +1 above VWAP, -1 below, 0 exactly on (treat as no cross)
    signs = np.sign(diff)

    crosses = 0
    prev_sign = signs[0]
    for s in signs[1:]:
        if s != 0 and prev_sign != 0 and s != prev_sign:
            crosses += 1
        if s != 0:
            prev_sign = s

    return crosses


# ── Builder ───────────────────────────────────────────────────────────────────

def build_regime_indicators(
    candles: list[dict],
    indicators: dict,
    config,  # RegimeConfig — typed loosely to avoid circular import
    history: Optional[list[dict]] = None,
) -> RegimeIndicators:
    """
    Assemble a ``RegimeIndicators`` instance from candle data and pre-computed
    ``indicators`` dict (output of ``compute_all()``).

    Re-uses already-computed values for ATR and VWAP to avoid redundant work.
    Calls the three compute functions above for ADX, ATR percentile, and VWAP
    cross count.

    Args:
        candles:    Today's OHLCV dicts in chronological order (newest last).
        indicators: Dict produced by ``app.strategies.indicators.compute_all``.
        config:     ``RegimeConfig`` instance (provides thresholds if needed).
        history:    Optional multi-day candles ending with today's. ADX and the
                    ATR percentile are computed on it, so the regime is
                    classifiable from the open instead of after ~34 candles;
                    the VWAP cross count stays on today's session.

    Returns:
        Populated ``RegimeIndicators`` dataclass.
    """
    # Empty feeds are common during startup. Return a fail-closed snapshot
    # rather than indexing closes[-1] or letting downstream code crash.
    if not candles:
        return RegimeIndicators(
            adx=None, atr=0.0, atr_percentile=50.0, vwap=0.0, price=0.0,
            vwap_cross_count=0, volume=0.0, avg_volume=0.0,
            orb_high=None, orb_low=None, pdh=None, pdl=None,
        )

    # ADX / ATR percentile on the longest series available (warm-up)
    trend_src = history if history and len(history) >= len(candles) else candles
    highs = np.array([cd["high"] for cd in trend_src], dtype=np.float64)
    lows = np.array([cd["low"] for cd in trend_src], dtype=np.float64)
    closes = np.array([cd["close"] for cd in trend_src], dtype=np.float64)

    # Re-use pre-computed values from compute_all() where available
    atr_val: float = indicators.get("atr") or 0.0
    vwap_val: float = indicators.get("vwap") or 0.0
    price: float = indicators.get("close") or (float(closes[-1]) if len(closes) else 0.0)
    volume: float = indicators.get("volume") or 0.0
    orb_high: Optional[float] = indicators.get("orb_high")
    orb_low: Optional[float] = indicators.get("orb_low")
    pdh: Optional[float] = indicators.get("pdh")
    pdl: Optional[float] = indicators.get("pdl")
    avg_volume: float = indicators.get("avg_volume") or 0.0

    # Compute regime-specific indicators
    adx_val = compute_adx(highs, lows, closes)
    atr_pct = compute_atr_percentile(atr_val, trend_src)
    vwap_crosses = compute_vwap_cross_count(candles, vwap_val)

    return RegimeIndicators(
        adx=adx_val,
        atr=atr_val,
        atr_percentile=atr_pct,
        vwap=vwap_val,
        price=price,
        vwap_cross_count=vwap_crosses,
        volume=volume,
        avg_volume=avg_volume,
        orb_high=orb_high,
        orb_low=orb_low,
        pdh=pdh,
        pdl=pdl,
    )
