"""
All 12 scalping strategies.
Each inherits BaseStrategy and emits BUY (buy CE) or SELL (buy PE) signals.
"""
import logging
from typing import Optional
from datetime import datetime, time as dtime, timezone, timedelta
import numpy as np

from app.strategies.base import BaseStrategy, Direction, SignalEvent
from app.strategies.indicators import ema, rsi, bollinger, supertrend, atr

logger = logging.getLogger(__name__)

# Module-level IST timezone — avoids repeated construction inside hot paths
IST = timezone(timedelta(hours=5, minutes=30))


def _c(candles, key, idx=-1):
    try:
        return float(candles[idx][key])
    except (TypeError, ValueError, KeyError, IndexError) as exc:
        logger.debug("_c: could not read key=%r idx=%d — %s", key, idx, exc)
        return None

def _is_between(t: dtime, s: dtime, e: dtime) -> bool:
    return s <= t <= e

def _market_time(candles) -> dtime:
    ts = candles[-1].get("timestamp", "")
    try:
        dt = datetime.fromisoformat(str(ts))
        if dt.tzinfo is not None:
            dt = dt.astimezone(IST)
        # Contract: naive timestamps are assumed to be IST already.
        # If the feed ever switches to UTC-naive, every time-window gate will shift
        # by 5h30m with no error.  The tick_writer stores IST-bucketed timestamps,
        # so this assumption holds for the current pipeline.
        return dt.time()
    except Exception:
        # Fallback: extract HH:MM:SS directly from the raw string so replay works.
        try:
            time_part = str(ts)[11:19]
            return datetime.strptime(time_part, "%H:%M:%S").time()
        except Exception:
            logger.debug("_market_time: could not parse timestamp %r — falling back to now", ts)
            return datetime.now(IST).time()

def _avg_volume(candles, n=10) -> float:
    vols = [float(c.get("volume", 0)) for c in candles[-n-1:-1]]
    return float(np.mean(vols)) if vols else 1.0


def _vol_surge(vol: float, avg: float, multiplier: float = 1.5) -> bool:
    """
    Return True if vol represents a genuine surge above average.
    On NSE index spot tokens the WebSocket feed always delivers volume=0,
    so avg will also be ~0.  In that case we skip the volume gate entirely
    (return True) rather than blocking every signal.  The real liquidity
    filter lives in option_selector's hard filters (OI + volume on the
    option contract itself) so skipping the spot-volume gate is safe.
    """
    if avg <= 0:
        return True   # no volume data (NSE index feed limitation) — don't block
    return vol > multiplier * avg

def _arrays(candles):
    h = np.array([float(c.get("high",   0)) for c in candles])
    l = np.array([float(c.get("low",    0)) for c in candles])
    c = np.array([float(c.get("close",  0)) for c in candles])
    o = np.array([float(c.get("open",   0)) for c in candles])
    v = np.array([float(c.get("volume", 0)) for c in candles])   # 0 matches _c/_avg_volume
    return h, l, c, o, v


def _atr_ok(indicators: dict, underlying: str) -> bool:
    """
    Return True if the 1-min ATR is high enough to make directional
    option trades worthwhile.

    Thresholds (points on the underlying):
      NIFTY     : ATR ≥ 3.0   (below = dead chop, options go nowhere)
      BANKNIFTY : ATR ≥ 10.0  (BN moves ~3× Nifty per point)

    These are tighter than the global ATR gate in strategy_runner
    (1.5 / 5.0) because here we want a MINIMUM bar for option entries,
    not just "the market is moving at all".
    """
    atr = indicators.get("atr") or 0.0
    if underlying == "BANKNIFTY":
        return atr >= 10.0
    return atr >= 3.0


# ─── 1. ORB + VWAP + Volume ──────────────────────────────────────────────────

