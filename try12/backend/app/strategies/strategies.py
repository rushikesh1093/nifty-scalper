"""
All 12 scalping strategies.
Each inherits BaseStrategy and emits BUY (buy CE) or SELL (buy PE) signals.
"""
import logging
from typing import Optional
from datetime import time as dtime
import numpy as np

from app.strategies.base import BaseStrategy, Direction, SignalEvent
from app.strategies.indicators import ema, rsi, bollinger, supertrend, atr
from app.strategies.names import ALL_STRATEGY_NAMES, StrategyName
from app.runtime.context import now_ist, trading_day
from app.services.market_data.timestamps import try_normalize_candle_timestamp

logger = logging.getLogger(__name__)


def _c(candles, key, idx=-1):
    try:
        return float(candles[idx][key])
    except Exception:
        return None

def _is_between(t: dtime, s: dtime, e: dtime) -> bool:
    return s <= t <= e

def _is_today_ist(candle) -> bool:
    dt = try_normalize_candle_timestamp(candle.get("timestamp"))
    return dt is not None and dt.date() == trading_day()


def _market_time(candles) -> dtime:
    dt = try_normalize_candle_timestamp(candles[-1].get("timestamp", ""))
    return (dt.time().replace(tzinfo=None) if dt else
            now_ist().time().replace(tzinfo=None))

def _avg_volume(candles, n=10) -> float:
    vols = [float(c.get("volume", 0)) for c in candles[-n-1:-1]]
    return float(np.mean(vols)) if vols else 1.0


def _vol_surge(vol: float, avg: float, multiplier: float = 1.5) -> bool:
    """
    True only when *vol* is a genuine surge above *avg*.

    Volume comes from the near-month index FUTURE (strategy_runner merges it
    into the spot candles), because NSE index spot carries volume=0. When no
    volume is available at all the check FAILS (returns False): an unconfirmed
    breakout is not a breakout. strategy_runner records a DATA_GAP when the
    futures volume is missing so this is visible, not silent.
    """
    if not avg or avg <= 0 or vol is None:
        return False
    return vol > multiplier * avg


def _arrays(candles):
    h = np.array([float(c.get("high",  0)) for c in candles])
    l = np.array([float(c.get("low",   0)) for c in candles])
    c = np.array([float(c.get("close", 0)) for c in candles])
    o = np.array([float(c.get("open",  0)) for c in candles])
    v = np.array([float(c.get("volume",1)) for c in candles])
    return h, l, c, o, v


# 1-min ATR floor as a fraction of price. Measured on this feed (Oct 2026):
# NIFTY's median 5-min range ≈ 21 pts, 25th percentile ≈ 13 pts, i.e. a 1-min
# ATR of roughly 6–9 pts. 0.025% of price ≈ 5.8 pts on NIFTY 23,100 and
# ≈ 13 pts on BANKNIFTY 52,000 — it blocks only the quietest quarter of the
# session, where bought options bleed theta without reaching target.
ATR_MIN_PCT_OF_PRICE = 0.00025


def atr_floor(price: Optional[float]) -> float:
    """Minimum 1-min ATR (points) for *price*; 0 when the price is unknown."""
    try:
        return float(price) * ATR_MIN_PCT_OF_PRICE if price and price > 0 else 0.0
    except (TypeError, ValueError):
        return 0.0


def _atr_ok(indicators: dict, underlying: str) -> bool:
    """True when the 1-min ATR clears ``atr_floor(close)``. One floor, shared
    with the strategy_runner cycle gate, so the two can never disagree."""
    atr_v = indicators.get("atr") or 0.0
    floor = atr_floor(indicators.get("close"))
    return floor > 0 and atr_v >= floor


def _crossed_above(prev_close: Optional[float], close: Optional[float], level: Optional[float]) -> bool:
    """A FRESH cross: the previous candle closed at/below *level*, this one above."""
    return None not in (prev_close, close, level) and prev_close <= level < close


def _crossed_below(prev_close: Optional[float], close: Optional[float], level: Optional[float]) -> bool:
    return None not in (prev_close, close, level) and prev_close >= level > close


class _OncePerDay:
    """Remembers which (trading_day, key) breakouts already fired, so a level
    is traded once per session instead of on every candle that stays beyond it."""

    def __init__(self) -> None:
        self._fired: set[tuple] = set()

    def done(self, key: str) -> bool:
        return (trading_day(), key) in self._fired

    def mark(self, key: str) -> None:
        today = trading_day()
        self._fired = {k for k in self._fired if k[0] == today}
        self._fired.add((today, key))


