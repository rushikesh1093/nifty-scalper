"""
The three NIFTY option-buying strategies (app.strategies.names).

Each emits BUY (buy ATM CE) or SELL (buy ATM PE) signals and attaches a spot
exit plan (app.services.trading.exit_plan) that paper_engine enforces next to
the premium hard stop (``sl_pct``):

  VWAP_EMA_PULLBACK  3-min. Trend (close vs VWAP, EMA 9 vs 21) → pullback to
                     within 5 pts of EMA 9/21 without a close through EMA 21 →
                     green/red trigger back across EMA 9 with RSI 50-70 / 30-50.
                     Stop: pullback low/high or 20 % premium. T1 = 1.5 R: book
                     half, stop to entry. Rest trails on EMA 9 (3-min close).
                     Hard exit on a 3-min close back across VWAP.
  ORB_BREAKOUT       5-min. OR = 09:15-09:30, width 40-120 pts. Close beyond
                     the OR (volume > 1.2× last 5 bars when the feed has
                     volume). Stop: OR mid or 25 % premium. Target: breakout
                     level ± OR width; stop to entry at 50 % of the way; 45-min
                     time exit. One trade per direction, no entries after
                     11:00, one reverse trade only after a stop-out.
  SUPERTREND_EMA     1-min. Supertrend(10, 2) flip + EMA 5/13 cross within ±2
                     bars, ADX(14) > 20. Stop: Supertrend line (trailed) or
                     15 % premium. Target +20 pts premium. Exit on an adverse
                     flip; 10-min time stop; 3-bar cooldown after an exit;
                     max 6 trades a day.

Point thresholds are NIFTY points, so these strategies are built for NIFTY
only (build_strategies returns none for other underlyings).

Entry is "next candle's open": the signal is evaluated as soon as the bar
closes and filled at market straight away, which is that open.
"""
import logging
from datetime import datetime, time as dtime, timedelta
from typing import Optional

import numpy as np

from app.runtime.context import now_utc, params, trading_day
from app.services.trading import exit_plan as xp
from app.services.trading.exit_plan import ExitPlan
from app.strategies.base import BaseStrategy, Direction, SignalEvent
from app.strategies.indicators import ema, resample, rsi, supertrend, vwap
from app.strategies.names import ALL_STRATEGY_NAMES, StrategyName
from app.services.market_data.timestamps import try_normalize_candle_timestamp

logger = logging.getLogger(__name__)

SUPPORTED_UNDERLYINGS = ("NIFTY",)


def _t(dt: datetime) -> dtime:
    return dt.time().replace(tzinfo=None)


def _in_windows(t: dtime, windows) -> bool:
    return any(s <= t <= e for s, e in windows)


def _ts(candle: dict) -> Optional[datetime]:
    return try_normalize_candle_timestamp(candle.get("timestamp"))


def _new_bars(plan, bars: list[dict]) -> list[int]:
    """Indexes of the completed bars the plan's bar exits have not seen yet,
    oldest first, and mark them seen. Walking every one (not only the newest)
    means a runner cycle that skips a minute cannot skip a bar exit. A plan
    without a mark (written before the field existed) sees the newest bar."""
    if not bars:
        return []
    if plan.last_bar_start is None:
        idx = [len(bars) - 1]
    else:
        seen = datetime.fromisoformat(plan.last_bar_start)
        idx = [i for i, b in enumerate(bars) if b["start"] > seen]
    if idx:
        plan.last_bar_start = bars[idx[-1]]["start"].isoformat()
    return idx


def _session_vwap_at(candles: list[dict]) -> dict:
    """Today's session VWAP after each 1-min candle, keyed by the candle's close
    time (aware IST), so a bar's VWAP is the value at the bar's end."""
    today = trading_day()
    rows = [(dt, c) for c in candles if (dt := _ts(c)) is not None and dt.date() == today]
    if not rows:
        return {}
    h = np.array([float(c["high"]) for _, c in rows])
    l = np.array([float(c["low"]) for _, c in rows])
    cl = np.array([float(c["close"]) for _, c in rows])
    v = np.array([float(c.get("volume") or 0) for _, c in rows])
    series = vwap(h, l, cl, v)
    return {dt.replace(second=0, microsecond=0) + timedelta(minutes=1): float(x)
            for (dt, _), x in zip(rows, series)}