class ORBVWAPStrategy(BaseStrategy):
    """9:15-9:30 opening range breakout + VWAP confirm + volume surge."""
    name = "ORB_VWAP"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 20: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(11,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); vol = _c(candles,"volume")
        vwap_v = indicators.get("vwap")
        orb_h = indicators.get("orb_high"); orb_l = indicators.get("orb_low")
        if None in (close, vol, vwap_v, orb_h, orb_l): return None
        # Don't trade on a half-built opening range (before 9:30)
        if not indicators.get("orb_ready", True): return None

        vol_surge = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.5)
        if close > orb_h and close > vwap_v and vol_surge:
            return self._make_signal(Direction.BUY, close, indicators,
                f"ORB UP close={close:.1f} orb_h={orb_h:.1f} vwap={vwap_v:.1f}")
        if close < orb_l and close < vwap_v and vol_surge:
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
    name = "VWAP_PULLBACK"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
    _cooldown_minutes = 15

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 10: return None
        if not _is_between(_market_time(candles), dtime(9,45), dtime(14,0)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        low = _c(candles,"low");     high  = _c(candles,"high")
        vwap_v = indicators.get("vwap")
        if None in (close, open_, low, high, vwap_v): return None

        # Derive today's date from the candle data, not wall-clock IST.
        # Wall-clock breaks replay/backtesting against historical candles.
        today_str = str(candles[-1].get("timestamp", ""))[:10]
        today_window = [
            c for c in candles[-7:-1]
            if str(c.get("timestamp", "")).startswith(today_str)
        ]
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
    name = "EMA_VWAP"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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
        if None in (close, vol, vwap_v): return None

        # Volume surge on crossover candle — confirms real participation, not drift
        vol_surge = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.3)

        _, _, c_arr, _, _ = _arrays(candles)
        e9_now  = indicators.get("ema9")
        e21_now = indicators.get("ema21")
        if e9_now is None or e21_now is None: return None   # warm-up not complete

        e9_full  = ema(c_arr, 9)
        e21_full = ema(c_arr, 21)
        if np.isnan(e9_full[-2]) or np.isnan(e21_full[-2]): return None

        # Use the locally-computed arrays for both bars to avoid boundary
        # disagreement between e9_now (from compute_all's candle set) and
        # e9_full[-1] (recomputed here on the same candle list).
        cross_up   = e9_full[-2] <= e21_full[-2] and e9_full[-1] > e21_full[-1]
        cross_down = e9_full[-2] >= e21_full[-2] and e9_full[-1] < e21_full[-1]

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
    name = "SUPERTREND_VWAP"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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

        if flip_up and close > vwap_v and ema9 is not None and ema21 is not None and ema9 > ema21:
            return self._make_signal(Direction.BUY, close, indicators,
                "Supertrend flip UP + VWAP + EMA bull")
        if flip_down and close < vwap_v and ema9 is not None and ema21 is not None and ema9 < ema21:
            return self._make_signal(Direction.SELL, close, indicators,
                "Supertrend flip DOWN + VWAP + EMA bear")
        return None


# ─── 5. Previous Day High/Low ─────────────────────────────────────────────────

class PDHPDLStrategy(BaseStrategy):
    """Clean breakout above PDH or below PDL with volume confirmation."""
    name = "PDH_PDL"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.pdh: Optional[float] = None
        self.pdl: Optional[float] = None

    def set_levels(self, pdh: float, pdl: float):
        self.pdh = pdh; self.pdl = pdl

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 5 or not self.pdh or not self.pdl: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,0)): return None
        if not _atr_ok(indicators, self.underlying): return None

        # On expiry day, PDH/PDL breakouts are extremely unreliable:
        # institutional covering and pin-risk cause violent reversals.
        # Allow only the first 90 minutes (09:30–11:00) when the move
        # still has genuine momentum; block entirely after 11:00.
        if indicators.get("is_expiry_day"):
            if not _is_between(_market_time(candles), dtime(9, 30), dtime(11, 0)):
                return None

        c1 = _c(candles,"close",-1); c2 = _c(candles,"close",-2)
        v1 = _c(candles,"volume",-1)
        if None in (c1, c2, v1): return None
        vol_ok = _vol_surge(v1, indicators.get("avg_volume") or _avg_volume(candles), 1.2)

        if c1 > self.pdh and c2 > self.pdh and vol_ok:
            return self._make_signal(Direction.BUY, c1, indicators,
                f"PDH break {c1:.1f} > {self.pdh:.1f}")
        if c1 < self.pdl and c2 < self.pdl and vol_ok:
            return self._make_signal(Direction.SELL, c1, indicators,
                f"PDL break {c1:.1f} < {self.pdl:.1f}")
        return None


# ─── 6. VWAP Reversal ────────────────────────────────────────────────────────

