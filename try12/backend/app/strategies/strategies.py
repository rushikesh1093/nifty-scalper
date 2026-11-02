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
                     the OR (futures volume > 1.2× earlier bars). Stop: a
                     quarter of the OR width back inside the range, or 25 %
                     premium. Target: breakout level ± OR width; stop to entry
                     at 50 % of the way; 90-min time exit (real-path sweep:
                     the OR-mid stop and 45-min exit lost most). One trade per direction, no entries after
                     11:00, one reverse trade only after a stop-out.
  SUPERTREND_EMA     1-min. Supertrend(10, 2) flip + EMA 5/13 cross within ±2
                     bars, ADX(14) > 20. Stop: Supertrend line (trailed) or
                     15 % premium. Target +8 pts premium (+20 needs ~38 NIFTY
                     pts in 10 min: reached ~15 % of the time on real data).
                     Exit on an adverse flip; 10-min time stop; 3-bar cooldown after an exit;
                     max 6 trades a day.
  CPR_VWAP           5-min, 09:30-13:00, only when yesterday's CPR is narrow
                     (|TC - BC| < 0.20 % of P). First close beyond the CPR on
                     the same side of session VWAP. Stop: the other side of
                     the CPR. Exit: 5-min Supertrend(10, 3) trail, close
                     across it, 15:15. One trade a day; puts only
                     (pe_only_strategies). A paper forward test: +0.16R after
                     costs for puts 2023-26, not proven (NEW_STRATEGY_RESULTS.md).

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
from app.strategies.indicators import atr, ema, resample, rsi, supertrend, vwap
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
    TIME_STOP_MINUTES = 90
    BREAKEVEN_FRAC = 0.5
    STOP_INSIDE_FRAC = 0.25        # stop this fraction of the OR width back inside the range

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
        if up:
            target = or_high + width
            stop = round(or_high - self.STOP_INSIDE_FRAC * width, 2)
            plan = ExitPlan(strategy=self.name, side="BUY", spot_entry=c, spot_stop=stop,
                            spot_target=round(target, 2),
                            breakeven_trigger=round(c + self.BREAKEVEN_FRAC * (target - c), 2),
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(
                Direction.BUY, c, indicators,
                f"ORB up: close={c:.1f} > OR high={or_high:.1f} width={width:.1f} "
                f"target={target:.1f} stop={stop:.1f}", plan)
        if down:
            target = or_low - width
            stop = round(or_low + self.STOP_INSIDE_FRAC * width, 2)
            plan = ExitPlan(strategy=self.name, side="SELL", spot_entry=c, spot_stop=stop,
                            spot_target=round(target, 2),
                            breakeven_trigger=round(c - self.BREAKEVEN_FRAC * (c - target), 2),
                            time_stop_minutes=self.TIME_STOP_MINUTES)
            return self._plan_signal(
                Direction.SELL, c, indicators,
                f"ORB down: close={c:.1f} < OR low={or_low:.1f} width={width:.1f} "
                f"target={target:.1f} stop={stop:.1f}", plan)
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
    PREMIUM_TARGET_PTS = 8.0
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


# ─── 4. 09:15 candle breakout (5-min) ────────────────────────────────────────

class FirstCandleBreakoutStrategy(_PlanStrategy):
    """The 09:15-09:20 candle's high/low ± 10 pts, broken on a 5-min close
    between 09:20 and 10:00. Stop: the other side of that candle, at most
    MAX_STOP_PTS from entry. One trade per direction a day.

    Exit (TRAIL_EXIT, since 30 Oct 2026): no fixed target; the stop trails the
    best price since entry minus 2 × the 5-min ATR(14) at the signal (an ATR
    chandelier, on every 1-min close), held to the session square-off.
    Research on the same entries, net of option costs, per year
    (docs/STRATEGY_PERFORMANCE_REPORT.md, exit variants): 1.5 × range target
    and 14:45 exit -0.147 / +0.003 / -0.196 R; chandelier +0.019 / +0.039 /
    -0.070 R. Less bad in every year; not shown to be profitable.
    TRAIL_EXIT = False restores the target and the 14:45 exit."""
    name = StrategyName.FIRST_CANDLE_BREAKOUT.value
    sl_pct = 0.25
    target_pct = 0.50
    BAR_MINUTES = 5
    BUFFER_PTS = 10.0
    TARGET_RANGE_MULT = 1.5
    MAX_STOP_PTS = 50.0         # the 09:15 candle is ~76 pts (median, real data): cap the stop
    LAST_BAR_END = dtime(10, 0)
    # Ready at the 09:25 close (09:15-09:24 = 10 candles: the 09:15 bar and the
    # first breakout bar). Under the old global 20-candle wait the 09:25 and
    # 09:30 closes were never evaluated (11 of 20 breakout days broke first).
    MIN_TODAY_CANDLES = 10
    EXIT_AT = "14:45"
    TRAIL_EXIT = True

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._taken: set = set()

    def restore_day(self, positions: list) -> None:
        super().restore_day(positions)
        self._taken = {Direction.SELL if (p.option_type or "").upper() == "PE" else Direction.BUY
                       for p in positions}

    def on_position_opened(self, signal: SignalEvent) -> None:
        self._taken.add(signal.direction)

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if self._roll_day():
            self._taken = set()
        bars = resample(candles, self.BAR_MINUTES)       # today only
        if len(bars) < 2 or not self._new_bar(bars) or self.open_positions:
            return None
        first, last = bars[0], bars[-1]
        if _t(first["start"]) != dtime(9, 15) or _t(last["end"]) > self.LAST_BAR_END:
            return None
        hi, lo = first["high"], first["low"]
        rng = hi - lo
        c = last["close"]
        if rng <= 0:
            return None
        if c > hi + self.BUFFER_PTS and Direction.BUY not in self._taken:
            plan = self._exit_plan("BUY", c, round(max(lo, c - self.MAX_STOP_PTS), 2),
                                   round(c + self.TARGET_RANGE_MULT * rng, 2), last)
            return self._plan_signal(Direction.BUY, c, indicators,
                                     f"09:15 candle {lo:.1f}-{hi:.1f} broken up: close={c:.1f}", plan)
        if c < lo - self.BUFFER_PTS and Direction.SELL not in self._taken:
            plan = self._exit_plan("SELL", c, round(min(hi, c + self.MAX_STOP_PTS), 2),
                                   round(c - self.TARGET_RANGE_MULT * rng, 2), last)
            return self._plan_signal(Direction.SELL, c, indicators,
                                     f"09:15 candle {lo:.1f}-{hi:.1f} broken down: close={c:.1f}", plan)
        return None

    def _exit_plan(self, side: str, c: float, stop: float, target: float, bar: dict) -> ExitPlan:
        if not self.TRAIL_EXIT:
            return ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=stop,
                            spot_target=target, exit_at=self.EXIT_AT)
        b5 = resample(self.history or [], 5)
        a = atr(np.array([b["high"] for b in b5]), np.array([b["low"] for b in b5]),
                np.array([b["close"] for b in b5]), 14) if len(b5) >= 15 else np.array([np.nan])
        trail_atr = float(a[-1]) if np.isfinite(a[-1]) and a[-1] > 0 else abs(c - stop)
        # 1-min bar exits start with the candle after the signal bar
        return ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=stop,
                        trail_atr=round(trail_atr, 2), trail_extreme=c,
                        last_bar_start=(bar["end"] - timedelta(minutes=1)).isoformat())

    def candle_exit(self, plan, candles, indicators) -> Optional[str]:
        """ATR chandelier: every completed 1-min candle not yet seen."""
        if not plan.trail_atr:
            return None
        bars = resample(candles, 1)
        for i in _new_bars(plan, bars):
            xp.chandelier_update(plan, bars[i]["high"], bars[i]["low"])
        return None


