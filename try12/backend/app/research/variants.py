"""
Research variants of the five enabled strategies (Part 5). Each subclasses the
live class, keeps its ``name`` (the live identity) and adds ``VARIANT``. None
of them is registered in build_strategies: they exist only for the harness.

Filter variants call the live ``evaluate`` and drop a signal whose trigger
bar fails the filter. That is exactly what a filter inside the live rule would
do, because none of these strategies changes its day state when it emits a
signal (state changes on on_position_opened). New-entry variants (retest,
sweep, 3-min execution) carry their own small state machine.

Thresholds are constructor arguments. The CLI sets them from medians of the
baseline signals in the DESIGN window only (the first 12 months), and the
walk-forward scores only test months after it, so no threshold sees the data
it is judged on.
"""
from __future__ import annotations

from datetime import time as dtime
from typing import Optional

import numpy as np

from app.services.trading import exit_plan as xp
from app.services.trading.exit_plan import ExitPlan
from app.strategies.base import Direction
from app.strategies.indicators import atr, ema, resample, rsi, supertrend, vwap
from app.strategies.strategies import (
    EMARSIPullback15mStrategy, FirstCandleBreakoutStrategy, ORBBreakoutStrategy,
    PivotBounceStrategy, SupertrendEMAStrategy, _in_windows, _t,
)
from app.runtime.context import trading_day


def bar_shape(bar: dict, sign: int) -> dict:
    """Body / range, where the close sits in the range toward the trade (1 =
    at the favourable extreme), and the range."""
    rng = bar["high"] - bar["low"]
    if rng <= 0:
        return {"body_ratio": 0.0, "close_loc": 0.5, "range": 0.0}
    loc = (bar["close"] - bar["low"]) / rng
    return {"body_ratio": abs(bar["close"] - bar["open"]) / rng,
            "close_loc": loc if sign > 0 else 1 - loc, "range": rng}


def _sign(sig) -> int:
    return 1 if sig.direction == Direction.BUY else -1


def fut_rel_volume(fut_candles: list[dict], bar: dict, minutes: int) -> Optional[float]:
    """Futures volume of *bar* / mean of the earlier bars that day (>= 3), or None."""
    fut = resample(fut_candles, minutes) if fut_candles else []
    cur = next((b for b in fut if b["start"] == bar["start"]), None)
    prior = [b["volume"] for b in fut if b["start"] < bar["start"]
             and b["start"].date() == bar["start"].date()]
    if cur is None or len(prior) < 3 or not cur["volume"] or np.mean(prior) <= 0:
        return None
    return float(cur["volume"] / np.mean(prior))


def session_vwap(candles: list[dict], fut_candles: list[dict]) -> Optional[float]:
    """Today's VWAP of the spot typical price weighted by futures volume per
    minute (the index has no volume); equal weights where futures are missing."""
    if not candles:
        return None
    vol = {c["timestamp"]: c["volume"] for c in fut_candles}
    h = np.array([c["high"] for c in candles]); l = np.array([c["low"] for c in candles])
    cl = np.array([c["close"] for c in candles])
    v = np.array([vol.get(c["timestamp"], 0.0) for c in candles])
    return float(vwap(h, l, cl, v)[-1])


# ── 1. FIRST_CANDLE_BREAKOUT ─────────────────────────────────────────────────