class VWAPReversalStrategy(BaseStrategy):
    """Price over-extended from VWAP + reversal candle pattern."""
    name = "VWAP_REVERSAL"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
    _cooldown_minutes = 10

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if len(candles) < 5: return None
        if not _is_between(_market_time(candles), dtime(10,0), dtime(14,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        high  = _c(candles,"high");  low   = _c(candles,"low")
        vwap_v = indicators.get("vwap"); rsi_v = indicators.get("rsi")
        if None in (close, open_, high, low, vwap_v): return None

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
    name = "BOLLINGER_BREAKOUT"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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
        # Index the raw width array directly — no NaN-stripping — so [-2] is
        # guaranteed to be the previous candle bar, not some older valid bar.
        # With a 20-period BB all NaNs are leading, so [-1] and [-2] are always
        # valid for len >= 25 (already guarded above).  A mid-series NaN from a
        # bad tick would now return None rather than silently drifting.
        if np.isnan(bb_all.width[-1]) or np.isnan(bb_all.width[-2]): return None
        prev_bb_w = float(bb_all.width[-2])
        # avg_w: exclude the last 2 bars (current breakout + prev squeeze bar)
        # so the squeeze baseline isn't polluted by the bars being evaluated.
        width_arr = bb_all.width
        avg_slice = width_arr[-22:-2]
        avg_slice = avg_slice[~np.isnan(avg_slice)]
        avg_w = float(np.mean(avg_slice)) if len(avg_slice) >= 3 else 0.0
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
    name = "RSI_VWAP"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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
        r_arr = rsi(c_arr, 14)
        # Use raw array indices for [-2] and [-3] — same candle alignment as [-1].
        # NaN-stripping would make [-2] "the last valid bar" which drifts if any
        # intermediate candle produces a NaN.
        if np.isnan(r_arr[-2]) or np.isnan(r_arr[-3]): return None
        rsi_cur   = rsi_v              # from indicators — authoritative latest
        rsi_prev  = float(r_arr[-2])
        rsi_prev2 = float(r_arr[-3])

        # Momentum BUY: RSI rising out of oversold territory (was < 40, now ≥ 45),
        # price above VWAP but NOT hugging it (away from the 0.3% band),
        # bullish candle.  Catches early-trend entries where momentum is building.
        # Requires rsi_cur >= 45 — a bar at 40.1 up from 40.0 is not a recovery.
        if (45 <= rsi_cur <= 60
                and rsi_prev2 < 40
                and rsi_cur > rsi_prev
                and not near_vwap
                and close > vwap_v and close > open_):
            return self._make_signal(Direction.BUY, close, indicators,
                f"RSI momentum recovery={rsi_cur:.1f} rising from oversold above VWAP")

        # Momentum SELL: RSI falling from overbought territory (was > 65, now 40–60),
        # price below VWAP but NOT hugging it, bearish candle.
        if (40 <= rsi_cur <= 60
                and rsi_prev2 > 65
                and rsi_cur < rsi_prev
                and not near_vwap
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
    name = "OI_SPIKE"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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
        # Skip zero-value updates (empty chain response from poller).
        # If we stored 0/0 as ce_oi_prev, the next real snapshot computes
        # chg = real_oi / max(0,1) * 100 which is astronomically large → fake spike.
        if ce_oi == 0 and pe_oi == 0:
            return
        self.ce_oi_prev = self.ce_oi; self.pe_oi_prev = self.pe_oi
        self.ce_oi = ce_oi;           self.pe_oi = pe_oi
        self._update_count += 1

    def reset_daily(self):
        self.ce_oi_prev = 0
        self.pe_oi_prev = 0
        self.ce_oi = 0
        self.pe_oi = 0
        self._update_count = 0

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        # Block entirely on expiry day — OI rolls, unwinding, and re-hedging
        # create massive fake spike signals all afternoon.
        if self.is_expiry_day: return None

        # Need at least 2 real OI snapshots and enough candles for trend check
        if len(candles) < 8 or self._update_count < 2: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(12,30)): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        vwap_v = indicators.get("vwap")
        st_dir = indicators.get("supertrend_dir")   # 1=UP, -1=DOWN, 0=warm-up
        if None in (close, open_, vwap_v): return None
        if st_dir == 0: st_dir = None   # treat warm-up as no supertrend signal

        ce_chg = (self.ce_oi - self.ce_oi_prev) / max(self.ce_oi_prev, 1) * 100
        pe_chg = (self.pe_oi - self.pe_oi_prev) / max(self.pe_oi_prev, 1) * 100

        # Absolute OI guard — check the side being acted on, not both together.
        # A 25% CE spike on ce_oi=20k with pe_oi=5L would pass the old "both < 1L"
        # guard and fire a SELL on noise. Now each signal checks its own side.
        MIN_ABS_OI = 100_000

        # Price must be meaningfully away from VWAP (not just touching)
        vwap_gap_pct = (close - vwap_v) / vwap_v * 100

        # 3-candle trend confirmation — last 3 closes must trend in the direction
        c1 = _c(candles,"close",-1); c2 = _c(candles,"close",-2); c3 = _c(candles,"close",-3)
        trend_up   = c3 and c2 and c1 and (c3 < c2 < c1)
        trend_down = c3 and c2 and c1 and (c3 > c2 > c1)

        # PE massive buildup + price > VWAP + supertrend UP + 3-bar uptrend = BUY CE
        if (pe_chg > 25 and vwap_gap_pct > 0.15
                and (st_dir is None or st_dir == 1)
                and trend_up and close > open_
                and self.pe_oi >= MIN_ABS_OI):   # guard: spike on own side only
            return self._make_signal(Direction.BUY, close, indicators,
                f"PE OI spike +{pe_chg:.1f}% support confirmed ST+3bar")

        # CE massive buildup + price < VWAP + supertrend DOWN + 3-bar downtrend = SELL
        if (ce_chg > 25 and vwap_gap_pct < -0.15
                and (st_dir is None or st_dir == -1)
                and trend_down and close < open_
                and self.ce_oi >= MIN_ABS_OI):   # guard: spike on own side only
            return self._make_signal(Direction.SELL, close, indicators,
                f"CE OI spike +{ce_chg:.1f}% resistance confirmed ST+3bar")

        # CE unwinding + price falling + ST DOWN + 3-bar downtrend = SELL
        if (ce_chg < -15 and vwap_gap_pct < -0.10
                and (st_dir is None or st_dir == -1)
                and trend_down and close < open_
                and self.ce_oi_prev >= MIN_ABS_OI):   # check pre-unwind base, not post-unwind current
            return self._make_signal(Direction.SELL, close, indicators,
                f"CE OI unwind {ce_chg:.1f}% bearish confirmed")

        # PE unwinding + price rising + ST UP + 3-bar uptrend = BUY
        if (pe_chg < -15 and vwap_gap_pct > 0.10
                and (st_dir is None or st_dir == 1)
                and trend_up and close > open_
                and self.pe_oi_prev >= MIN_ABS_OI):   # check pre-unwind base, not post-unwind current
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
    name = "IV_CRUSH"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
    _cooldown_minutes = 15

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.atm_iv:        float = 0
        self.prev_atm_iv:   float = 0
        self._iv_updated_at: Optional[datetime] = None   # UTC time of last update
        self.is_expiry_day: bool  = False   # set by strategy_runner, same as OISpikeStrategy

    def update_iv(self, atm_iv: float):
        self.prev_atm_iv    = self.atm_iv
        self.atm_iv         = atm_iv
        self._iv_updated_at = datetime.now(timezone.utc)

    def reset_daily(self):
        self.atm_iv = 0
        self.prev_atm_iv = 0
        self._iv_updated_at = None

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if not self.atm_iv or not self.prev_atm_iv: return None
        if len(candles) < 5: return None
        # Block on expiry day — IV collapses mechanically into expiry; the
        # "IV crush + BB squeeze" branch fires spuriously all afternoon.
        if self.is_expiry_day: return None
        # Staleness guard: if the option-chain poller stalled, atm_iv/prev_atm_iv
        # are frozen — trading on stale IV is dangerous.
        if self._iv_updated_at is not None:
            stale_secs = (datetime.now(timezone.utc) - self._iv_updated_at).total_seconds()
            if stale_secs > 300:   # > 5 minutes since last real IV update
                return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,0)): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        high  = _c(candles,"high");  low   = _c(candles,"low")
        vwap_v = indicators.get("vwap")
        if None in (close, open_, high, low, vwap_v): return None

        iv_rising  = self.atm_iv > self.prev_atm_iv * 1.05
        iv_falling = self.atm_iv < self.prev_atm_iv * 0.95

        candle_range = high - low
        body         = abs(close - open_)
        body_ratio   = body / candle_range if candle_range > 0 else 0

        # High IV expanding + trending: momentum trade.
        # Body confirmation: require directional candle (body > 35% of range)
        # so a spinning-top or doji above/below VWAP doesn't trigger the trade.
        if self.atm_iv > 20 and iv_rising and close > vwap_v:
            if close < open_ or body_ratio < 0.35:   # bearish or indecision candle
                return None
            return self._make_signal(Direction.BUY, close, indicators,
                f"IV expanding={self.atm_iv:.1f}% price above VWAP — momentum CE")
        if self.atm_iv > 20 and iv_rising and close < vwap_v:
            if close > open_ or body_ratio < 0.35:   # bullish or indecision candle
                return None
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


