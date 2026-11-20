"""
The four candidates pre-registered in docs/NEW_STRATEGY_PREREGISTRATION.md.

They are written exactly like the live strategies (subclasses of the live
_PlanStrategy, signals with an ExitPlan, bar exits in ``candle_exit``) so a
candidate that passes can move into app/strategies/strategies.py unchanged.
Until then nothing here is registered in build_strategies. CPR_VWAP (puts
only) and the shared once-a-day / 5-min trail bases have moved there, as a
paper forward test (docs/SYSTEM_EDGE_SCORECARD.md).

Common trail (VOL_SQUEEZE_BREAKOUT, TREND_DAY_CONTINUATION): after each
completed 5-min bar, once the 5-min Supertrend(10, 3) points the trade's way
the stop trails its line (only tightening); a later bar closing with the
Supertrend turned against the trade exits. A trade entered while the
Supertrend still points the other way keeps its structural stop until the
line turns with it (a close "across" a line needs the trade to be on its right
side first). The arming is recorded in ``plan.breakeven_done``, which these
plans never use for a breakeven (they have no breakeven_trigger).
"""
from __future__ import annotations

from datetime import time as dtime
from typing import Optional

import numpy as np

from app.runtime.context import trading_day
from app.services.trading import exit_plan as xp
from app.services.trading.exit_plan import ExitPlan
from app.strategies.base import Direction
from app.strategies.indicators import atr, ema, resample, supertrend
from app.strategies.strategies import (  # noqa: F401  CPRVWAP: re-exported for the research CLI
    SIDES, CPRVWAP, _OncePerDay, _PlanStrategy, _Trail5, _arr, _in_windows, _new_bars, _session_vwap_at, _t,
)




class VolSqueezeBreakout(_Trail5):
    name = "VOL_SQUEEZE_BREAKOUT"
    BOX, LOOKBACK = 12, 88
    PCTL, EXPANSION = 25.0, 1.5
    WINDOW = (dtime(9, 45), dtime(14, 0))

    async def evaluate(self, candles, indicators):
        bars = self._ready(candles)
        if bars is None or len(bars) < self.BOX + self.LOOKBACK + 1:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        t = len(bars) - 1
        rng = _arr(bars, "high") - _arr(bars, "low")
        box = bars[t - self.BOX:t]
        mean_box = float(rng[t - self.BOX:t].mean())
        ref = [float(rng[j:j + self.BOX].mean()) for j in range(t - self.BOX - self.LOOKBACK, t - self.BOX)]
        if mean_box <= 0 or mean_box > np.percentile(ref, self.PCTL):
            return None
        if rng[t] < self.EXPANSION * mean_box:
            return None
        hi, lo = max(b["high"] for b in box), min(b["low"] for b in box)
        mid, c = round((hi + lo) / 2, 2), last["close"]
        for side, sgn, direction in SIDES:
            level = hi if sgn > 0 else lo
            if sgn * (c - level) > 0:
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=mid)
                return self._plan_signal(direction, c, indicators,
                                         f"squeeze box {lo:.1f}-{hi:.1f} broken {side}, close {c:.1f}", plan)
        return None


class TrendDayContinuation(_Trail5):
    name = "TREND_DAY_CONTINUATION"
    MIN_DAY_MOVE = 0.005
    VWAP_BARS = 6
    TOUCH_ATR, STOP_ATR = 0.25, 0.25
    WINDOW = (dtime(11, 0), dtime(13, 30))

    async def evaluate(self, candles, indicators):
        bars = self._ready(candles)
        if bars is None or len(bars) < 40:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        today_open = float(candles[0]["open"])
        c = last["close"]
        move = (c - today_open) / today_open
        if abs(move) < self.MIN_DAY_MOVE:
            return None
        sgn = 1 if move > 0 else -1
        side, direction = ("BUY", Direction.BUY) if sgn > 0 else ("SELL", Direction.SELL)
        vw = _session_vwap_at(candles)
        recent = bars[-self.VWAP_BARS:]
        if any(vw.get(b["end"]) is None or not sgn * (b["close"] - vw[b["end"]]) > 0 for b in recent):
            return None
        h, l, cl = _arr(bars, "high"), _arr(bars, "low"), _arr(bars, "close")
        a, e20 = atr(h, l, cl, 14), ema(cl, 20)
        t = len(bars) - 1
        if not (np.isfinite(a[t]) and np.isfinite(e20[t - 1])):
            return None
        prev = bars[t - 1]
        near = prev["low"] if sgn > 0 else prev["high"]
        touched = sgn * (near - e20[t - 1]) <= self.TOUCH_ATR * a[t]
        trigger = sgn * (c - (prev["high"] if sgn > 0 else prev["low"])) > 0
        if not (touched and trigger):
            return None
        stop = round(near - sgn * self.STOP_ATR * float(a[t]), 2)
        plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=stop)
        return self._plan_signal(direction, c, indicators,
                                 f"trend day {move * 100:+.2f}%, pullback to EMA20 {e20[t - 1]:.1f}, "
                                 f"close {c:.1f}", plan)