class FCBStrong(FirstCandleBreakoutStrategy):
    VARIANT = "FCB_STRONG"

    def __init__(self, body_min: float, close_loc_min: float, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.body_min, self.close_loc_min = body_min, close_loc_min

    async def evaluate(self, candles, indicators):
        sig = await super().evaluate(candles, indicators)
        if sig is None:
            return None
        shp = bar_shape(resample(candles, 5)[-1], _sign(sig))
        if shp["body_ratio"] < self.body_min or shp["close_loc"] < self.close_loc_min:
            return None
        return sig


class FCBRetest(FirstCandleBreakoutStrategy):
    """Close beyond the 09:15 candle ± buffer (the live break), then a later
    5-min bar trades back to within the buffer of the broken level and closes
    outside it again (the retest), then the next bar closes beyond the retest
    bar's extreme (continuation). A close back inside the 09:15 candle
    cancels. Stop: the retest bar's far extreme (capped at MAX_STOP_PTS).
    Target 1.5 × the 09:15 range. Signals by 10:30."""
    VARIANT = "FCB_RETEST"
    LAST_BAR_END = dtime(10, 30)

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._state: dict = {}

    async def evaluate(self, candles, indicators):
        if self._roll_day():
            self._taken, self._state = set(), {}
        bars = resample(candles, 5)
        if len(bars) < 2 or not self._new_bar(bars) or self.open_positions:
            return None
        first, last = bars[0], bars[-1]
        if _t(first["start"]) != dtime(9, 15) or _t(last["end"]) > self.LAST_BAR_END:
            return None
        hi, lo = first["high"], first["low"]
        rng = hi - lo
        if rng <= 0:
            return None
        for side, sgn, direction, level, inside in (("BUY", 1, Direction.BUY, hi, lo),
                                                    ("SELL", -1, Direction.SELL, lo, hi)):
            if direction in self._taken:
                continue
            st = self._state.get(side)
            c = last["close"]
            if st is None:
                if sgn * (c - (level + sgn * self.BUFFER_PTS)) > 0:
                    self._state[side] = {"phase": "broken"}
                continue
            if sgn * (c - level) < 0:                      # closed back through the level
                self._state[side] = None if sgn * (c - inside) > 0 else {"phase": "dead"}
                continue
            if st["phase"] == "broken":
                near = last["low"] if sgn > 0 else last["high"]
                if sgn * (near - (level + sgn * self.BUFFER_PTS)) <= 0:
                    st.update(phase="retest", ext=last["high"] if sgn > 0 else last["low"],
                              far=near)
            elif st["phase"] == "retest":
                if sgn * (c - st["ext"]) > 0:
                    stop = st["far"] if sgn > 0 else st["far"]
                    stop = max(stop, c - self.MAX_STOP_PTS) if sgn > 0 else min(stop, c + self.MAX_STOP_PTS)
                    st["phase"] = "dead"
                    plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                                    spot_target=round(c + sgn * self.TARGET_RANGE_MULT * rng, 2),
                                    exit_at=self.EXIT_AT)
                    return self._plan_signal(direction, c, indicators,
                                             f"09:15 break-retest-go {side} close={c:.1f}", plan)
                far = last["low"] if sgn > 0 else last["high"]
                if sgn * (far - st["far"]) < 0:
                    st["far"] = far                       # a deeper retest: stop follows it
        return None


# ── 2. ORB_BREAKOUT ──────────────────────────────────────────────────────────