# ─── 1. ORB + VWAP + Volume ──────────────────────────────────────────────────

class ORBVWAPStrategy(BaseStrategy):
    """9:15-9:30 opening range breakout + VWAP confirm + volume surge.

    Fires only on the FRESH cross of the opening-range high/low (previous close
    inside the range, this close outside), and at most once per side per day —
    not on every later candle that is still beyond the range.
    """
    name = StrategyName.ORB_VWAP.value; sl_pct = 0.15; target_pct = 0.30

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._once = _OncePerDay()

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 20: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(11,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); vol = _c(candles,"volume")
        vwap_v = indicators.get("vwap")
        orb_h = indicators.get("orb_high"); orb_l = indicators.get("orb_low")
        if None in (close, vol, vwap_v, orb_h, orb_l): return None

        prev = _c(candles, "close", -2)
        vol_surge = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.5)
        if (not self._once.done("UP") and _crossed_above(prev, close, orb_h)
                and close > vwap_v and vol_surge):
            self._once.mark("UP")
            return self._make_signal(Direction.BUY, close, indicators,
                f"ORB UP close={close:.1f} orb_h={orb_h:.1f} vwap={vwap_v:.1f}")
        if (not self._once.done("DOWN") and _crossed_below(prev, close, orb_l)
                and close < vwap_v and vol_surge):
            self._once.mark("DOWN")
            return self._make_signal(Direction.SELL, close, indicators,
                f"ORB DOWN close={close:.1f} orb_l={orb_l:.1f} vwap={vwap_v:.1f}")
        return None


# ─── 2. VWAP Pullback ────────────────────────────────────────────────────────

class VWAPPullbackStrategy(BaseStrategy):
    """Trend established, pullback to VWAP, rejection candle entry.

    The trend-count window (last 6 candles) now only counts candles that belong
    to today's IST session (timestamp starts with today's date string).  Without
    this guard, at market open the window contains yesterday's candles which have
    no bearing on today's intraday VWAP direction — causing false trend counts
    that make the strategy fire immediately at 9:45 on stale data.
    """
    name = StrategyName.VWAP_PULLBACK.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 15

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 10: return None
        if not _is_between(_market_time(candles), dtime(9,45), dtime(14,0)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        low = _c(candles,"low");     high  = _c(candles,"high")
        vwap_v = indicators.get("vwap")
        if None in (close, open_, low, high, vwap_v): return None

        # Only count candles from today's IST session in the trend window.
        # Candles are filtered to today at the strategy_runner level, but the
        # guard here is a safety net in case the fallback path supplies
        # multi-day data (e.g. during pre-market startup).
        today_window = [c for c in candles[-7:-1] if _is_today_ist(c)]
        # Need at least 4 today-candles in the window to establish a trend
        if len(today_window) < 4:
            return None

        above = sum(1 for c in today_window if (_c([c], "close") or 0) > vwap_v)
        below = sum(1 for c in today_window if (_c([c], "close") or 0) < vwap_v)

        if above >= 4 and low <= vwap_v*1.001 and close > vwap_v and close > open_:
            return self._make_signal(Direction.BUY, close, indicators,
                f"VWAP pullback bounce vwap={vwap_v:.1f}")
        if below >= 4 and high >= vwap_v*0.999 and close < vwap_v and close < open_:
            return self._make_signal(Direction.SELL, close, indicators,
                f"VWAP pullback rejection vwap={vwap_v:.1f}")
        return None


# ─── 3. EMA 9/21 + VWAP ──────────────────────────────────────────────────────

class EMAVWAPStrategy(BaseStrategy):
    """EMA9 crosses EMA21 + price on correct side of VWAP + EMA50 filter.

    ATR gate prevents firing during dead chop where EMA crosses are pure noise.
    Volume surge confirms real participation on the crossover candle — filters
    out fake crosses caused by low-volume drift between EMAs.
    """
    name = StrategyName.EMA_VWAP.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 20   # EMA cross is a trend signal — needs time to play out

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 25: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(14,30)): return None
        # ATR gate: EMA crosses in dead chop are noise — only trade when market moves
        if not _atr_ok(indicators, self.underlying): return None

        close  = _c(candles, "close")
        vol    = _c(candles, "volume")
        vwap_v = indicators.get("vwap")
        ema50  = indicators.get("ema50")
        if None in (close, vwap_v): return None

        # Volume surge on crossover candle — confirms real participation, not drift
        vol_surge = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.3)

        _, _, c_arr, _, _ = _arrays(candles)
        e9_now  = indicators.get("ema9")
        e21_now = indicators.get("ema21")
        if e9_now is None or e21_now is None: return None

        e9_full  = ema(c_arr, 9)
        e21_full = ema(c_arr, 21)
        if np.isnan(e9_full[-2]) or np.isnan(e21_full[-2]): return None

        cross_up   = e9_full[-2] <= e21_full[-2] and e9_now > e21_now
        cross_down = e9_full[-2] >= e21_full[-2] and e9_now < e21_now

        if cross_up and close > vwap_v and vol_surge and (ema50 is None or close > ema50):
            return self._make_signal(Direction.BUY, close, indicators,
                f"EMA9 cross UP above VWAP={vwap_v:.1f} vol_surge=True")
        if cross_down and close < vwap_v and vol_surge and (ema50 is None or close < ema50):
            return self._make_signal(Direction.SELL, close, indicators,
                f"EMA9 cross DOWN below VWAP={vwap_v:.1f} vol_surge=True")
        return None