class _PlanStrategy(BaseStrategy):
    """Shared plumbing: bar building, once-per-bar evaluation, plan signals."""
    _cooldown_minutes = 0          # per-strategy trade rules replace the generic cooldown
    BAR_MINUTES = 1

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._last_bar_start: Optional[datetime] = None
        self._day = None

    def _bars(self, candles: list[dict]) -> list[dict]:
        return resample(self.history or candles, self.BAR_MINUTES)

    def _new_bar(self, bars: list[dict]) -> bool:
        """True once per completed bar, and only for today's bars."""
        if not bars:
            return False
        start = bars[-1]["start"]
        if start == self._last_bar_start or start.date() != trading_day():
            return False
        self._last_bar_start = start
        return True

    def _roll_day(self) -> bool:
        day = trading_day()
        if self._day != day:
            self._day = day
            return True
        return False

    def restore_day(self, positions: list) -> None:
        """Rebuild today's per-strategy state from this strategy's persisted
        positions (oldest first) after a restart: the open count here, the
        trade rules in subclasses. Marks the day as already rolled."""
        self._day = trading_day()
        self.open_positions = sum(1 for p in positions if p.is_open)

    def _plan_signal(self, direction: Direction, spot: float, indicators: dict,
                     notes: str, plan: ExitPlan,
                     bar_start: Optional[datetime] = None) -> Optional[SignalEvent]:
        # Bar exits start after the trigger bar (the bar of this strategy's timeframe)
        start = bar_start or self._last_bar_start
        if start is not None and plan.last_bar_start is None:
            plan.last_bar_start = start.isoformat()
        problems = plan.validate()
        if problems:
            logger.debug("[%s] plan rejected: %s", self.name, "; ".join(problems))
            return None
        sig = self._make_signal(direction, spot, indicators, notes, exit_plan=plan.to_dict())
        # The premium R:R filter checks target_pct / sl_pct. The real target is
        # the spot plan, so the premium target is recorded at exactly the floor.
        sig.target_pct = round(self.sl_pct * params().min_rr, 6)
        return sig


# ─── 1. VWAP + EMA Pullback (3-min trend scalp) ──────────────────────────────

class VWAPEMAPullbackStrategy(_PlanStrategy):
    name = StrategyName.VWAP_EMA_PULLBACK.value
    sl_pct = 0.20
    target_pct = 0.40
    BAR_MINUTES = 3
    NEAR_EMA_PTS = 5.0
    PULLBACK_BARS = 4              # trigger bar + the 3 before it
    RSI_LONG = (50.0, 70.0)
    RSI_SHORT = (30.0, 50.0)
    T1_R = 1.5
    WINDOWS = ((dtime(9, 30), dtime(11, 30)), (dtime(13, 30), dtime(14, 45)))

    @staticmethod
    def _emas(bars):
        closes = np.array([b["close"] for b in bars])
        return ema(closes, 9), ema(closes, 21), rsi(closes, 14)

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        bars = self._bars(candles)
        if len(bars) < 22 or not self._new_bar(bars):
            return None
        if self.open_positions:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), self.WINDOWS):
            return None
        vwap_v = indicators.get("vwap")
        if vwap_v is None:
            return None

        e9, e21, r = self._emas(bars)
        t = len(bars) - 1
        win = range(t - self.PULLBACK_BARS + 1, t + 1)
        if any(np.isnan(e9[i]) or np.isnan(e21[i]) for i in win) or np.isnan(r[t]):
            return None
        c, o, rsi_t = last["close"], last["open"], float(r[t])

        # ── long: buy CE ─────────────────────────────────────────────────────
        if c > vwap_v and e9[t] > e21[t]:
            touches = [i for i in win if bars[i]["low"] <= e9[i] + self.NEAR_EMA_PTS]
            held = all(bars[i]["close"] >= e21[i] and e9[i] > e21[i] for i in win)
            recent_touch = bool(touches) and touches[-1] >= t - 1
            if (recent_touch and held and c > o and c > e9[t]
                    and self.RSI_LONG[0] <= rsi_t <= self.RSI_LONG[1]):
                stop = min(bars[i]["low"] for i in range(touches[0], t + 1))
                risk = c - stop
                plan = ExitPlan(strategy=self.name, side="BUY", spot_entry=c, spot_stop=stop,
                                spot_t1=round(c + self.T1_R * risk, 2))
                return self._plan_signal(
                    Direction.BUY, c, indicators,
                    f"3m pullback long: close={c:.1f} ema9={e9[t]:.1f} ema21={e21[t]:.1f} "
                    f"vwap={vwap_v:.1f} rsi={rsi_t:.1f} stop={stop:.1f}", plan)

        # ── short: buy PE (mirror) ───────────────────────────────────────────
        if c < vwap_v and e9[t] < e21[t]:
            touches = [i for i in win if bars[i]["high"] >= e9[i] - self.NEAR_EMA_PTS]
            held = all(bars[i]["close"] <= e21[i] and e9[i] < e21[i] for i in win)
            recent_touch = bool(touches) and touches[-1] >= t - 1
            if (recent_touch and held and c < o and c < e9[t]
                    and self.RSI_SHORT[0] <= rsi_t <= self.RSI_SHORT[1]):
                stop = max(bars[i]["high"] for i in range(touches[0], t + 1))
                risk = stop - c
                plan = ExitPlan(strategy=self.name, side="SELL", spot_entry=c, spot_stop=stop,
                                spot_t1=round(c - self.T1_R * risk, 2))
                return self._plan_signal(
                    Direction.SELL, c, indicators,
                    f"3m pullback short: close={c:.1f} ema9={e9[t]:.1f} ema21={e21[t]:.1f} "
                    f"vwap={vwap_v:.1f} rsi={rsi_t:.1f} stop={stop:.1f}", plan)
        return None

    def candle_exit(self, plan, candles, indicators) -> Optional[str]:
        bars = self._bars(candles)
        if len(bars) < 22:
            return None
        new = _new_bars(plan, bars)
        if not new:
            return None
        vwap_at = _session_vwap_at(candles)
        e9, _, _ = self._emas(bars)
        for i in new:
            c = bars[i]["close"]
            vwap_v = vwap_at.get(bars[i]["end"])
            if vwap_v is None and i == len(bars) - 1:
                vwap_v = indicators.get("vwap")
            if vwap_v is not None and xp.breached(plan.side, c, vwap_v):
                return xp.VWAP_EXIT                  # hard exit: closed back across VWAP
            if plan.t1_done and not np.isnan(e9[i]) and xp.breached(plan.side, c, float(e9[i])):
                return xp.EMA_TRAIL                  # runner closed on the wrong side of EMA 9
        return None