# ─── 5. 15-min EMA trend + RSI pullback ──────────────────────────────────────

class EMARSIPullback15mStrategy(_PlanStrategy):
    """15-min EMA 9 > 21 (21 < 9 for PE); RSI pulls back into 40-50 (50-60);
    the next candle(s) close back across EMA 9 between 09:30 and 13:00. Stop:
    the pullback candle's low (high).

    Exit (TRAIL_EXIT, since 30 Oct 2026): no T1/T2; on every completed 5-min
    bar the stop trails the 5-min Supertrend(10, 3) line (only tightening) and
    a bar with the Supertrend against the trade exits at its close; held to the
    session square-off. Research on the same entries, net of option costs, per
    year: T1/T2/14:45 -0.311 / -0.045 / -0.083 R; Supertrend trail -0.147 /
    +0.171 / +0.083 R. Better in every year; the first year still lost.
    TRAIL_EXIT = False restores T1 1R (stop to entry), T2 2R and the 14:45 exit."""
    name = StrategyName.EMA_RSI_PULLBACK_15M.value
    sl_pct = 0.20
    target_pct = 0.40
    BAR_MINUTES = 15
    RSI_LONG, RSI_SHORT = (40.0, 50.0), (50.0, 60.0)
    TOUCH_LOOKBACK = 3            # the RSI touch may be up to 3 bars before the trigger
    WINDOW = (dtime(9, 30), dtime(13, 0))
    EXIT_AT = "14:45"
    TRAIL_EXIT = True
    TRAIL_BAR_MINUTES, ST_PERIOD, ST_MULT = 5, 10, 3.0

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        bars = self._bars(candles)                        # multi-day history
        if len(bars) < 30 or not self._new_bar(bars) or self.open_positions:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        cl = np.array([b["close"] for b in bars])
        e9, e21, r = ema(cl, 9), ema(cl, 21), rsi(cl, 14)
        t = len(bars) - 1
        if np.isnan(e21[t]) or np.isnan(r[t - 1]):
            return None
        c = last["close"]
        touches = range(max(1, t - self.TOUCH_LOOKBACK), t)
        for side, sgn, direction, band in (("BUY", 1, Direction.BUY, self.RSI_LONG),
                                           ("SELL", -1, Direction.SELL, self.RSI_SHORT)):
            if not sgn * (e9[t] - e21[t]) > 0:
                continue
            hit = [i for i in touches if band[0] <= r[i] <= band[1]]
            if not hit:
                continue
            crossed = sgn * (c - e9[t]) > 0 and sgn * (bars[t - 1]["close"] - e9[t - 1]) <= 0
            if not crossed:
                continue
            k = hit[-1]
            stop = bars[k]["low"] if sgn > 0 else bars[k]["high"]
            risk = sgn * (c - stop)
            if risk <= 0:
                continue
            if self.TRAIL_EXIT:
                # 5-min bar exits start with the bar after the 15-min signal bar
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                                last_bar_start=(last["end"] - timedelta(
                                    minutes=self.TRAIL_BAR_MINUTES)).isoformat())
            else:
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                                spot_t1=round(c + sgn * risk, 2),
                                spot_target=round(c + sgn * 2 * risk, 2), exit_at=self.EXIT_AT)
            return self._plan_signal(direction, c, indicators,
                                     f"15m EMA9/21 trend, RSI {r[k]:.1f} pullback, close {c:.1f} "
                                     f"across EMA9 {e9[t]:.1f}", plan)
        return None

    def candle_exit(self, plan, candles, indicators) -> Optional[str]:
        """Supertrend(10, 3) trail on completed 5-min bars. A plan with T1 /
        target / exit_at (made with TRAIL_EXIT off) keeps those exits only."""
        if plan.spot_t1 is not None or plan.spot_target is not None or plan.exit_at:
            return None
        bars = resample(self.history or candles, self.TRAIL_BAR_MINUTES)
        if len(bars) < self.ST_PERIOD + 5:
            return None
        new = _new_bars(plan, bars)
        if not new:
            return None
        st = supertrend(np.array([b["high"] for b in bars]), np.array([b["low"] for b in bars]),
                        np.array([b["close"] for b in bars]), self.ST_PERIOD, self.ST_MULT)
        for i in new:
            if np.isnan(st.supertrend[i]):
                continue
            if int(st.direction[i]) != plan.sign:
                return xp.ST_FLIP
            xp.tighten_stop(plan, round(float(st.supertrend[i]), 2))
        return None