# ─── 4. Supertrend + VWAP ────────────────────────────────────────────────────

class SupertrendVWAPStrategy(BaseStrategy):
    """Supertrend direction flip confirmed by VWAP + EMA alignment.

    ATR gate added: supertrend flips in low-volatility chop are unreliable
    because the ATR-based bands compress and any tiny tick can trigger a flip.
    """
    name = StrategyName.SUPERTREND_VWAP.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 25   # Supertrend flip is rare and high-conviction — long cooldown

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 15: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(14,0)): return None
        # ATR gate: compressed ATR = narrow bands = unreliable flips
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close")
        vwap_v = indicators.get("vwap")
        ema9 = indicators.get("ema9"); ema21 = indicators.get("ema21")
        if None in (close, vwap_v): return None

        h, l, c_arr, _, _ = _arrays(candles)
        st = supertrend(h, l, c_arr, 10, 3.0)
        # Guard: need at least 2 valid (non-NaN) supertrend values for flip detection
        if np.isnan(st.supertrend[-1]) or np.isnan(st.supertrend[-2]): return None

        flip_up   = st.direction[-2] == -1 and st.direction[-1] == 1
        flip_down = st.direction[-2] == 1  and st.direction[-1] == -1

        if flip_up and close > vwap_v and ema9 and ema21 and ema9 > ema21:
            return self._make_signal(Direction.BUY, close, indicators,
                "Supertrend flip UP + VWAP + EMA bull")
        if flip_down and close < vwap_v and ema9 and ema21 and ema9 < ema21:
            return self._make_signal(Direction.SELL, close, indicators,
                "Supertrend flip DOWN + VWAP + EMA bear")
        return None


# ─── 5. Previous Day High/Low ─────────────────────────────────────────────────

class PDHPDLStrategy(BaseStrategy):
    """Clean breakout above PDH or below PDL with volume confirmation.

    Two-candle confirmation of a FRESH break: the candle before the pair was
    still on the near side of the level. Once per side per day.
    """
    name = StrategyName.PDH_PDL.value; sl_pct = 0.15; target_pct = 0.30

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None
        self._once = _OncePerDay()

    def set_levels(self, pdh: float, pdl: float):
        self.pdh = pdh; self.pdl = pdl

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 5 or not self.pdh or not self.pdl: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,0)): return None
        if not _atr_ok(indicators, self.underlying): return None

        c1 = _c(candles,"close",-1); c2 = _c(candles,"close",-2); c3 = _c(candles,"close",-3)
        v1 = _c(candles,"volume",-1)
        if None in (c1, c2, c3, v1): return None
        vol_ok = _vol_surge(v1, indicators.get("avg_volume") or _avg_volume(candles), 1.2)

        if (not self._once.done("PDH") and c3 <= self.pdh < c2 and c1 > self.pdh and vol_ok):
            self._once.mark("PDH")
            return self._make_signal(Direction.BUY, c1, indicators,
                f"PDH break {c1:.1f} > {self.pdh:.1f}")
        if (not self._once.done("PDL") and c3 >= self.pdl > c2 and c1 < self.pdl and vol_ok):
            self._once.mark("PDL")
            return self._make_signal(Direction.SELL, c1, indicators,
                f"PDL break {c1:.1f} < {self.pdl:.1f}")
        return None


# ─── 6. VWAP Reversal ────────────────────────────────────────────────────────