class GapFade(_OncePerDay):
    name = "GAP_FADE"
    MIN_GAP, MAX_GAP = 0.003, 0.010
    WINDOW = (dtime(9, 35), dtime(10, 30))          # bar END times 09:35-10:30
    EXIT_AT = "14:45"
    NEEDS_PREV_SESSION = True

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._levels_day, self._prev = None, None

    def set_prev_session(self, for_day, levels, reason: Optional[str] = None) -> None:
        self._levels_day, self._prev = for_day, levels

    def prev_session_problem(self) -> Optional[str]:
        if self._levels_day != trading_day() or self._prev is None:
            return "previous-session close not resolved for today"
        return None

    async def evaluate(self, candles, indicators):
        bars = resample(candles, 5)                        # today only
        if len(bars) < 4 or not self._new_bar(bars) or self.open_positions \
                or self._traded_day == trading_day() or self.prev_session_problem():
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)) or _t(bars[0]["start"]) != dtime(9, 15):
            return None
        pc = float(self._prev.close)
        gap = (float(candles[0]["open"]) - pc) / pc
        if not (self.MIN_GAP <= abs(gap) <= self.MAX_GAP):
            return None
        opening = bars[:3]                                  # 09:15-09:30
        o_hi, o_lo = max(b["high"] for b in opening), min(b["low"] for b in opening)
        c = last["close"]
        sgn = -1 if gap > 0 else 1                          # trade toward the previous close
        level, stop = (o_lo, o_hi) if sgn < 0 else (o_hi, o_lo)
        if not sgn * (c - level) > 0 or not sgn * (pc - c) > 0:
            return None
        side, direction = ("BUY", Direction.BUY) if sgn > 0 else ("SELL", Direction.SELL)
        plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                        spot_target=round(pc, 2), exit_at=self.EXIT_AT)
        return self._plan_signal(direction, c, indicators,
                                 f"gap {gap * 100:+.2f}% fading, close {c:.1f} beyond opening "
                                 f"range {o_lo:.1f}-{o_hi:.1f}, target prev close {pc:.1f}", plan)


class VWAPExtremeReversion(_OncePerDay):
    name = "VWAP_EXTREME_REVERSION"
    STRETCH_ATR, STOP_ATR, ADX_MAX = 2.5, 0.5, 20.0
    WINDOW = (dtime(10, 0), dtime(14, 30))
    EXIT_AT = "14:45"

    async def evaluate(self, candles, indicators):
        bars = self._ready(candles)
        if bars is None or len(bars) < 40:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        from app.services.trading.regime_indicators import compute_adx
        h, l, cl = _arr(bars, "high"), _arr(bars, "low"), _arr(bars, "close")
        adx = compute_adx(h, l, cl, 14)
        a = atr(h, l, cl, 14)
        t = len(bars) - 1
        if adx is None or adx >= self.ADX_MAX or not np.isfinite(a[t]) or a[t] <= 0:
            return None
        vw = _session_vwap_at(candles)
        prev = bars[t - 1]
        v_prev, v_now = vw.get(prev["end"]), vw.get(last["end"])
        if v_prev is None or v_now is None:
            return None
        stretch = (prev["close"] - v_prev) / a[t]
        if abs(stretch) < self.STRETCH_ATR:
            return None
        sgn = -1 if stretch > 0 else 1                      # trade back toward VWAP
        mid = (prev["high"] + prev["low"]) / 2
        c = last["close"]
        if not sgn * (c - mid) > 0 or not sgn * (v_now - c) > 0:
            return None
        ext = max(prev["high"], last["high"]) if sgn < 0 else min(prev["low"], last["low"])
        stop = round(ext - sgn * self.STOP_ATR * float(a[t]), 2)
        side, direction = ("BUY", Direction.BUY) if sgn > 0 else ("SELL", Direction.SELL)
        plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=stop,
                        spot_target=round(v_now, 2), exit_at=self.EXIT_AT)
        return self._plan_signal(direction, c, indicators,
                                 f"{stretch:+.1f} ATR from VWAP {v_now:.1f}, ADX {adx:.1f}, "
                                 f"reverting, close {c:.1f}", plan)


# ── Round 2 (docs/NEW_STRATEGY_PREREGISTRATION.md) ────────────────────────────

class BollingerSqueezeBreakout(_Trail5):
    name = "BOLLINGER_SQUEEZE_BREAKOUT"
    PERIOD, MULT = 20, 2.0
    LOOKBACK, PCTL = 88, 25.0
    WINDOW = (dtime(9, 45), dtime(14, 0))

    async def evaluate(self, candles, indicators):
        bars = self._ready(candles)
        if bars is None or len(bars) < self.PERIOD + self.LOOKBACK + 1:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        cl = _arr(bars, "close")
        t = len(bars) - 1

        def band(i):                      # (lower, middle, upper) of the 20 closes ending at i
            w = cl[i - self.PERIOD + 1:i + 1]
            m, sd = float(w.mean()), float(w.std())
            return m - self.MULT * sd, m, m + self.MULT * sd

        widths = []
        for i in range(t - self.LOOKBACK - 1, t):           # previous bar and the 88 before it
            lo, mid, up = band(i)
            widths.append((up - lo) / mid if mid else 0.0)
        prev_w, ref = widths[-1], widths[:-1]
        if prev_w > np.percentile(ref, self.PCTL):
            return None
        lo, mid, up = band(t)
        c = last["close"]
        for side, sgn, direction in SIDES:
            if sgn * (c - (up if sgn > 0 else lo)) > 0:
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(mid, 2))
                return self._plan_signal(direction, c, indicators,
                                         f"Bollinger squeeze breakout {side}, close {c:.1f}, "
                                         f"band {lo:.1f}-{up:.1f}", plan)
        return None