# ─── 6. Pivot point bounce (3-min) ───────────────────────────────────────────

class PivotBounceStrategy(_PlanStrategy):
    """Previous trading session's floor pivots: PP = (H+L+C)/3, R1 = 2PP-L, S1 = 2PP-H.
    CE: a 3-min candle dips to S1 or PP from above and closes bullish above it.
    PE: a candle pokes up to R1 or PP from below and closes bearish below it.
    09:30-14:00. Stop 15 pts beyond the level; target the next pivot."""
    name = StrategyName.PIVOT_BOUNCE.value
    sl_pct = 0.15
    target_pct = 0.30
    BAR_MINUTES = 3
    STOP_PTS = 15.0
    WINDOW = (dtime(9, 30), dtime(14, 0))
    EXIT_AT = "14:45"

    # The runner resolves the previous session's H/L/C once a day
    # (app.services.market_data.prev_session) and hands it over with
    # set_prev_session; the rolling candle cache is never read for it.
    NEEDS_PREV_SESSION = True

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._used: set = set()
        self._levels_day = None            # trading day the levels below were resolved for
        self._prev = None                  # prev_session.PrevSession, or None
        self._prev_reason: Optional[str] = "previous-session levels not resolved yet"

    def restore_day(self, positions: list) -> None:
        super().restore_day(positions)
        self._used = set()

    def set_prev_session(self, for_day, levels, reason: Optional[str] = None) -> None:
        """Levels (a PrevSession, or None with *reason*) for trading day *for_day*."""
        self._levels_day, self._prev = for_day, levels
        self._prev_reason = None if levels is not None else (reason or "no previous-session levels")

    def prev_session_problem(self) -> Optional[str]:
        """Why the strategy cannot trade today for want of levels, or None."""
        if self._levels_day != trading_day():
            return "previous-session levels not resolved for today"
        return self._prev_reason

    def pivots(self) -> Optional[dict]:
        """PP/R1/S1 from the previous trading session's high, low and close."""
        if self.prev_session_problem() is not None:
            return None
        h, l, cl = self._prev.high, self._prev.low, self._prev.close
        pp = (h + l + cl) / 3
        return {"S1": round(2 * pp - h, 2), "PP": round(pp, 2), "R1": round(2 * pp - l, 2)}

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        if self._roll_day():
            self._used = set()
        bars = resample(candles, self.BAR_MINUTES)       # today only
        if len(bars) < 3 or not self._new_bar(bars) or self.open_positions:
            return None
        last, prev = bars[-1], bars[-2]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        pv = self.pivots()
        if pv is None:
            return None
        o, h, l, c = last["open"], last["high"], last["low"], last["close"]
        ladder = [("S1", pv["S1"]), ("PP", pv["PP"]), ("R1", pv["R1"])]
        for i, (lvl_name, lvl) in enumerate(ladder[:2]):                 # CE at S1 / PP
            key = ("BUY", lvl_name)
            if (key not in self._used and prev["close"] > lvl and l <= lvl < c and c > o):
                target = ladder[i + 1][1]
                plan = ExitPlan(strategy=self.name, side="BUY", spot_entry=c,
                                spot_stop=round(lvl - self.STOP_PTS, 2), spot_target=target,
                                exit_at=self.EXIT_AT)
                sig = self._plan_signal(Direction.BUY, c, indicators,
                                        f"bounce at {lvl_name} {lvl:.1f}, close {c:.1f}", plan)
                if sig is not None:
                    self._used.add(key)
                return sig
        for i, (lvl_name, lvl) in ((2, ladder[2]), (1, ladder[1])):        # PE at R1 / PP
            key = ("SELL", lvl_name)
            if (key not in self._used and prev["close"] < lvl and h >= lvl > c and c < o):
                target = ladder[i - 1][1]
                plan = ExitPlan(strategy=self.name, side="SELL", spot_entry=c,
                                spot_stop=round(lvl + self.STOP_PTS, 2), spot_target=target,
                                exit_at=self.EXIT_AT)
                sig = self._plan_signal(Direction.SELL, c, indicators,
                                        f"rejection at {lvl_name} {lvl:.1f}, close {c:.1f}", plan)
                if sig is not None:
                    self._used.add(key)
                return sig
        return None