class ORBFilter(ORBBreakoutStrategy):
    """Live ORB plus one trigger-bar filter: displacement (body / OR width),
    futures relative volume (required, not skipped when missing) or VWAP side."""

    def __init__(self, variant: str, threshold: float = 0.0, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.VARIANT, self.threshold = variant, threshold

    async def evaluate(self, candles, indicators):
        sig = await super().evaluate(candles, indicators)
        if sig is None:
            return None
        s = _sign(sig)
        bar = resample(candles, 5)[-1]
        if self.VARIANT == "ORB_DISPLACEMENT":
            hi, lo = self.opening_range(candles)
            if abs(bar["close"] - bar["open"]) / (hi - lo) < self.threshold:
                return None
        elif self.VARIANT == "ORB_VOLUME":
            rv = fut_rel_volume(self.futures_candles, bar, 5)
            if rv is None or rv < self.threshold:
                return None
        elif self.VARIANT == "ORB_VWAP":
            vw = session_vwap(candles, self.futures_candles)
            if vw is None or s * (bar["close"] - vw) <= 0:
                return None
        return sig


class ORBRetest(ORBBreakoutStrategy):
    """A 5-min close beyond the OR (no volume rule), a retest of the broken
    level, then a close beyond the retest bar's extreme. Stop: the retest
    bar's far extreme. Live target / breakeven / 90-min time stop. By 11:00."""
    VARIANT = "ORB_RETEST"

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._state: dict = {}

    async def evaluate(self, candles, indicators):
        if self._roll_day():
            self._trades, self._state = [], {}
        bars = resample(candles, 5)
        if len(bars) < 4 or not self._new_bar(bars):
            return None
        last = bars[-1]
        if _t(last["start"]) < self.OR_END or _t(last["end"]) > self.LAST_ENTRY or self.open_positions:
            return None
        rng = self.opening_range(candles)
        if rng is None:
            return None
        or_high, or_low = rng
        width = or_high - or_low
        if not (self.MIN_WIDTH <= width <= self.MAX_WIDTH):
            return None
        c = last["close"]
        for side, sgn, direction, level in (("BUY", 1, Direction.BUY, or_high),
                                            ("SELL", -1, Direction.SELL, or_low)):
            if not self._allowed(direction):
                continue
            st = self._state.get(side)
            if st is None:
                if sgn * (c - level) > 0:
                    self._state[side] = {"phase": "broken"}
                continue
            if st["phase"] == "dead":
                continue
            if sgn * (c - level) < 0:
                self._state[side] = None
                continue
            near = last["low"] if sgn > 0 else last["high"]
            if st["phase"] == "broken":
                if sgn * (near - level) <= 0:
                    st.update(phase="retest", ext=last["high"] if sgn > 0 else last["low"], far=near)
            elif sgn * (c - st["ext"]) > 0:
                st["phase"] = "dead"
                target = level + sgn * width
                if sgn * (target - c) <= 0:
                    continue
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c,
                                spot_stop=round(st["far"], 2), spot_target=round(target, 2),
                                breakeven_trigger=round(c + self.BREAKEVEN_FRAC * (target - c), 2),
                                time_stop_minutes=self.TIME_STOP_MINUTES)
                return self._plan_signal(direction, c, indicators,
                                         f"ORB break-retest-go {side} close={c:.1f}", plan)
            elif sgn * (near - st["far"]) < 0:
                st["far"] = near
        return None


# ── 3. SUPERTREND_EMA ────────────────────────────────────────────────────────