# ─── 2. Opening Range Breakout (5-min) ───────────────────────────────────────

class ORBBreakoutStrategy(_PlanStrategy):
    name = StrategyName.ORB_BREAKOUT.value
    sl_pct = 0.25
    target_pct = 0.50
    BAR_MINUTES = 5
    OR_START, OR_END = dtime(9, 15), dtime(9, 30)
    MIN_WIDTH, MAX_WIDTH = 40.0, 120.0
    VOL_MULT, VOL_BARS = 1.2, 5
    MIN_VOL_BARS = 3       # a 09:35 breakout has only 4 earlier bars that day
    LAST_ENTRY = dtime(11, 0)
    TIME_STOP_MINUTES = 45
    BREAKEVEN_FRAC = 0.5

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._trades: list[list] = []          # [direction, exit_reason | None] per trade today
        # Nearest-expiry index-future 1-min candles (per-minute volume), set by
        # the runner each cycle. Empty → the volume rule cannot be checked.
        self.futures_candles: list[dict] = []
        self._vol_warned_day = None

    def restore_day(self, positions: list) -> None:
        super().restore_day(positions)
        self._trades = [[Direction.SELL if (p.option_type or "").upper() == "PE" else Direction.BUY,
                         None if p.is_open else p.exit_reason] for p in positions]

    def _volume_ok(self, bar: dict) -> bool:
        """Futures volume of *bar* > VOL_MULT × the average of the (up to)
        VOL_BARS bars before it that day, needing at least MIN_VOL_BARS. No
        futures volume (replay, feed gap) → not checked, logged once a day;
        the index spot itself carries no volume."""
        fut = resample(self.futures_candles, self.BAR_MINUTES) if self.futures_candles else []
        by_start = {b["start"]: b for b in fut}
        cur = by_start.get(bar["start"])
        prior = [b for b in fut if b["start"] < bar["start"]
                 and b["start"].date() == bar["start"].date()][-self.VOL_BARS:]
        if cur is None or len(prior) < self.MIN_VOL_BARS or not cur["volume"]:
            if self._vol_warned_day != trading_day():
                self._vol_warned_day = trading_day()
                logger.warning("[%s] no futures volume for %s bars — volume rule not checked",
                               self.name, self.underlying)
            return True
        avg = float(np.mean([b["volume"] for b in prior]))
        return avg > 0 and cur["volume"] > self.VOL_MULT * avg

    def opening_range(self, candles) -> Optional[tuple[float, float]]:
        """(high, low) of today's 09:15-09:29 one-minute candles, or None until
        the range is complete (first and last minute both present)."""
        today = trading_day()
        rng = [(dt, c) for c in candles if (dt := _ts(c)) is not None and dt.date() == today
               and self.OR_START <= _t(dt) < self.OR_END]
        if not rng:
            return None
        mins = {_t(dt) for dt, _ in rng}
        if dtime(9, 15) not in mins or dtime(9, 29) not in mins:
            return None
        return (max(float(c["high"]) for _, c in rng), min(float(c["low"]) for _, c in rng))

    def _allowed(self, direction: Direction) -> bool:
        """One trade per direction; a reverse trade only after the first stopped out."""
        if not self._trades:
            return True
        if len(self._trades) == 1:
            first_dir, first_exit = self._trades[0]
            return first_exit in xp.STOP_REASONS and direction != first_dir
        return False

    def on_position_opened(self, signal: SignalEvent) -> None:
        self._trades.append([signal.direction, None])

    def on_position_closed(self, direction: Direction, reason: str) -> None:
        for tr in reversed(self._trades):
            if tr[0] == direction and tr[1] is None:
                tr[1] = reason
                return

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if self._roll_day():
            self._trades = []
        bars = resample(candles, self.BAR_MINUTES)       # today only: no warm-up needed
        if len(bars) < 4 or not self._new_bar(bars):
            return None
        last = bars[-1]
        if _t(last["start"]) < self.OR_END or _t(last["end"]) > self.LAST_ENTRY:
            return None
        if self.open_positions:
            return None
        rng = self.opening_range(candles)
        if rng is None:
            return None
        or_high, or_low = rng
        width = or_high - or_low
        if not (self.MIN_WIDTH <= width <= self.MAX_WIDTH):
            return None

        c, prev_c = last["close"], bars[-2]["close"]
        up = c > or_high and prev_c <= or_high and self._allowed(Direction.BUY)
        down = c < or_low and prev_c >= or_low and self._allowed(Direction.SELL)
        if not (up or down) or not self._volume_ok(last):
            return None
        mid = round((or_high + or_low) / 2, 2)

        if up:
            target = or_high + width
            plan = ExitPlan(strategy=self.name, side="BUY", spot_entry=c, spot_stop=mid,
                            spot_target=round(target, 2),
                            breakeven_trigger=round(c + self.BREAKEVEN_FRAC * (target - c), 2),
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(
                Direction.BUY, c, indicators,
                f"ORB up: close={c:.1f} > OR high={or_high:.1f} width={width:.1f} "
                f"target={target:.1f} stop(mid)={mid:.1f}", plan)
        if down:
            target = or_low - width
            plan = ExitPlan(strategy=self.name, side="SELL", spot_entry=c, spot_stop=mid,
                            spot_target=round(target, 2),
                            breakeven_trigger=round(c - self.BREAKEVEN_FRAC * (c - target), 2),
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(
                Direction.SELL, c, indicators,
                f"ORB down: close={c:.1f} < OR low={or_low:.1f} width={width:.1f} "
                f"target={target:.1f} stop(mid)={mid:.1f}", plan)
        return None


# ─── 3. Supertrend + EMA crossover (1-min momentum flip) ─────────────────────

class SupertrendEMAStrategy(_PlanStrategy):
    name = StrategyName.SUPERTREND_EMA.value
    sl_pct = 0.15
    target_pct = 0.30
    BAR_MINUTES = 1
    ST_PERIOD, ST_MULT = 10, 2.0
    ADX_MIN = 20.0
    CROSS_WINDOW = 2
    PREMIUM_TARGET_PTS = 20.0
    TIME_STOP_MINUTES = 10
    COOLDOWN_BARS = 3
    MAX_TRADES = 6

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._trades_today = 0
        self._last_exit_at: Optional[datetime] = None

    def on_position_opened(self, signal: SignalEvent) -> None:
        self._trades_today += 1

    def restore_day(self, positions: list) -> None:
        super().restore_day(positions)
        self._trades_today = len(positions)
        exits = [p.exit_time for p in positions if p.exit_time is not None]
        self._last_exit_at = max(exits) if exits else None
        if self._last_exit_at is not None and self._last_exit_at.tzinfo is None:
            from datetime import timezone
            self._last_exit_at = self._last_exit_at.replace(tzinfo=timezone.utc)

    def on_position_closed(self, direction: Direction, reason: str) -> None:
        self._last_exit_at = now_utc()

    def _series(self, bars):
        h = np.array([b["high"] for b in bars])
        l = np.array([b["low"] for b in bars])
        c = np.array([b["close"] for b in bars])
        st = supertrend(h, l, c, self.ST_PERIOD, self.ST_MULT)
        return h, l, c, st

    @staticmethod
    def _last_event(idx: range, pred) -> Optional[int]:
        hits = [i for i in idx if pred(i)]
        return hits[-1] if hits else None

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if self._roll_day():
            self._trades_today, self._last_exit_at = 0, None
        bars = self._bars(candles)
        if len(bars) < 40 or not self._new_bar(bars):
            return None
        if self.open_positions or self._trades_today >= self.MAX_TRADES:
            return None
        if self._last_exit_at is not None and \
                (now_utc() - self._last_exit_at).total_seconds() < self.COOLDOWN_BARS * 60:
            return None

        # Imported here, not at module top: regime_indicators imports
        # app.strategies.indicators, whose package __init__ imports this module.
        from app.services.trading.regime_indicators import compute_adx

        h, l, c, st = self._series(bars)
        e5, e13 = ema(c, 5), ema(c, 13)
        adx = compute_adx(h, l, c, 14)
        t = len(bars) - 1
        if adx is None or adx <= self.ADX_MIN or np.isnan(st.supertrend[t]) \
                or np.isnan(e5[t - 5]) or np.isnan(e13[t - 5]):
            return None
        d = st.direction
        look = range(t - 2 * self.CROSS_WINDOW, t + 1)

        for side, sgn, direction in (("BUY", 1, Direction.BUY), ("SELL", -1, Direction.SELL)):
            if d[t] != sgn or not (sgn * (e5[t] - e13[t]) > 0):
                continue
            flip = self._last_event(look, lambda i: d[i] == sgn and d[i - 1] == -sgn
                                    and not np.isnan(st.supertrend[i - 1]))
            cross = self._last_event(look, lambda i: sgn * (e5[i] - e13[i]) > 0
                                     and sgn * (e5[i - 1] - e13[i - 1]) <= 0)
            if flip is None or cross is None or abs(flip - cross) > self.CROSS_WINDOW:
                continue
            if max(flip, cross) != t:      # fire on the confirmation candle only
                continue
            close, stop = float(c[t]), float(st.supertrend[t])
            plan = ExitPlan(strategy=self.name, side=side, spot_entry=close, spot_stop=stop,
                            premium_target_pts=self.PREMIUM_TARGET_PTS,
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(
                direction, close, indicators,
                f"ST flip {'up' if sgn > 0 else 'down'} (bar {flip - t}) + EMA5/13 cross "
                f"(bar {cross - t}) ADX={adx:.1f} st={stop:.1f}", plan)
        return None

    def candle_exit(self, plan, candles, indicators) -> Optional[str]:
        bars = self._bars(candles)
        if len(bars) < 40:
            return None
        new = _new_bars(plan, bars)
        if not new:
            return None
        _, _, _, st = self._series(bars)
        for i in new:
            if np.isnan(st.supertrend[i]):
                continue
            if int(st.direction[i]) != plan.sign:
                return xp.ST_FLIP
            xp.tighten_stop(plan, round(float(st.supertrend[i]), 2))   # trail on the ST line
        return None


# ─── Registry ─────────────────────────────────────────────────────────────────

def build_strategies(underlying: str = "NIFTY") -> list[BaseStrategy]:
    """Instantiate the strategies for *underlying* (NIFTY only — the rules are in NIFTY points)."""
    if underlying not in SUPPORTED_UNDERLYINGS:
        return []
    return [
        VWAPEMAPullbackStrategy(underlying),
        ORBBreakoutStrategy(underlying),
        SupertrendEMAStrategy(underlying),
    ]


# Re-exported for compatibility; the canonical tuple lives in app.strategies.names.
STRATEGY_NAMES = ALL_STRATEGY_NAMES