class VWAPReversalStrategy(BaseStrategy):
    """Price over-extended from VWAP + reversal candle pattern."""
    name = StrategyName.VWAP_REVERSAL.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 10

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 5: return None
        if not _is_between(_market_time(candles), dtime(10,0), dtime(14,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        high  = _c(candles,"high");  low   = _c(candles,"low")
        vwap_v = indicators.get("vwap"); rsi_v = indicators.get("rsi")
        if None in (close, open_, vwap_v): return None

        pct = (close - vwap_v) / vwap_v * 100
        body  = abs(close - open_)
        range_= high - low
        body_r = body / range_ if range_ > 0 else 0

        # RSI must be present — reversal without RSI confirmation is pure noise
        if rsi_v is None: return None

        # Raised extension threshold 0.5% → 0.8%: at 0.5% the price has barely
        # moved from VWAP and reversals fail more than half the time.  0.8% means
        # the market is genuinely over-extended and a snap-back is likely.
        if pct > 0.8 and close < open_ and body_r > 0.5 and rsi_v > 70:
            return self._make_signal(Direction.SELL, close, indicators,
                f"VWAP reversal DOWN +{pct:.2f}% RSI={rsi_v:.1f}")
        if pct < -0.8 and close > open_ and body_r > 0.5 and rsi_v < 30:
            return self._make_signal(Direction.BUY, close, indicators,
                f"VWAP reversal UP {pct:.2f}% RSI={rsi_v:.1f}")
        return None


# ─── 7. Bollinger Breakout ───────────────────────────────────────────────────

class BollingerBreakoutStrategy(BaseStrategy):
    """BB squeeze + explosive breakout beyond bands confirmed by VWAP.

    ATR gate added: a breakout candle outside the bands is meaningless if the
    underlying isn't actually moving — options won't follow without spot movement.
    """
    name = StrategyName.BOLLINGER_BREAKOUT.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 20

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 25: return None
        if not _is_between(_market_time(candles), dtime(9,45), dtime(14,0)): return None
        # ATR gate: low ATR = bands are artificially compressed by chop, not genuine squeeze
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close")
        bb_u = indicators.get("bb_upper"); bb_l = indicators.get("bb_lower")
        bb_w = indicators.get("bb_width"); vwap_v = indicators.get("vwap")
        if None in (close, bb_u, bb_l, bb_w, vwap_v): return None

        _, _, c_arr, _, _ = _arrays(candles)
        bb_all = bollinger(c_arr, 20, 2.0)
        valid_w = bb_all.width[~np.isnan(bb_all.width)]
        if len(valid_w) < 5: return None

        # Squeeze detection uses the PREVIOUS bar's width, not the current bar.
        # A genuine breakout candle is wide by definition — if we checked the
        # current bar we'd never pass squeeze=True at the exact moment of breakout.
        # Instead we check that the bar *before* this one was inside the squeeze,
        # confirming that coiling happened and THIS candle is the escape.
        prev_bb_w = float(valid_w[-2]) if len(valid_w) >= 2 else float(valid_w[-1])
        # avg_w excludes the current and previous bars (use -22:-2 window)
        avg_w = (
            float(np.mean(valid_w[-22:-2])) if len(valid_w) >= 22
            else float(np.mean(valid_w[:-2])) if len(valid_w) > 2
            else 0.0
        )
        # Squeeze: previous bar's width was tight AND current bar breaks out
        squeeze = avg_w > 0 and prev_bb_w < avg_w * 0.80

        if squeeze and close > bb_u and close > vwap_v:
            return self._make_signal(Direction.BUY, close, indicators,
                f"BB squeeze breakout UP width={bb_w:.4f}")
        if squeeze and close < bb_l and close < vwap_v:
            return self._make_signal(Direction.SELL, close, indicators,
                f"BB squeeze breakout DOWN width={bb_w:.4f}")
        return None


# ─── 8. RSI + VWAP ───────────────────────────────────────────────────────────

class RSIVWAPStrategy(BaseStrategy):
    """RSI oversold/overbought near VWAP level with candle confirmation.

    ATR gate added: RSI extremes during low-volatility consolidation produce
    false reversal signals — the underlying isn't actually reversing, it's just
    grinding sideways and options bleed theta without hitting targets.

    Removed: stale 'RSI < 30 and still falling' momentum SELL.  Rationale:
      - By the time RSI reaches 30 AND is still declining for 3 bars AND price
        is below VWAP AND we're trying to enter a PE, the move is already 30–40
        candles old and the risk/reward to a bounce is unfavourable.
      - This condition also conflicts with the reversal SELL at RSI > 72 — it
        creates a mirror-image entry at the wrong extreme (deep oversold → we
        should be looking for BUY, not adding more SELL).
      - Removed to prevent chasing exhausted moves.
    """
    name = StrategyName.RSI_VWAP.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 8

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 15: return None
        if not _is_between(_market_time(candles), dtime(10,0), dtime(14,30)): return None
        # ATR gate: RSI extremes in low-volatility chop are not real reversals
        if not _atr_ok(indicators, self.underlying): return None

        close  = _c(candles, "close"); open_ = _c(candles, "open")
        rsi_v  = indicators.get("rsi"); vwap_v = indicators.get("vwap")
        if None in (close, open_, rsi_v, vwap_v): return None

        near_vwap = abs(close - vwap_v) / vwap_v * 100 < 0.3

        # ── Reversal signals (RSI at extreme, near VWAP) ─────────────────────
        # Thresholds are deliberately non-overlapping with the momentum block:
        #   Reversal BUY  : RSI < 35  (oversold)
        #   Reversal SELL : RSI > 72  (overbought)  ← raised clear of momentum's > 70
        # This means a bar with RSI 70–72 can only trigger a momentum signal,
        # never a reversal — no ambiguity at the boundary.
        if rsi_v < 35 and near_vwap and close > open_:
            return self._make_signal(Direction.BUY, close, indicators,
                f"RSI={rsi_v:.1f} oversold + VWAP bounce")
        if rsi_v > 72 and near_vwap and close < open_:
            return self._make_signal(Direction.SELL, close, indicators,
                f"RSI={rsi_v:.1f} overbought + VWAP reject")

        # ── Momentum signals (RSI trending, away from VWAP) ──────────────────
        # Uses the pre-computed rsi array from indicators for the [-1] value and
        # rebuilds only the full array (needed for [-2] and [-3] trend check).
        # compute_all stores only the latest scalar so prior bars need the array.
        _, _, c_arr, _, _ = _arrays(candles)
        r_arr   = rsi(c_arr, 14)
        valid_r = r_arr[~np.isnan(r_arr)]
        if len(valid_r) < 3:
            return None

        rsi_cur   = rsi_v              # from indicators — authoritative latest
        rsi_prev  = float(valid_r[-2])
        rsi_prev2 = float(valid_r[-3])

        # Momentum BUY: RSI rising out of oversold territory (was < 40, now 40–60),
        # price above VWAP, bullish candle.  This catches early-trend entries where
        # momentum is building, NOT the overbought zone (70+) where reversals live.
        # Condition: RSI was below 40 two bars ago and has recovered through 45+
        if (40 <= rsi_cur <= 60
                and rsi_prev2 < 40
                and rsi_cur > rsi_prev
                and close > vwap_v and close > open_):
            return self._make_signal(Direction.BUY, close, indicators,
                f"RSI momentum recovery={rsi_cur:.1f} rising from oversold above VWAP")

        # Momentum SELL: RSI falling from overbought territory (was > 65, now 40–60),
        # price below VWAP, bearish candle.
        if (40 <= rsi_cur <= 60
                and rsi_prev2 > 65
                and rsi_cur < rsi_prev
                and close < vwap_v and close < open_):
            return self._make_signal(Direction.SELL, close, indicators,
                f"RSI momentum reversal={rsi_cur:.1f} falling from overbought below VWAP")

        return None


# ─── 9. OI Spike + Price ─────────────────────────────────────────────────────

class OISpikeStrategy(BaseStrategy):
    """
    Sudden OI buildup in CE/PE signals smart money positioning.
    CE OI spike + price rising  → CE writing by big players → cap at resistance → SELL
    PE OI spike + price falling → PE writing → support forming → BUY
    CE OI spike + price falling → CE unwinding → bearish → SELL
    PE OI spike + price rising  → PE unwinding → bullish → BUY

    Hardened rules (prevent expiry-day noise losses):
      - Blocked on expiry day entirely (OI rolls create massive fake spikes)
      - OI threshold raised 15% → 25% (genuine smart-money accumulation)
      - Supertrend must agree with trade direction
      - 3-candle trend confirmation required (close[−3] < close[−2] < close[−1] for BUY)
      - Price must be > 0.15% from VWAP (not just touching it)
      - sl_pct raised to 0.15 (was 0.10) so a 2-tick adverse move doesn't hit SL
      - Only active 9:30–12:30 (not 13:30; expiry afternoon OI is pure noise)
    """
    name = StrategyName.OI_SPIKE.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 15

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.ce_oi_prev:    float = 0
        self.pe_oi_prev:    float = 0
        self.ce_oi:         float = 0
        self.pe_oi:         float = 0
        self._update_count: int   = 0   # guard: need ≥2 real snapshots before firing
        self.is_expiry_day: bool  = False   # set by strategy_runner before evaluate

    def update_oi(self, ce_oi: float, pe_oi: float):
        self.ce_oi_prev = self.ce_oi; self.pe_oi_prev = self.pe_oi
        self.ce_oi = ce_oi;           self.pe_oi = pe_oi
        self._update_count += 1

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        # Block entirely on expiry day — OI rolls, unwinding, and re-hedging
        # create massive fake spike signals all afternoon.
        if self.is_expiry_day: return None

        # Need at least 2 real OI snapshots and enough candles for trend check
        if len(candles) < 8 or self._update_count < 2: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(12,30)): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        vwap_v = indicators.get("vwap")
        st_dir = indicators.get("supertrend_dir")   # 1=UP, −1=DOWN
        if None in (close, open_, vwap_v): return None

        ce_chg = (self.ce_oi - self.ce_oi_prev) / max(self.ce_oi_prev, 1) * 100
        pe_chg = (self.pe_oi - self.pe_oi_prev) / max(self.pe_oi_prev, 1) * 100

        # Absolute OI guard: percentage change on a tiny base is meaningless.
        # A 25% spike on 10,000 OI is noise; on 5,00,000 OI it's real accumulation.
        # Minimum: CE and PE must each have ≥ 1,00,000 OI (1 lakh) for the
        # percentage change to represent genuine smart-money sizing.
        MIN_ABS_OI = 100_000
        if self.ce_oi < MIN_ABS_OI and self.pe_oi < MIN_ABS_OI:
            return None   # both sides too thin — percentage spikes are noise

        # Price must be meaningfully away from VWAP (not just touching)
        vwap_gap_pct = (close - vwap_v) / vwap_v * 100

        # 3-candle trend confirmation — last 3 closes must trend in the direction
        c1 = _c(candles,"close",-1); c2 = _c(candles,"close",-2); c3 = _c(candles,"close",-3)
        trend_up   = c3 and c2 and c1 and (c3 < c2 < c1)
        trend_down = c3 and c2 and c1 and (c3 > c2 > c1)

        # PE massive buildup + price > VWAP + supertrend UP + 3-bar uptrend = BUY CE
        if (pe_chg > 25 and vwap_gap_pct > 0.15
                and (st_dir is None or st_dir == 1)
                and trend_up and close > open_):
            return self._make_signal(Direction.BUY, close, indicators,
                f"PE OI spike +{pe_chg:.1f}% support confirmed ST+3bar")

        # CE massive buildup + price < VWAP + supertrend DOWN + 3-bar downtrend = SELL
        if (ce_chg > 25 and vwap_gap_pct < -0.15
                and (st_dir is None or st_dir == -1)
                and trend_down and close < open_):
            return self._make_signal(Direction.SELL, close, indicators,
                f"CE OI spike +{ce_chg:.1f}% resistance confirmed ST+3bar")

        # CE unwinding + price falling + ST DOWN + 3-bar downtrend = SELL
        if (ce_chg < -15 and vwap_gap_pct < -0.10
                and (st_dir is None or st_dir == -1)
                and trend_down and close < open_):
            return self._make_signal(Direction.SELL, close, indicators,
                f"CE OI unwind {ce_chg:.1f}% bearish confirmed")

        # PE unwinding + price rising + ST UP + 3-bar uptrend = BUY
        if (pe_chg < -15 and vwap_gap_pct > 0.10
                and (st_dir is None or st_dir == 1)
                and trend_up and close > open_):
            return self._make_signal(Direction.BUY, close, indicators,
                f"PE OI unwind {pe_chg:.1f}% bullish confirmed")

        return None


# ─── 10. IV Crush Play ────────────────────────────────────────────────────────

class IVCrushStrategy(BaseStrategy):
    """
    ATM straddle IV above 20% and expanding → directional momentum trade (buy CE/PE).
    ATM IV below 15% and contracting + BB squeeze → breakout play.
    Uses IV data injected from option chain snapshots.

    BB squeeze branch hardened:
      - ATR gate added: low ATR + BB squeeze can mean dead grind, not coiling.
        Without a minimum ATR the breakout candle may be a single tick anomaly.
      - Candle body confirmation (body > 40% of range, close confirms direction):
        prevents firing on doji / spinning-top candles inside the squeeze that
        look like breakouts on LTP alone but have no real directional commitment.
    """
    name = StrategyName.IV_CRUSH.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 15

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.atm_iv:      float = 0
        self.prev_atm_iv: float = 0

    def update_iv(self, atm_iv: float):
        self.prev_atm_iv = self.atm_iv; self.atm_iv = atm_iv

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if not self.atm_iv or not self.prev_atm_iv: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,0)): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        high  = _c(candles,"high");  low   = _c(candles,"low")
        vwap_v = indicators.get("vwap")
        if None in (close, open_, high, low, vwap_v): return None

        iv_rising  = self.atm_iv > self.prev_atm_iv * 1.05
        iv_falling = self.atm_iv < self.prev_atm_iv * 0.95

        # High IV expanding + trending: momentum trade.
        # Threshold 20%: Nifty ATM IV typically sits 12–20% on normal days.
        # 20%+ = genuinely elevated/event-driven volatility; options expand
        # with the move and directional buys make sense.
        if self.atm_iv > 20 and iv_rising and close > vwap_v:
            return self._make_signal(Direction.BUY, close, indicators,
                f"IV expanding={self.atm_iv:.1f}% price above VWAP — momentum CE")
        if self.atm_iv > 20 and iv_rising and close < vwap_v:
            return self._make_signal(Direction.SELL, close, indicators,
                f"IV expanding={self.atm_iv:.1f}% price below VWAP — momentum PE")

        # Low IV contracting + BB squeeze: breakout imminent.
        # ATR gate: confirms the underlying is actually capable of moving, not just
        # having a narrow band from dead grind.
        # Body confirmation: breakout candle must show directional commitment —
        # body must be > 40% of the candle range and close must confirm direction.
        if self.atm_iv < 15 and iv_falling and _atr_ok(indicators, self.underlying):
            bb_w = indicators.get("bb_width")
            if bb_w and bb_w < 0.005:
                candle_range = high - low
                body         = abs(close - open_)
                body_ratio   = body / candle_range if candle_range > 0 else 0
                if body_ratio < 0.40:
                    return None   # doji / indecision — not a real breakout candle
                direction = Direction.BUY if close > vwap_v else Direction.SELL
                # Confirm candle closes in the direction of the trade
                if direction == Direction.BUY and close < open_:
                    return None   # bearish candle in bullish breakout — skip
                if direction == Direction.SELL and close > open_:
                    return None   # bullish candle in bearish breakout — skip
                return self._make_signal(
                    direction, close, indicators,
                    f"IV crush={self.atm_iv:.1f}% + BB squeeze body={body_ratio:.2f} — breakout"
                )
        return None