# ─── 7. 5-min momentum burst ─────────────────────────────────────────────────

class MomentumBurstStrategy(_PlanStrategy):
    """Three consecutive green 5-min candles, each closing above the previous
    candle's high, the last above EMA 20, with 5-min ATR(14) > 15 pts (mirror
    for PE). 09:30-13:30. Stop: the third candle's low (high). Target: 1.5 ×
    ATR from entry. Fires on the first such candle of a burst only."""
    name = StrategyName.MOMENTUM_BURST.value
    sl_pct = 0.20
    target_pct = 0.40
    BAR_MINUTES = 5
    ATR_MIN = 15.0
    TARGET_ATR_MULT = 1.5
    WINDOW = (dtime(9, 30), dtime(13, 30))
    EXIT_AT = "14:30"

    @staticmethod
    def _burst(bars, t, sgn) -> bool:
        for i in (t - 2, t - 1, t):
            b, p = bars[i], bars[i - 1]
            if sgn > 0 and not (b["close"] > b["open"] and b["close"] > p["high"]):
                return False
            if sgn < 0 and not (b["close"] < b["open"] and b["close"] < p["low"]):
                return False
        return True

    async def evaluate(self, candles, indicators) -> Optional[SignalEvent]:
        bars = self._bars(candles)                        # multi-day history (EMA / ATR warm-up)
        if len(bars) < 25 or not self._new_bar(bars) or self.open_positions:
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        today = [i for i, b in enumerate(bars) if b["start"].date() == trading_day()]
        t = len(bars) - 1
        if len(today) < 4:                                # the 3 candles + the one before, today
            return None
        cl = np.array([b["close"] for b in bars])
        hi = np.array([b["high"] for b in bars])
        lo = np.array([b["low"] for b in bars])
        e20 = ema(cl, 20)
        a = atr(hi, lo, cl, 14)
        if np.isnan(e20[t]) or np.isnan(a[t]) or a[t] <= self.ATR_MIN:
            return None
        c = last["close"]
        for side, sgn, direction in (("BUY", 1, Direction.BUY), ("SELL", -1, Direction.SELL)):
            if not (self._burst(bars, t, sgn) and sgn * (c - e20[t]) > 0):
                continue
            if self._burst(bars, t - 1, sgn):            # already a burst a bar ago: not fresh
                continue
            stop = last["low"] if sgn > 0 else last["high"]
            plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2),
                            spot_target=round(c + sgn * self.TARGET_ATR_MULT * float(a[t]), 2),
                            exit_at=self.EXIT_AT)
            return self._plan_signal(direction, c, indicators,
                                     f"3-candle burst {'up' if sgn > 0 else 'down'}, close {c:.1f}, "
                                     f"EMA20 {e20[t]:.1f}, ATR {a[t]:.1f}", plan)
        return None