class STVariant(SupertrendEMAStrategy):
    """ST_EMA921: the live rule with EMA 9/21 in place of 5/13.
    ST_STRUCTURE: the live rule, and the close must clear the high (low) of the
    previous 10 bars. ST_PULLBACK: Supertrend and EMA 9/21 aligned for 10
    bars, a bar touching EMA 9, then a close back beyond EMA 9 in trend."""

    def __init__(self, variant: str, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.VARIANT = variant
        self.fast, self.slow = (9, 21) if variant in ("ST_EMA921", "ST_PULLBACK") else (5, 13)

    async def evaluate(self, candles, indicators):
        if self.VARIANT == "ST_STRUCTURE":
            sig = await super().evaluate(candles, indicators)
            if sig is None:
                return None
            bars = self._bars(candles)
            prior = bars[-11:-1]
            s = _sign(sig)
            ext = max(b["high"] for b in prior) if s > 0 else min(b["low"] for b in prior)
            return sig if s * (bars[-1]["close"] - ext) > 0 else None
        return await self._own(candles, indicators)

    async def _own(self, candles, indicators):
        if self._roll_day():
            self._trades_today, self._last_exit_at = 0, None
        bars = self._bars(candles)
        if len(bars) < 40 or not self._new_bar(bars):
            return None
        if self.open_positions or self._trades_today >= self.MAX_TRADES:
            return None
        from app.runtime.context import now_utc
        if self._last_exit_at is not None and \
                (now_utc() - self._last_exit_at).total_seconds() < self.COOLDOWN_BARS * 60:
            return None
        from app.services.trading.regime_indicators import compute_adx
        h, l, c, st = self._series(bars)
        ef, es = ema(c, self.fast), ema(c, self.slow)
        adx = compute_adx(h, l, c, 14)
        t = len(bars) - 1
        if adx is None or adx <= self.ADX_MIN or np.isnan(st.supertrend[t]) \
                or np.isnan(es[t - 10]):
            return None
        d = st.direction
        for side, sgn, direction in (("BUY", 1, Direction.BUY), ("SELL", -1, Direction.SELL)):
            if d[t] != sgn or not sgn * (ef[t] - es[t]) > 0:
                continue
            if self.VARIANT == "ST_EMA921":
                look = range(t - 2 * self.CROSS_WINDOW, t + 1)
                flip = self._last_event(look, lambda i: d[i] == sgn and d[i - 1] == -sgn
                                        and not np.isnan(st.supertrend[i - 1]))
                cross = self._last_event(look, lambda i: sgn * (ef[i] - es[i]) > 0
                                         and sgn * (ef[i - 1] - es[i - 1]) <= 0)
                if flip is None or cross is None or abs(flip - cross) > self.CROSS_WINDOW \
                        or max(flip, cross) != t:
                    continue
            else:                                                   # ST_PULLBACK
                if not all(d[i] == sgn and sgn * (ef[i] - es[i]) > 0 for i in range(t - 10, t + 1)):
                    continue
                touched = any(sgn * ((l[i] if sgn > 0 else h[i]) - ef[i]) <= 0 for i in (t - 2, t - 1))
                if not (touched and sgn * (c[t] - ef[t]) > 0 and sgn * (c[t - 1] - ef[t - 1]) <= 0):
                    continue
            close, stop = float(c[t]), float(st.supertrend[t])
            plan = ExitPlan(strategy=self.name, side=side, spot_entry=close, spot_stop=stop,
                            premium_target_pts=self.PREMIUM_TARGET_PTS,
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(direction, close, indicators,
                                     f"{self.VARIANT} {side} ADX={adx:.1f} st={stop:.1f}", plan)
        return None


# ── 4. EMA_RSI_PULLBACK_15M ──────────────────────────────────────────────────

class ER3m(EMARSIPullback15mStrategy):
    """15-min context as live (EMA 9 vs 21, RSI touched 40-50 / 50-60 in the
    last 3 completed 15-min bars), executed on 3-min bars: a 3-min close back
    across the 3-min EMA 9 (ER_3M), and also beyond the previous 3-min bar's
    extreme (ER_3M_CONFIRM). Stop: the extreme of the last five 3-min bars.
    T1 1R (stop to entry), T2 2R, 14:45 exit, as live."""
    BAR_MINUTES = 3

    def __init__(self, variant: str, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.VARIANT = variant

    async def evaluate(self, candles, indicators):
        src = self.history or candles
        b3 = resample(src, 3)
        if len(b3) < 30 or not self._new_bar(b3) or self.open_positions:
            return None
        last = b3[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        b15 = [b for b in resample(src, 15) if b["end"] <= last["end"]]
        if len(b15) < 30:
            return None
        c15 = np.array([b["close"] for b in b15])
        e9, e21, r = ema(c15, 9), ema(c15, 21), rsi(c15, 14)
        t15 = len(b15) - 1
        c3 = np.array([b["close"] for b in b3])
        e3 = ema(c3, 9)
        t = len(b3) - 1
        if np.isnan(e21[t15]) or np.isnan(e3[t]):
            return None
        for side, sgn, direction, band in (("BUY", 1, Direction.BUY, self.RSI_LONG),
                                           ("SELL", -1, Direction.SELL, self.RSI_SHORT)):
            if not sgn * (e9[t15] - e21[t15]) > 0:
                continue
            if not any(band[0] <= r[i] <= band[1] for i in range(max(1, t15 - 2), t15 + 1)):
                continue
            c = last["close"]
            if not (sgn * (c - e3[t]) > 0 and sgn * (b3[t - 1]["close"] - e3[t - 1]) <= 0):
                continue
            if self.VARIANT == "ER_3M_CONFIRM":
                prev_ext = b3[t - 1]["high"] if sgn > 0 else b3[t - 1]["low"]
                if not sgn * (c - prev_ext) > 0:
                    continue
            win = b3[t - 4:t + 1]
            stop = min(b["low"] for b in win) if sgn > 0 else max(b["high"] for b in win)
            risk = sgn * (c - stop)
            if risk <= 0:
                continue
            plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                            spot_t1=round(c + sgn * risk, 2), spot_target=round(c + sgn * 2 * risk, 2),
                            exit_at=self.EXIT_AT)
            return self._plan_signal(direction, c, indicators, f"{self.VARIANT} {side} close {c:.1f}", plan)
        return None


class ERNotExtended(EMARSIPullback15mStrategy):
    """Live rule; skip when the close is more than *max_atr* 15-min ATRs from
    the 15-min EMA 21 (a late entry far from the trend structure)."""
    VARIANT = "ER_NOT_EXTENDED"

    def __init__(self, max_atr: float, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.max_atr = max_atr

    async def evaluate(self, candles, indicators):
        sig = await super().evaluate(candles, indicators)
        if sig is None:
            return None
        bars = self._bars(candles)
        cl = np.array([b["close"] for b in bars])
        a = atr(np.array([b["high"] for b in bars]), np.array([b["low"] for b in bars]), cl, 14)
        e21 = ema(cl, 21)
        if not np.isfinite(a[-1]) or a[-1] <= 0:
            return sig
        return sig if abs(cl[-1] - e21[-1]) / a[-1] <= self.max_atr else None


# ── 5. PIVOT_BOUNCE ──────────────────────────────────────────────────────────

class PivotSweep(PivotBounceStrategy):
    """PV_SWEEP_CLOSE: a 3-min bar trades at least *sweep_min* points through
    S1/PP (R1/PP) from the right side and closes back across the level (any
    colour). PV_SWEEP_CONFIRM: that bar, then the next bar closes beyond the
    sweep bar's far side (its high for a long). Stop: the sweep extreme ±
    5 pts. Target: the next pivot. 09:30-14:00, one trade per level and side."""
    STOP_BEYOND_SWEEP = 5.0

    def __init__(self, variant: str, sweep_min: float, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self.VARIANT, self.sweep_min = variant, sweep_min
        self._pending = None

    async def evaluate(self, candles, indicators):
        if self._roll_day():
            self._used, self._pending = set(), None
        bars = resample(candles, self.BAR_MINUTES)
        if len(bars) < 3 or not self._new_bar(bars) or self.open_positions:
            return None
        last, prev = bars[-1], bars[-2]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        pv = self.pivots()
        if pv is None:
            return None
        ladder = [("S1", pv["S1"]), ("PP", pv["PP"]), ("R1", pv["R1"])]
        pend, self._pending = self._pending, None
        if pend is not None:
            sgn, side, direction, name, stop, target, ext = pend
            if sgn * (last["close"] - ext) > 0 and sgn * (target - last["close"]) > 0:
                self._used.add((side, name))
                return self._signal(side, direction, name, last["close"], stop, target)
        cands = [(1, "BUY", Direction.BUY, i, n, lv) for i, (n, lv) in enumerate(ladder[:2])] + \
                [(-1, "SELL", Direction.SELL, i, n, lv) for i, (n, lv) in ((2, ladder[2]), (1, ladder[1]))]
        for sgn, side, direction, i, name, lvl in cands:
            if (side, name) in self._used:
                continue
            far = last["low"] if sgn > 0 else last["high"]
            if not (sgn * (prev["close"] - lvl) > 0 and sgn * (lvl - far) >= self.sweep_min
                    and sgn * (last["close"] - lvl) > 0):
                continue
            target = ladder[i + sgn][1]
            stop = round(far - sgn * self.STOP_BEYOND_SWEEP, 2)
            if self.VARIANT == "PV_SWEEP_CONFIRM":
                ext = last["high"] if sgn > 0 else last["low"]
                self._pending = (sgn, side, direction, name, stop, target, ext)
                return None
            self._used.add((side, name))
            return self._signal(side, direction, name, last["close"], stop, target)
        return None

    def _signal(self, side, direction, name, c, stop, target):
        plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=stop,
                        spot_target=target, exit_at=self.EXIT_AT)
        return self._plan_signal(direction, c, {}, f"{self.VARIANT} at {name}, close {c:.1f}", plan)


def pivot_level(notes: str) -> Optional[str]:
    for lv in ("S1", "PP", "R1"):
        if f" {lv} " in f" {notes} ".replace(",", " "):
            return lv
    return None