# ─── 11. CPR + VWAP ───────��──────────────────────────────────────────────────

class CPRVWAPStrategy(BaseStrategy):
    """
    Central Pivot Range (CPR) confluence with VWAP.
    - Price above TC + VWAP above CPR midpoint → bullish
    - Price below BC + VWAP below CPR midpoint → bearish
    - Narrow CPR (width < 0.1% of pivot) = trending day expected
    """
    name = StrategyName.CPR_VWAP.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 30   # CPR is a daily level — once confirmed, don't re-enter all day

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._once = _OncePerDay()
        self.pivot:     float = 0
        self.bc:        float = 0
        self.tc:        float = 0
        self.cpr_width: float = 0

    def set_cpr(self, pivot: float, bc: float, tc: float):
        self.pivot = pivot; self.bc = bc; self.tc = tc
        self.cpr_width = tc - bc

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if not self.pivot: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        vwap_v = indicators.get("vwap")
        ema9   = indicators.get("ema9"); ema21 = indicators.get("ema21")
        vol    = _c(candles, "volume")
        if None in (close, open_, vwap_v): return None

        narrow_cpr = self.cpr_width < self.pivot * 0.001 if self.pivot else False

        # Volume must be above average — no low-volume CPR breakouts
        vol_ok = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.2)

        prev = _c(candles, "close", -2)

        # Bullish: FRESH close above TC (once a day) + VWAP above pivot + EMA9>EMA21 + volume
        if (not self._once.done("TC") and _crossed_above(prev, close, self.tc)
                and vwap_v > self.pivot and close > open_
                and (ema9 is None or ema21 is None or ema9 > ema21)
                and vol_ok):
            self._once.mark("TC")
            return self._make_signal(Direction.BUY, close, indicators,
                f"CPR bull: above TC={self.tc:.1f} VWAP={vwap_v:.1f} narrow={narrow_cpr}")

        # Bearish: FRESH close below BC (once a day) + VWAP below pivot + EMA9<EMA21 + volume
        if (not self._once.done("BC") and _crossed_below(prev, close, self.bc)
                and vwap_v < self.pivot and close < open_
                and (ema9 is None or ema21 is None or ema9 < ema21)
                and vol_ok):
            self._once.mark("BC")
            return self._make_signal(Direction.SELL, close, indicators,
                f"CPR bear: below BC={self.bc:.1f} VWAP={vwap_v:.1f} narrow={narrow_cpr}")

        # CPR magnet: only on narrow CPR + volume + EMA agreement
        if narrow_cpr and abs(close - self.pivot) / self.pivot < 0.001 and vol_ok:
            if close > open_ and vwap_v > self.pivot and (ema9 is None or ema21 is None or ema9 > ema21):
                return self._make_signal(Direction.BUY, close, indicators,
                    f"CPR pivot bounce bull narrow_cpr=True")
            if close < open_ and vwap_v < self.pivot and (ema9 is None or ema21 is None or ema9 < ema21):
                return self._make_signal(Direction.SELL, close, indicators,
                    f"CPR pivot bounce bear narrow_cpr=True")
        return None