# ─── Once-a-day 5-min strategies (docs/NEW_STRATEGY_PREREGISTRATION.md) ──────

SIDES = (("BUY", 1, Direction.BUY), ("SELL", -1, Direction.SELL))


def _arr(bars, key):
    return np.array([b[key] for b in bars], dtype=float)


class _OncePerDay(_PlanStrategy):
    BAR_MINUTES = 5
    sl_pct = 0.35                 # premium hard stop; the spot plan stop normally comes first
    target_pct = 0.70

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._traded_day = None

    def on_position_opened(self, signal) -> None:
        self._traded_day = trading_day()

    def restore_day(self, positions: list) -> None:
        super().restore_day(positions)
        self._traded_day = trading_day() if positions else None

    def _ready(self, candles) -> Optional[list[dict]]:
        bars = self._bars(candles)
        if not bars or not self._new_bar(bars) or self.open_positions \
                or self._traded_day == trading_day():
            return None
        return bars


class _Trail5(_OncePerDay):
    ST_PERIOD, ST_MULT = 10, 3.0

    def candle_exit(self, plan, candles, indicators) -> Optional[str]:
        bars = self._bars(candles)
        if len(bars) < self.ST_PERIOD + 5:
            return None
        new = _new_bars(plan, bars)
        if not new:
            return None
        st = supertrend(_arr(bars, "high"), _arr(bars, "low"), _arr(bars, "close"),
                        self.ST_PERIOD, self.ST_MULT)
        for i in new:
            if np.isnan(st.supertrend[i]):
                continue
            if int(st.direction[i]) == plan.sign:
                plan.breakeven_done = True                 # trail armed
                xp.tighten_stop(plan, round(float(st.supertrend[i]), 2))
            elif plan.breakeven_done:
                return xp.ST_FLIP
        return None


