"""
Vectorized indicators library using numpy.
All functions accept numpy arrays and return numpy arrays.
Designed for speed on 1-min candle data (375 candles/session).

Changes vs previous version:
  - ema()        : seeds from first valid index so ema(ema(...)) chains work (MACD fix)
  - rma()        : new — Wilder's RMA (alpha=1/period); used by ATR and RSI
  - atr()        : switched from EMA to RMA to match TradingView / TA-Lib
  - rsi()        : switched from EMA to RMA; fixed prepend bias (use NaN not c[0])
  - sma()        : vectorized via np.convolve — O(n) instead of O(n·period) loop
  - bollinger()  : vectorized via sliding_window_view; ddof=0 (population std,
                   matches TradingView convention)
  - supertrend() : direction flip uses dir_ array instead of float equality check
  - compute_all  : supertrend_dir returns 0 during warm-up; ORBLevels carries
                   ready flag; unused `opens` array removed
"""
import numpy as np
from dataclasses import dataclass, field
from numpy.lib.stride_tricks import sliding_window_view


# ── Helpers ───────────────────────────────────────────────────────────────────

def _ensure(arr) -> np.ndarray:
    return np.asarray(arr, dtype=np.float64)


# ── VWAP ──────────────────────────────────────────────────────────────────────

def vwap(high: np.ndarray, low: np.ndarray, close: np.ndarray,
         volume: np.ndarray) -> np.ndarray:
    """
    Volume-weighted average price.
    Resets at session start — call with intraday candles only.
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
    std = np.sqrt(np.maximum(variance, 0))
    return vw + multiplier * std, vw - multiplier * std


# ── EMA ───────────────────────────────────────────────────────────────────────

def ema(close: np.ndarray, period: int) -> np.ndarray:
    """
    Exponential moving average — alpha = 2/(period+1).

    Seeds from the first non-NaN run of `period` values so that
    ema(ema(...)) chains work correctly (e.g. MACD signal line).
    Without this, NaN-seeded chains propagate NaN forever.
    """
    c   = _ensure(close)
    out = np.full_like(c, np.nan)
    k   = 2.0 / (period + 1)

    # Locate first contiguous window of `period` non-NaN values
    valid = np.flatnonzero(~np.isnan(c))
    if len(valid) < period:
        return out

    start = valid[0] + period - 1
    if start >= len(c):
        return out

    out[start] = np.mean(c[valid[0]: valid[0] + period])
    for i in range(start + 1, len(c)):
        if np.isnan(c[i]):
            out[i] = out[i - 1]   # carry forward over isolated NaNs
        else:
            out[i] = c[i] * k + out[i - 1] * (1 - k)
    return out


# ── RMA (Wilder's smoothing) ──────────────────────────────────────────────────

def rma(x: np.ndarray, period: int) -> np.ndarray:
    """
    Wilder's Running Moving Average — alpha = 1/period.

    Used by standard ATR and RSI to match TradingView / TA-Lib results.
    EMA uses alpha = 2/(period+1) ≈ 1/(period/2), making it roughly twice
    as reactive — RSI(14) with EMA behaves like RSI(~7.5).
    """
    x   = _ensure(x)
    out = np.full_like(x, np.nan)
    # Skip leading NaNs (same chain-safety as ema)
    valid = np.flatnonzero(~np.isnan(x))
    if len(valid) < period:
        return out
    start = valid[0] + period - 1
    if start >= len(x):
        return out
    out[start] = np.mean(x[valid[0]: valid[0] + period])
    a = 1.0 / period
    for i in range(start + 1, len(x)):
        if np.isnan(x[i]):
            out[i] = out[i - 1]
        else:
            out[i] = x[i] * a + out[i - 1] * (1 - a)
    return out


# ── SMA ───────────────────────────────────────────────────────────────────────

def sma(close: np.ndarray, period: int) -> np.ndarray:
    """
    Simple moving average — O(n) via np.convolve (replaces O(n·period) loop).
    """
    c   = _ensure(close)
    out = np.full_like(c, np.nan)
    if len(c) >= period:
        out[period - 1:] = np.convolve(c, np.ones(period) / period, mode="valid")
    return out


# ── ATR ───────────────────────────────────────────────────────────────────────

def atr(high, low, close, period: int = 14) -> np.ndarray:
    """
    Average True Range using Wilder's RMA (alpha=1/period).
    Matches TradingView ATR output exactly.
    """
    h, l, c = _ensure(high), _ensure(low), _ensure(close)
    prev_c    = np.roll(c, 1)
    prev_c[0] = c[0]
    tr = np.maximum(h - l,
         np.maximum(np.abs(h - prev_c), np.abs(l - prev_c)))
    return rma(tr, period)


# ── RSI ───────────────────────────────────────────────────────────────────────

def rsi(close: np.ndarray, period: int = 14) -> np.ndarray:
    """
    Relative Strength Index using Wilder's RMA (alpha=1/period).
    Matches TradingView RSI output.

    Seed fix: use np.nan as prepend so the first diff is NaN (no phantom
    gain/loss at bar 0), then drop it — avoids a spurious seed bias that
    shifts the initial avg_gain/avg_loss.
    """
    c    = _ensure(close)
    # Correct diff: no prepend — length n-1, then prepend NaN so length = n
    delta = np.empty(len(c))
    delta[0] = np.nan
    delta[1:] = np.diff(c)

    gain = np.where(delta > 0,  delta, 0.0)
    loss = np.where(delta < 0, -delta, 0.0)

    avg_gain = rma(gain, period)
    avg_loss = rma(loss, period)

    with np.errstate(divide="ignore", invalid="ignore"):
        rs = np.divide(
            avg_gain, avg_loss,
            out=np.full_like(avg_gain, np.inf),
            where=avg_loss != 0,
        )
    flat   = (avg_gain == 0) & (avg_loss == 0)
    result = 100.0 - (100.0 / (1.0 + rs))
    return np.where(flat, 50.0, result)


# ── Bollinger Bands ───────────────────────────────────────────────────────────

@dataclass
class BollingerBands:
    upper: np.ndarray
    mid:   np.ndarray
    lower: np.ndarray
    width: np.ndarray   # (upper-lower)/mid — squeeze indicator


def bollinger(close: np.ndarray, period: int = 20, std_dev: float = 2.0) -> BollingerBands:
    """
    Bollinger Bands.
    Uses population std (ddof=0) via sliding_window_view — matches TradingView.
    O(n) via stride tricks instead of O(n·period) Python loop.
    """
    c   = _ensure(close)
    mid = sma(c, period)
    std = np.full_like(c, np.nan)
    if len(c) >= period:
        windows        = sliding_window_view(c, period)   # shape (n-period+1, period)
        std[period - 1:] = windows.std(axis=1, ddof=0)    # population std, matches TV
    upper = mid + std_dev * std
    lower = mid - std_dev * std
    with np.errstate(invalid="ignore", divide="ignore"):
        width = np.where(mid != 0, (upper - lower) / mid, np.nan)
    return BollingerBands(upper=upper, mid=mid, lower=lower, width=width)


# ── Supertrend ────────────────────────────────────────────────────────────────

@dataclass
class SupertrendResult:
    supertrend: np.ndarray
    direction:  np.ndarray   # 1=UP (bullish), -1=DOWN (bearish), 0=warm-up


def supertrend(high, low, close, period: int = 10, multiplier: float = 3.0) -> SupertrendResult:
    """
    Supertrend indicator.
    Direction flip uses dir_ array (not float equality on band values) to avoid
    fragility when ATR=0 makes upper==lower.
    """
    h, l, c  = _ensure(high), _ensure(low), _ensure(close)
    atr_vals = atr(h, l, c, period)
    hl2      = (h + l) / 2.0
    upper_band = hl2 + multiplier * atr_vals
    lower_band = hl2 - multiplier * atr_vals

    n           = len(c)
    st          = np.full(n, np.nan)
    dir_        = np.zeros(n, dtype=int)   # 0 = warm-up / no signal

    final_upper = upper_band.copy()
    final_lower = lower_band.copy()

    # Find first bar where ATR warm-up is complete
    first_valid = period - 1
    while first_valid < n and (
        np.isnan(final_lower[first_valid]) or np.isnan(final_upper[first_valid])
    ):
        first_valid += 1

    if first_valid >= n:
        return SupertrendResult(supertrend=st, direction=dir_)

    # Seed first valid bar as bullish
    st[first_valid]   = final_lower[first_valid]
    dir_[first_valid] = 1

    for i in range(first_valid + 1, n):
        # Bands only tighten (ratchet) unless price crossed them last bar
        final_upper[i] = (
            upper_band[i]
            if upper_band[i] < final_upper[i - 1] or c[i - 1] > final_upper[i - 1]
            else final_upper[i - 1]
        )
        final_lower[i] = (
            lower_band[i]
            if lower_band[i] > final_lower[i - 1] or c[i - 1] < final_lower[i - 1]
            else final_lower[i - 1]
        )

        # Direction flip — use dir_ array, not float equality on band values
        if dir_[i - 1] == -1:
            # Was bearish — flip bullish only if price closes above upper band
            dir_[i] = 1 if c[i] > final_upper[i] else -1
        else:
            # Was bullish (1) or warm-up (0) — flip bearish only if price closes below lower
            dir_[i] = -1 if c[i] < final_lower[i] else 1

        st[i] = final_lower[i] if dir_[i] == 1 else final_upper[i]

    return SupertrendResult(supertrend=st, direction=dir_)


# ── ORB (Opening Range Breakout) ──────────────────────────────────────────────

@dataclass
class ORBLevels:
    high:   float
    low:    float
    broken: str    # "UP" / "DOWN" / "NONE"
    ready:  bool   # True once the full opening range window has formed


def orb(high: np.ndarray, low: np.ndarray, close: np.ndarray,
        orb_candles: int = 15) -> ORBLevels:
    """
    Opening range = first `orb_candles` 1-min candles (default 15 = 9:15-9:30).
    `ready=False` means the range is still forming — strategies should not act.
    """
    h, l, c = _ensure(high), _ensure(low), _ensure(close)
    if len(h) < orb_candles:
        # Still building the opening range — return partial levels but flag not ready
        return ORBLevels(
            high=float(np.max(h)), low=float(np.min(l)),
            broken="NONE", ready=False,
        )
    orb_h  = float(np.max(h[:orb_candles]))
    orb_l  = float(np.min(l[:orb_candles]))
    last   = float(c[-1])
    broken = "UP" if last > orb_h else ("DOWN" if last < orb_l else "NONE")
    return ORBLevels(high=orb_h, low=orb_l, broken=broken, ready=True)


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
    if tc < bc:
        tc, bc = bc, tc
    return CPRLevels(pivot=pivot, bc=bc, tc=tc, r1=r1, r2=r2, s1=s1, s2=s2, width=tc - bc)


# ── MACD ──────────────────────────────────────────────────────────────────────

@dataclass
class MACDResult:
    macd:   np.ndarray
    signal: np.ndarray
    hist:   np.ndarray


def macd(close: np.ndarray, fast: int = 12, slow: int = 26,
         signal_p: int = 9) -> MACDResult:
    """
    MACD — works correctly because ema() now skips leading NaNs.
    Previously, ema(macd_line, signal_p) seeded on NaN (slow EMA warm-up)
    and propagated NaN forever through the signal line.
    """
    c       = _ensure(close)
    macd_l  = ema(c, fast) - ema(c, slow)
    signal_ = ema(macd_l, signal_p)   # safe: ema() skips leading NaNs
    hist    = macd_l - signal_
    return MACDResult(macd=macd_l, signal=signal_, hist=hist)


# ── compute_all ───────────────────────────────────────────────────────────────

def compute_all(candles: list[dict]) -> dict:
    """
    Given a list of candle dicts {open,high,low,close,volume},
    compute all indicators and return a flat dict of the latest values.

    Changes:
      - `opens` array removed (was computed but never used)
      - supertrend_dir returns 0 during ATR warm-up (not a false 1/bullish)
      - orb_ready flag exposed so ORB strategies can gate on range completeness
    """
    if len(candles) < 2:
        return {}

    highs  = np.array([float(cd.get("high",  0)) for cd in candles])
    lows   = np.array([float(cd.get("low",   0)) for cd in candles])
    closes = np.array([float(cd.get("close", 0)) for cd in candles])
    vols   = np.array([float(cd.get("volume", 0)) for cd in candles])

    e9    = ema(closes, 9)
    e21   = ema(closes, 21)
    e50   = ema(closes, 50)
    r14   = rsi(closes, 14)
    atr14 = atr(highs, lows, closes, 14)
    bb    = bollinger(closes, 20, 2.0)
    vw    = vwap(highs, lows, closes, vols)
    st    = supertrend(highs, lows, closes, 10, 3.0)
    orb_  = orb(highs, lows, closes, 15)

    def last(arr):
        valid = arr[~np.isnan(arr)]
        return float(valid[-1]) if len(valid) else None

    # supertrend_dir: 0 means ATR is still warming up — no directional signal yet.
    # Callers (strategies) should treat 0 the same as "no supertrend data".
    st_dir = int(st.direction[-1])   # 1, -1, or 0 (warm-up)

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
        "supertrend_dir": st_dir,
        "orb_high":      orb_.high,
        "orb_low":       orb_.low,
        "orb_broken":    orb_.broken,
        "orb_ready":     orb_.ready,
        "close":         float(closes[-1]),
        "high":          float(highs[-1]),
        "low":           float(lows[-1]),
        "volume":        float(vols[-1]),
        # Rolling 10-candle average volume — avoids per-strategy recompute each tick.
        # NSE index tokens deliver volume=0 so this will also be 0/near-0 for spot;
        # strategies fall back to _avg_volume(candles) in that case via the falsy check.
        "avg_volume":    float(np.mean(vols[-11:-1])) if len(vols) > 1 else float(vols[-1]),
    }