# ─── 12. Gap + VWAP Fill ─────────────────────────────────────────────────────

class GapVWAPFillStrategy(BaseStrategy):
    """
    Gap up/down open + VWAP fill tendency.
    Gap up (open > prev_close * 1.003) + price starts fading below VWAP → SELL
    Gap down (open < prev_close * 0.997) + price starts recovering above VWAP → BUY
    Only valid in first 90 minutes.
    """
    name = StrategyName.GAP_VWAP_FILL.value; sl_pct = 0.15; target_pct = 0.30
    _cooldown_minutes = 20

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.prev_close: float = 0

    def set_prev_close(self, prev_close: float):
        self.prev_close = prev_close

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if not self.prev_close or len(candles) < 5: return None
        if not _is_between(_market_time(candles), dtime(9,15), dtime(10,45)): return None
        if not _atr_ok(indicators, self.underlying): return None

        # Safety: only use candles that are confirmed to be today's session.
        # If the list contains yesterday's candles (fallback path in strategy_runner),
        # candles[0] is not today's open and gap_pct would be wrong.
        # Use the ctx clock's IST trading day — server may run UTC.
        if not _is_today_ist(candles[0]):
            return None

        # Sort by timestamp so candles[0] is the earliest candle of the day
        # regardless of the order they were stored in Redis.
        sorted_candles = sorted(candles, key=lambda c: str(c.get("timestamp", "")))
        # The gap is measured from the 09:15 open. After a late start or a
        # partial seed the first cached candle is some later minute, whose open
        # is not the day's open — then the gap is unknown and nothing fires.
        first_dt = try_normalize_candle_timestamp(sorted_candles[0].get("timestamp"))
        if first_dt is None or (first_dt.hour, first_dt.minute) > (9, 16):
            return None
        day_open = _c(sorted_candles, "open", 0)   # first candle of day
        close = _c(candles,"close"); open_ = _c(candles,"open")
        vwap_v = indicators.get("vwap")
        if None in (day_open, close, open_, vwap_v): return None

        gap_pct = (day_open - self.prev_close) / self.prev_close * 100

        # Gap down + price recovering above VWAP = fill trade BUY
        if gap_pct < -0.3 and close > vwap_v and close > open_:
            return self._make_signal(Direction.BUY, close, indicators,
                f"Gap DOWN {gap_pct:.2f}% filling — price reclaimed VWAP={vwap_v:.1f}")

        # Gap up + price fading below VWAP = fill trade SELL
        if gap_pct > 0.3 and close < vwap_v and close < open_:
            return self._make_signal(Direction.SELL, close, indicators,
                f"Gap UP {gap_pct:.2f}% fading — price lost VWAP={vwap_v:.1f}")
        return None


# ─── Registry ─────────────────────────────────────────────────────────────────

def build_strategies(underlying: str = "NIFTY") -> list[BaseStrategy]:
    """Instantiate every canonical strategy for a given underlying: the 12
    legacy strategies plus the unified TREND_ENGINE. Which of them actually
    trade is decided by TradingParams.decision_engine in the strategy runner."""
    from app.strategies.trend_engine import TrendFollowingEngine
    return [
        ORBVWAPStrategy(underlying),
        VWAPPullbackStrategy(underlying),
        EMAVWAPStrategy(underlying),
        SupertrendVWAPStrategy(underlying),
        PDHPDLStrategy(underlying),
        VWAPReversalStrategy(underlying),
        BollingerBreakoutStrategy(underlying),
        RSIVWAPStrategy(underlying),
        OISpikeStrategy(underlying),
        IVCrushStrategy(underlying),
        CPRVWAPStrategy(underlying),
        GapVWAPFillStrategy(underlying),
        TrendFollowingEngine(underlying),
    ]


# Re-exported for compatibility; the canonical tuple lives in app.strategies.names.
STRATEGY_NAMES = ALL_STRATEGY_NAMES