class CPRVWAP(_Trail5):
    name = StrategyName.CPR_VWAP.value
    MAX_WIDTH_PCT = 0.20
    WINDOW = (dtime(9, 30), dtime(13, 0))
    NEEDS_PREV_SESSION = True

    def __init__(self, underlying: str = "NIFTY"):
        super().__init__(underlying)
        self._levels_day, self._prev = None, None

    def set_prev_session(self, for_day, levels, reason: Optional[str] = None) -> None:
        self._levels_day, self._prev = for_day, levels

    def prev_session_problem(self) -> Optional[str]:
        if self._levels_day != trading_day() or self._prev is None:
            return "previous-session levels not resolved for today"
        return None

    async def evaluate(self, candles, indicators):
        bars = resample(candles, 5)                        # today only
        if len(bars) < 3 or not self._new_bar(bars) or self.open_positions \
                or self._traded_day == trading_day() or self.prev_session_problem():
            return None
        last = bars[-1]
        if not _in_windows(_t(last["end"]), (self.WINDOW,)):
            return None
        h, l, c0 = float(self._prev.high), float(self._prev.low), float(self._prev.close)
        p = (h + l + c0) / 3
        bc = (h + l) / 2
        tc = 2 * p - bc
        top, bottom = max(tc, bc), min(tc, bc)
        if (top - bottom) / p * 100 >= self.MAX_WIDTH_PCT:
            return None
        vw = _session_vwap_at(candles).get(last["end"])
        if vw is None:
            return None
        c = last["close"]
        for side, sgn, direction in SIDES:
            level, stop = (top, bottom) if sgn > 0 else (bottom, top)
            if sgn * (c - level) > 0 and sgn * (c - vw) > 0:
                plan = ExitPlan(strategy=self.name, side=side, spot_entry=c, spot_stop=round(stop, 2))
                return self._plan_signal(direction, c, indicators,
                                         f"narrow CPR {bottom:.1f}-{top:.1f} broken {side} with VWAP "
                                         f"{vw:.1f}, close {c:.1f}", plan)
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
        FirstCandleBreakoutStrategy(underlying),
        EMARSIPullback15mStrategy(underlying),
        PivotBounceStrategy(underlying),
        MomentumBurstStrategy(underlying),
        CPRVWAP(underlying),
    ]


# Re-exported for compatibility; the canonical tuple lives in app.strategies.names.
STRATEGY_NAMES = ALL_STRATEGY_NAMES