# ─── 11. CPR + VWAP ──────────────────────────────────────────────────────────

class CPRVWAPStrategy(BaseStrategy):
    """
    Central Pivot Range (CPR) confluence with VWAP.
    - Price above TC + VWAP above CPR midpoint → bullish
    - Price below BC + VWAP below CPR midpoint → bearish
    - Narrow CPR (width < 0.1% of pivot) = trending day expected
    """
    name = "CPR_VWAP"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
    _cooldown_minutes = 30   # CPR is a daily level — once confirmed, don't re-enter all day

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.pivot:     float = 0
        self.bc:        float = 0
        self.tc:        float = 0
        self.cpr_width: float = 0

    def set_cpr(self, pivot: float, bc: float, tc: float):
        self.pivot = pivot; self.bc = bc; self.tc = tc
        self.cpr_width = tc - bc

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if not self.pivot: return None
        if len(candles) < 5: return None
        if not _is_between(_market_time(candles), dtime(9,30), dtime(13,30)): return None
        if not _atr_ok(indicators, self.underlying): return None

        close = _c(candles,"close"); open_ = _c(candles,"open")
        vwap_v = indicators.get("vwap")
        ema9   = indicators.get("ema9"); ema21 = indicators.get("ema21")
        vol    = _c(candles, "volume")
        if None in (close, open_, vol, vwap_v): return None

        narrow_cpr = self.cpr_width < self.pivot * 0.001 if self.pivot else False

        # Volume must be above average — no low-volume CPR breakouts
        vol_ok = _vol_surge(vol, indicators.get("avg_volume") or _avg_volume(candles), 1.2)

        # Bullish: price above TC + VWAP above pivot + EMA9>EMA21 + volume surge
        if (close > self.tc and vwap_v > self.pivot and close > open_
                and (ema9 is None or ema21 is None or ema9 > ema21)
                and vol_ok):
            return self._make_signal(Direction.BUY, close, indicators,
                f"CPR bull: above TC={self.tc:.1f} VWAP={vwap_v:.1f} narrow={narrow_cpr}")

        # Bearish: price below BC + VWAP below pivot + EMA9<EMA21 + volume surge
        if (close < self.bc and vwap_v < self.pivot and close < open_
                and (ema9 is None or ema21 is None or ema9 < ema21)
                and vol_ok):
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
    name = "GAP_VWAP_FILL"; sl_pct = 0.15; target_pct = 0.30; min_rr = 2.0
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

        # Sort first so candles[0] is chronologically earliest regardless of
        # storage order, then derive today_str from the data (not wall-clock IST)
        # so the guard works correctly during replay/backtesting.
        sorted_candles = sorted(candles, key=lambda c: str(c.get("timestamp", "")))
        today_str = str(sorted_candles[0].get("timestamp", ""))[:10]
        # Reject if the last candle is from a different day (stale multi-day data)
        if not str(sorted_candles[-1].get("timestamp", "")).startswith(today_str):
            return None
        # Require the first candle to be the true market open (≤ 9:16 IST).
        # If the runner supplies candles starting mid-session (late start / reconnect)
        # the "day_open" is not the real open and gap_pct will be silently wrong.
        first_time = _market_time([sorted_candles[0]])
        if first_time > dtime(9, 16):
            return None
        day_open = _c(sorted_candles, "open", 0)   # first candle of day
        close = _c(sorted_candles, "close", -1)    # last candle (sorted)
        open_ = _c(sorted_candles, "open",  -1)    # last candle (sorted)
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
    """Instantiate all 12 strategies for a given underlying."""
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
    ]


STRATEGY_NAMES = [
    cls.name for cls in (
        ORBVWAPStrategy, VWAPPullbackStrategy, EMAVWAPStrategy,
        SupertrendVWAPStrategy, PDHPDLStrategy, VWAPReversalStrategy,
        BollingerBreakoutStrategy, RSIVWAPStrategy, OISpikeStrategy,
        IVCrushStrategy, CPRVWAPStrategy, GapVWAPFillStrategy,
    )
]
