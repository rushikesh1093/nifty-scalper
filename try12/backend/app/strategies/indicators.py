"""
Vectorized indicators library using numpy.
All functions accept numpy arrays and return numpy arrays.
Designed for speed on 1-min candle data.
"""
import numpy as np
from dataclasses import dataclass
from typing import Optional


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure(arr) -> np.ndarray:
    return np.asarray(arr, dtype=np.float64)


# ── VWAP ──────────────────────────────────────────────────────────────────────

def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         volume: np.ndarray) -> np.ndarray:
    """
    Volume-weighted average price.
    Resets at session start (call with intraday candles only).
    """
    h, l, c, v = _ensure(high), _ensure(low), _ensure(close), _ensure(volume)
    typical = (h + l + c) / 3.0
    cumtpv  = np.cumsum(typical * v)
    cumvol  = np.cumsum(v)
    with np.errstate(invalid="ignore", divide="ignore"):
        result = np.where(cumvol > 0, cumtpv / cumvol, typical)
    return result


def vwap_bands(high, low, close, volume, multiplier: float = 1.0):
    """VWAP ± 1 std-dev band."""
    h, l, c, v = _ensure(high), _ensure(low), _ensure(close), _ensure(volume)
    typical = (h + l + c) / 3.0
    vw      = vwap(h, l, c, v)
    cumvol  = np.cumsum(v)
    cumsq   = np.cumsum(typical ** 2 * v)
    with np.errstate(invalid="ignore", divide="ignore"):
        variance = np.where(cumvol > 0, cumsq / cumvol - vw ** 2, 0.0)
    std    = np.sqrt(np.maximum(variance, 0))
    return vw + multiplier * std, vw - multiplier * std


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(close: np.ndarray, period: int) -> np.ndarray:
    c   = _ensure(close)
    k   = 2.0 / (period + 1)
    out = np.empty_like(c)
    out[:] = np.nan
    # Find first valid
    start = period - 1
    if start >= len(c):
        return out
    out[start] = np.mean(c[:period])
    for i in range(start + 1, len(c)):
        out[i] = c[i] * k + out[i - 1] * (1 - k)
    return out


def sma(close: np.ndarray, period: int) -> np.ndarray:
    c   = _ensure(close)
    out = np.full_like(c, np.nan)
    for i in range(period - 1, len(c)):
        out[i] = np.mean(c[i - period + 1: i + 1])
    return out


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(high, low, close, period: int = 14) -> np.ndarray:
    h, l, c = _ensure(high), _ensure(low), _ensure(close)
    prev_c  = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l,
         np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return ema(tr, period)


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    c    = _ensure(close)
    diff = np.diff(c, prepend=c[0])
    gain = np.where(diff > 0, diff, 0.0)
    loss = np.where(diff < 0, -diff, 0.0)
    avg_gain = ema(gain, period)
    avg_loss = ema(loss, period)
    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(avg_gain, avg_loss, out=np.full_like(avg_gain, np.inf), where=avg_loss != 0)
    flat = (avg_gain == 0) & (avg_loss == 0)
    result = 100 - (100 / (1 + rs))
    return np.where(flat, 50.0, result)


# ── Bollinger Bands ───────────────────────────────────────────────────────────

@dataclass
class BollingerBands:
    upper: np.ndarray
    mid:   np.ndarray
    lower: np.ndarray
    width: np.ndarray   # (upper-lower)/mid — squeeze indicator


def bollinger(close: np.ndarray, period: int = 20, std_dev: float = 2.0) -> BollingerBands:
    c   = _ensure(close)
    mid = sma(c, period)
    std = np.array([
        np.std(c[max(0, i - period + 1): i + 1], ddof=1)
        if i >= period - 1 else np.nan
        for i in range(len(c))
    ])
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    with np.errstate(invalid="ignore", divide="ignore"):
        width = np.where(mid != 0, (upper - lower) / mid, np.nan)
    return BollingerBands(upper=upper, mid=mid, lower=lower, width=width)


# ── Supertrend ────────────────────────────────────────────────────────────────

@dataclass
class SupertrendResult:
    supertrend: np.ndarray
    direction:  np.ndarray   # 1 = UP (bullish), -1 = DOWN (bearish)


def supertrend(high, low, close, period: int = 10, multiplier: float = 3.0) -> SupertrendResult:
    h, l, c = _ensure(high), _ensure(low), _ensure(close)
    atr_vals = atr(h, l, c, period)
    hl2      = (h + l) / 2.0
    upper_band = hl2 + multiplier * atr_vals
    lower_band = hl2 - multiplier * atr_vals

    n    = len(c)
    st   = np.full(n, np.nan)
    dir_ = np.ones(n, dtype=int)   # 1=UP, -1=DOWN

    # Final bands (adjusted) — initialise with raw bands; NaN for warm-up bars
    final_upper = upper_band.copy()
    final_lower = lower_band.copy()

    # Find first bar where ATR is valid (warm-up complete)
    first_valid = period - 1   # ema needs period-1 prior bars for ATR
    # Advance to first index where both bands are finite
    while first_valid < n and (
        np.isnan(final_lower[first_valid]) or np.isnan(final_upper[first_valid])
    ):
        first_valid += 1

    if first_valid >= n:
        # Not enough data — return all-NaN supertrend, direction stays 1
        return SupertrendResult(supertrend=st, direction=dir_)

    # Seed the first valid bar
    st[first_valid]   = final_lower[first_valid]   # assume bullish at start
    dir_[first_valid] = 1

    for i in range(first_valid + 1, n):
        # Adjust bands: only tighten, never widen, unless price crossed the band
        final_upper[i] = upper_band[i] if (
            upper_band[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1]
        ) else final_upper[i - 1]

        final_lower[i] = lower_band[i] if (
            lower_band[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1]
        ) else final_lower[i - 1]

        # Direction: previous bar was bearish (on upper band)
        if st[i - 1] == final_upper[i - 1]:
            dir_[i] = 1 if c[i] > final_upper[i] else -1
        else:
            # Previous bar was bullish (on lower band)
            dir_[i] = -1 if c[i] < final_lower[i] else 1

        st[i] = final_lower[i] if dir_[i] == 1 else final_upper[i]

    return SupertrendResult(supertrend=st, direction=dir_)


# ── ORB (Opening Range Breakout) ──────────────────────────────────────────────

@dataclass
class ORBLevels:
    high:   float
    low:    float
    broken: str   # "UP" / "DOWN" / "NONE"


def orb(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        orb_candles: int = 15) -> ORBLevels:
    """
    Opening range = first `orb_candles` 1-min candles (default 15 = 9:15-9:30).
    """
    h, l, c = _ensure(high), _ensure(low), _ensure(close)
    if len(h) < orb_candles:
        return ORBLevels(high=float(np.max(h)), low=float(np.min(l)), broken="NONE")
    orb_h = float(np.max(h[:orb_candles]))
    orb_l = float(np.min(l[:orb_candles]))
    last  = float(c[-1])
    broken = "UP" if last > orb_h else ("DOWN" if last < orb_l else "NONE")
    return ORBLevels(high=orb_h, low=orb_l, broken=broken)


# ── CPR (Central Pivot Range) ─────────────────────────────────────────────────

@dataclass
class CPRLevels:
    pivot:  float
    bc:     float   # Bottom Central
    tc:     float   # Top Central
    r1:     float
    r2:     float
    s1:     float
    s2:     float
    width:  float   # TC - BC (narrow = trending day expected)


def cpr(prev_high: float, prev_low: float, prev_close: float) -> CPRLevels:
    """Calculate CPR from previous day's H/L/C."""
    pivot = (prev_high + prev_low + prev_close) / 3.0
    bc    = (prev_high + prev_low) / 2.0
    tc    = (pivot - bc) + pivot
    r1    = 2 * pivot - prev_low
    r2    = pivot + (prev_high - prev_low)
    s1    = 2 * pivot - prev_high
    s2    = pivot - (prev_high - prev_low)
    # Ensure TC > BC
    if tc < bc:
        tc, bc = bc, tc
    return CPRLevels(pivot=pivot, bc=bc, tc=tc, r1=r1, r2=r2, s1=s1, s2=s2, width=tc - bc)


# ── MACD (available but not wired into compute_all / strategies) ─────────────
# Included here for completeness; can be added to compute_all if needed.

@dataclass
class MACDResult:
    macd:   np.ndarray
    signal: np.ndarray
    hist:   np.ndarray


def macd(close: np.ndarray, fast: int = 12, slow: int = 26, signal_p: int = 9) -> MACDResult:
    c       = _ensure(close)
    macd_l  = ema(c, fast) - ema(c, slow)
    signal_ = ema(macd_l, signal_p)
    hist    = macd_l - signal_
    return MACDResult(macd=macd_l, signal=signal_, hist=hist)


# ── Convenience: build indicator state from candle list ───────────────────────

def compute_all(candles: list[dict]) -> dict:
    """
    Given a list of candle dicts {open,high,low,close,volume},
    compute all indicators and return a flat dict of latest values.
    """
    if len(candles) < 2:
        return {}

    highs  = np.array([cd["high"]          for cd in candles])
    lows   = np.array([cd["low"]           for cd in candles])
    closes = np.array([cd["close"]         for cd in candles])
    opens  = np.array([cd["open"]          for cd in candles])
    # Use 0 (not 1) as the default so zero-volume NSE index ticks don't corrupt
    # VWAP.  The vwap() implementation already guards against cumvol==0 via the
    # np.where clause, so passing zeros is safe.
    vols   = np.array([cd.get("volume", 0) for cd in candles])

    e9   = ema(closes, 9)
    e21  = ema(closes, 21)
    e50  = ema(closes, 50)
    r14  = rsi(closes, 14)
    atr14= atr(highs, lows, closes, 14)
    bb   = bollinger(closes, 20, 2.0)
    vw   = vwap(highs, lows, closes, vols)
    st   = supertrend(highs, lows, closes, 10, 3.0)
    orb_ = orb(highs, lows, closes, 15)

    def last(arr):
        valid = arr[~np.isnan(arr)]
        return float(valid[-1]) if len(valid) else None

    return {
        "ema9":          last(e9),
        "ema21":         last(e21),
        "ema50":         last(e50),
        "rsi":           last(r14),
        "atr":           last(atr14),
        "bb_upper":      last(bb.upper),
        "bb_mid":        last(bb.mid),
        "bb_lower":      last(bb.lower),
        "bb_width":      last(bb.width),
        "vwap":          last(vw),
        "supertrend":    last(st.supertrend),
        "supertrend_dir": int(st.direction[-1]),
        "orb_high":      orb_.high,
        "orb_low":       orb_.low,
        "orb_broken":    orb_.broken,
        "close":         float(closes[-1]),
        "high":          float(highs[-1]),
        "low":           float(lows[-1]),
        "volume":        float(vols[-1]),
    }
