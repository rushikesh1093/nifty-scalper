"""
Experiment A simulator: the live strategy classes driven minute by minute
over recorded 1-min spot candles, one strategy at a time, one position at a
time (the strategies' own one-position rule).

Causality
---------
At clock time T (one second after a 1-min candle closes) a strategy sees the
candles that closed before T, exactly as strategy_runner hands them over
(``candles`` = today's, ``history`` = the last 500 session candles). A
signal at T fills at the OPEN of the candle starting at T. The position is
then stepped through each later candle on a pessimistic path (open, the
adverse extreme, the favourable extreme, close — the replay engine's
PESSIMISTIC_LOW_FIRST order), calling the live ``exit_plan.tick_action`` at
every point. So a candle that touches both the stop and the target is a stop,
and the trade is flagged ``ambiguous``. Bar exits (Supertrend flip / trail,
EMA trail, VWAP exit) come from the strategy's own ``candle_exit`` at each
candle close, at that close.

Live behaviour reproduced: the 11:30-12:15 entry blackout and the 15:05
entry cutoff (risk_gate), the 15:15 square-off, the strategies' own windows,
per-day trade rules and exit plans. Not reproduced (portfolio rules, judged
separately): max trades a day across strategies, max open positions, daily
loss halt, regime enablement, capital and option selection.

One-lot reality: with ONE lot the live T1 partial books nothing and only
moves the stop to entry (paper_engine._skip_partial); that is what is
simulated. The Supertrend premium-point target is mapped to spot as
``premium_target_pts / ASSUMED_DELTA``.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, time as dtime, timedelta, timezone
from typing import Callable, Optional

import numpy as np

from app.core.trading_params import TradingParams
from app.research.data import Session
from app.runtime.context import RunConfig, RuntimeContext, bind, unbind
from app.services.replay.ports_impl import ReplayClock, ReplayTradingParams
from app.services.trading import exit_plan as xp
from app.strategies.base import Direction

HISTORY_CANDLES = 500                     # strategy_runner reads 500 cached candles
ASSUMED_DELTA = 0.5
SQUARE_OFF = dtime(15, 15)
ENTRY_CUTOFF = dtime(15, 5)               # square-off 15:15 - entry_cutoff_buffer_min 10
BLACKOUT = ((dtime(11, 30), dtime(12, 15)),)
EXECUTION_MODEL_VERSION = "spot-pessimistic-path-v1"


@dataclass
class Trade:
    strategy: str
    variant: str
    day: str
    side: str                               # BUY (CE) / SELL (PE)
    signal_time: str                        # IST, the close of the signal candle
    entry_time: str                         # IST, open of the fill candle
    plan_entry: float                       # spot close at the signal
    entry: float                            # spot open of the fill candle
    stop0: float
    target: Optional[float]
    risk_pts: float                         # sign × (plan_entry − stop0), the plan's 1R
    exit_time: str = ""
    exit_px: float = 0.0
    exit_reason: str = ""
    ambiguous: bool = False                 # a candle touched stop and target/T1
    r_gross: float = 0.0                    # sign × (exit − entry) / risk_pts
    hold_min: float = 0.0
    mfe_r: float = 0.0                      # best / worst spot excursion while held
    mae_r: float = 0.0
    entry_idx: int = 0                      # candle index in the session (fill candle)
    exit_idx: int = 0
    notes: str = ""
    features: dict = field(default_factory=dict)

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    def row(self) -> dict:
        d = asdict(self)
        feats = d.pop("features")
        d.update({f"f_{k}": v for k, v in feats.items()})
        return d


def run_sync(coro):
    """Drive a coroutine that never awaits I/O (Strategy.evaluate) to completion."""
    try:
        coro.send(None)
    except StopIteration as stop:
        return stop.value
    coro.close()
    raise RuntimeError("strategy awaited I/O; the research harness cannot drive it")


def research_context(clock: ReplayClock, params: Optional[TradingParams] = None) -> RuntimeContext:
    p = params or TradingParams()
    cfg = RunConfig(mode="PAPER", capital=p.capital, max_trades_per_day=p.max_trades_per_day,
                    max_open_positions=p.max_open_positions, max_lots_per_trade=p.max_lots_per_trade,
                    max_daily_loss=p.max_daily_loss, max_daily_loss_pct=p.max_daily_loss_pct,
                    open_exposure_cap_pct=p.open_exposure_cap_pct, square_off_time="15:15",
                    entry_cutoff_buffer_min=p.entry_cutoff_buffer_min)
    return RuntimeContext(run_id="RESEARCH", clock=clock, market=None, sessions=None,
                          regime_config=None, params=ReplayTradingParams(p), cfg=cfg,
                          user_flags=None, instruments=None, control=None, is_replay=True)


def _blocked(t: dtime) -> bool:
    if t >= ENTRY_CUTOFF:
        return True
    return any(s <= t < e for s, e in BLACKOUT)


def _path(c: dict, side: str) -> tuple[float, float, float, float]:
    if side == "BUY":
        return c["open"], c["low"], c["high"], c["close"]
    return c["open"], c["high"], c["low"], c["close"]


def _evaluates_at(strategy, k: int) -> bool:
    """A strategy of BAR_MINUTES N can only see a new bar when k (candles
    closed since 09:15) is a multiple of N; calling it in between is a no-op
    in live (_new_bar), so the harness skips the call."""
    n = getattr(strategy, "BAR_MINUTES", 1) or 1
    return k % n == 0


def _prev_session_levels(prev: Session):
    from app.services.market_data.prev_session import PrevSession
    return PrevSession(day=prev.day, high=max(c["high"] for c in prev.candles),
                       low=min(c["low"] for c in prev.candles),
                       close=prev.candles[-1]["close"], source="research", detail="")


class _OpenPosition:
    def __init__(self, trade: Trade, plan: xp.ExitPlan, direction: Direction,
                 entry_dt: datetime, premium_target_spot: Optional[float]):
        self.trade, self.plan, self.direction = trade, plan, direction
        self.entry_dt = entry_dt
        self.premium_target_spot = premium_target_spot
        self.best = self.worst = trade.entry


def run_days(factory: Callable[[], object], sessions: list[Session], prev_tail: list[dict],
             prev_session: Optional[Session], variant: str,
             feature_fn: Optional[Callable] = None) -> list[Trade]:
    """Run one strategy over consecutive *sessions*. *prev_tail* is the candle
    history before the first session (indicator warm-up); *prev_session* the
    session before it (pivot levels)."""
    strat = factory()
    clock = ReplayClock(datetime(2000, 1, 1, tzinfo=timezone.utc))
    token = bind(research_context(clock))
    trades: list[Trade] = []
    try:
        tail = list(prev_tail[-HISTORY_CANDLES:])
        prior = prev_session
        for sess in sessions:
            if getattr(strat, "NEEDS_PREV_SESSION", False):
                clock.set(sess.candles[0]["timestamp"])
                strat.set_prev_session(sess.day, _prev_session_levels(prior) if prior else None,
                                       None if prior else "no previous session in the dataset")
            trades.extend(_run_session(strat, sess, tail, clock, variant, feature_fn))
            tail = (tail + sess.candles)[-HISTORY_CANDLES:]
            prior = sess
    finally:
        unbind(token)
    return trades


def _run_session(strat, sess: Session, tail: list[dict], clock: ReplayClock, variant: str,
                 feature_fn) -> list[Trade]:
    candles = sess.candles
    n = len(candles)
    out: list[Trade] = []
    pos: Optional[_OpenPosition] = None
    for k in range(1, n + 1):
        last = candles[k - 1]
        close_dt = last["timestamp"] + timedelta(minutes=1)
        clock.set(close_dt + timedelta(seconds=1))
        today = candles[:k]
        history = (tail + today)[-HISTORY_CANDLES:]

        # 1. the position's path through the candle that just closed
        if pos is not None and k - 1 >= pos.trade.entry_idx:
            if _step_candle(pos, last, k - 1):
                out.append(_close(strat, pos))
                pos = None
        # 2. bar exits at the close
        if pos is not None and k - 1 >= pos.trade.entry_idx:
            strat.history = history
            reason = strat.candle_exit(pos.plan, today, {})
            if reason:
                _finish(pos, k - 1, last["close"], reason, close_dt)
                out.append(_close(strat, pos))
                pos = None
        if pos is not None and k == n:                       # data ends before square-off
            _finish(pos, k - 1, last["close"], "DATA_END", close_dt)
            out.append(_close(strat, pos))
            pos = None
        # 3. a new signal, flat only, on the strategy's bar boundary
        if pos is not None or k >= n or not _evaluates_at(strat, k):
            continue
        strat.history = history
        if hasattr(strat, "futures_candles"):
            strat.futures_candles = [c for c in sess.fut_candles if c["timestamp"] < close_dt]
        sig = run_sync(strat.evaluate(today, {}))
        if sig is None:
            continue
        sig_t = close_dt.time().replace(tzinfo=None)
        if _blocked(sig_t):
            continue                                          # risk gate refuses; no state change
        plan = xp.ExitPlan.from_dict(sig.exit_plan)
        fill = candles[k]
        side = "BUY" if sig.direction == Direction.BUY else "SELL"
        risk = plan.sign * (plan.spot_entry - plan.spot_stop)
        tr = Trade(strategy=strat.name, variant=variant, day=str(sess.day), side=side,
                   signal_time=close_dt.strftime("%H:%M"), entry_time=fill["timestamp"].strftime("%H:%M"),
                   plan_entry=plan.spot_entry, entry=fill["open"], stop0=plan.spot_stop,
                   target=plan.spot_target, risk_pts=round(risk, 2), entry_idx=k, notes=sig.notes or "")
        if feature_fn is not None:
            tr.features = feature_fn(sess, k, history, today, tr)
        pts = None
        if plan.premium_target_pts:
            pts = tr.entry + plan.sign * plan.premium_target_pts / ASSUMED_DELTA
        pos = _OpenPosition(tr, plan, sig.direction, fill["timestamp"], pts)
        strat.open_positions += 1
        strat.on_position_opened(sig)
        # the fill candle itself is stepped on the next loop pass
    return out


def _finish(pos: _OpenPosition, idx: int, px: float, reason: str, when: datetime) -> None:
    t = pos.trade
    t.exit_idx, t.exit_px, t.exit_reason = idx, round(px, 2), reason
    t.exit_time = when.strftime("%H:%M")
    t.hold_min = round((when - pos.entry_dt).total_seconds() / 60, 1)
    t.r_gross = round(t.sign * (px - t.entry) / t.risk_pts, 4) if t.risk_pts > 0 else 0.0
    t.mfe_r = round(t.sign * (pos.best - t.entry) / t.risk_pts, 4) if t.risk_pts > 0 else 0.0
    t.mae_r = round(t.sign * (pos.worst - t.entry) / t.risk_pts, 4) if t.risk_pts > 0 else 0.0


def _close(strat, pos: _OpenPosition):
    strat.open_positions = max(0, strat.open_positions - 1)
    strat.on_position_closed(pos.direction, pos.trade.exit_reason)
    return pos.trade


def _step_candle(pos: _OpenPosition, c: dict, idx: int) -> bool:
    """Walk one candle's pessimistic path. True when the position closed."""
    plan, t = pos.plan, pos.trade
    start = c["timestamp"]
    now_t = start.time().replace(tzinfo=None)
    hold = (start - pos.entry_dt).total_seconds() / 60
    if now_t >= SQUARE_OFF:
        _finish(pos, idx, c["open"], "EOD", start)
        return True
    touched_fav = False
    for j, px in enumerate(_path(c, t.side)):
        if t.sign * (px - pos.best) > 0:
            pos.best = px
        if t.sign * (px - pos.worst) < 0:
            pos.worst = px
        if pos.premium_target_spot is not None and xp.reached(t.side, px, pos.premium_target_spot):
            _finish(pos, idx, pos.premium_target_spot if j else px, xp.TARGET, start)
            return True
        action = xp.tick_action(plan, px, hold, now_t)
        if action == xp.SPOT_SL:
            fill = px if xp.breached(t.side, px, plan.spot_stop) and j == 0 else plan.spot_stop
            fav = _path(c, t.side)[2]
            t.ambiguous = t.ambiguous or touched_fav or any(
                lvl is not None and xp.reached(t.side, fav, lvl)
                for lvl in (plan.spot_target, None if plan.t1_done else plan.spot_t1,
                            pos.premium_target_spot))
            _finish(pos, idx, fill, xp.SPOT_SL, start)
            return True
        if action == xp.TARGET:
            _finish(pos, idx, plan.spot_target if j else px, xp.TARGET, start)
            return True
        if action in (xp.TIME_EXIT, xp.SESSION_EXIT):
            _finish(pos, idx, px, action, start)
            return True
        if action == xp.T1_PARTIAL:
            plan.t1_done = True                    # one lot: nothing booked, stop to entry
            xp.move_stop_to_entry(plan)
            touched_fav = True
        elif action == xp.BREAKEVEN:
            plan.breakeven_done = True
            xp.move_stop_to_entry(plan)
            touched_fav = True
    return False


# ── Excursions to the session horizon (signal quality, independent of exits) ─

def excursions(sess: Session, t: Trade, horizon: dtime = SQUARE_OFF) -> dict:
    """From the fill candle to *horizon*, ignoring the plan's exits: MFE/MAE
    in R, minutes to +1R / +2R / the initial stop, which came first (a candle
    touching both counts as the stop: never the favourable outcome), and
    the 30-minute directional result."""
    c = sess.candles
    s, e0, r = t.sign, t.entry, t.risk_pts
    out = {"mfe_eod_r": 0.0, "mae_eod_r": 0.0, "t_1r": None, "t_2r": None, "t_stop": None,
           "first": "NEITHER", "dir_30m": None}
    if r <= 0:
        return out
    best = worst = 0.0
    for i in range(t.entry_idx, len(c)):
        if c[i]["timestamp"].time().replace(tzinfo=None) >= horizon:
            break
        fav = s * ((c[i]["high"] if s > 0 else c[i]["low"]) - e0) / r
        adv = s * ((c[i]["low"] if s > 0 else c[i]["high"]) - e0) / r
        best, worst = max(best, fav), min(worst, adv)
        m = i - t.entry_idx + 1
        stop_hit = s * ((c[i]["low"] if s > 0 else c[i]["high"]) - t.stop0) <= 0
        if out["t_stop"] is None and stop_hit:
            out["t_stop"] = m
        if out["t_1r"] is None and fav >= 1:
            out["t_1r"] = m
        if out["t_2r"] is None and fav >= 2:
            out["t_2r"] = m
        if out["first"] == "NEITHER":
            if stop_hit:
                out["first"] = "STOP"
            elif fav >= 2:
                out["first"] = "2R"
        if i == t.entry_idx + 29:
            out["dir_30m"] = 1 if s * (c[i]["close"] - e0) > 0 else 0
    out["mfe_eod_r"], out["mae_eod_r"] = round(best, 4), round(worst, 4)
    return out


# ── Exit-engine variants on fixed entries (Part 8) ───────────────────────────

EXIT_VARIANTS = ("LIVE", "A_FIXED_2R", "B_BE1R_2R", "C_BE1R_ATR_TRAIL", "D_SWING_TRAIL",
                 "E_ATR_CHANDELIER", "F_ST5_TRAIL")


def _bars5(candles: list[dict]) -> list[dict]:
    from app.strategies.indicators import resample
    return resample(candles, 5)


def walk_exit(sess: Session, t: Trade, variant: str, history: list[dict]) -> dict:
    """Re-run the trade *t*'s entry with exit *variant*. Stops only tighten
    (never past the initial stop), trailing levels are computed from closed
    candles and apply from the next candle, the path is pessimistic, and the
    position squares off at 15:15. Returns {r, reason, hold_min, exit_idx,
    ambiguous}."""
    from app.strategies.indicators import atr, supertrend
    c = sess.candles
    s, entry, r = t.sign, t.entry, t.risk_pts
    stop = t.stop0
    target = entry + s * 2 * r if variant in ("A_FIXED_2R", "B_BE1R_2R") else None
    be_armed = variant in ("B_BE1R_2R", "C_BE1R_ATR_TRAIL", "D_SWING_TRAIL")
    hist5 = _bars5(history + c[:t.entry_idx])
    a5 = atr(np.array([b["high"] for b in hist5]), np.array([b["low"] for b in hist5]),
             np.array([b["close"] for b in hist5]), 14) if len(hist5) >= 15 else np.array([np.nan])
    atr_entry = float(a5[-1]) if len(a5) and np.isfinite(a5[-1]) else r
    hi_close = entry
    hi_ext = entry
    reached_1r = False
    ambiguous = False

    def tighten(level):
        nonlocal stop
        if level is not None and s * (level - stop) > 0:
            stop = level

    for i in range(t.entry_idx, len(c)):
        cd = c[i]
        if cd["timestamp"].time().replace(tzinfo=None) >= SQUARE_OFF:
            return _res(cd["open"], "EOD", i, t, ambiguous)
        fav_seen = False
        for j, px in enumerate(_path(cd, t.side)):
            if s * (px - stop) <= 0:
                fill = px if j == 0 else stop
                fav = _path(cd, t.side)[2]
                hit_tgt = target is not None and s * (fav - target) >= 0
                return _res(fill, "STOP" if stop == t.stop0 else "TRAIL_STOP", i, t,
                            ambiguous or fav_seen or hit_tgt)
            if target is not None and s * (px - target) >= 0:
                return _res(target if j else px, "TARGET", i, t, ambiguous)
            if be_armed and not reached_1r and s * (px - entry) >= r:
                reached_1r = True
                fav_seen = True
                tighten(entry)
            elif s * (px - entry) >= r:
                reached_1r = True
        # end of candle: trailing levels from closed data only
        hi_close = max(hi_close, cd["close"]) if s > 0 else min(hi_close, cd["close"])
        hi_ext = max(hi_ext, cd["high"]) if s > 0 else min(hi_ext, cd["low"])
        if variant == "C_BE1R_ATR_TRAIL" and reached_1r:
            tighten(hi_close - s * 1.5 * atr_entry)
        elif variant == "E_ATR_CHANDELIER":
            tighten(hi_ext - s * 2.0 * atr_entry)
        elif variant in ("D_SWING_TRAIL", "F_ST5_TRAIL"):
            minute = cd["timestamp"].minute + cd["timestamp"].hour * 60 - (9 * 60 + 15) + 1
            if minute % 5:
                continue
            b5 = _bars5(history[-200:] + c[:i + 1])
            if variant == "D_SWING_TRAIL" and reached_1r and len(b5) >= 5:
                # a swing confirmed by two closed bars after it (no look-ahead)
                for j in range(len(b5) - 3, 1, -1):
                    if b5[j]["start"] < c[t.entry_idx]["timestamp"] - timedelta(minutes=5):
                        break
                    lo = [b["low"] if s > 0 else -b["high"] for b in b5[j - 2:j + 3]]
                    if lo[2] < min(lo[:2] + lo[3:]):
                        tighten(b5[j]["low"] if s > 0 else b5[j]["high"])
                        break
            elif variant == "F_ST5_TRAIL" and len(b5) >= 15:
                st = supertrend(np.array([b["high"] for b in b5]), np.array([b["low"] for b in b5]),
                                np.array([b["close"] for b in b5]), 10, 3.0)
                if np.isfinite(st.supertrend[-1]):
                    if int(st.direction[-1]) != s:
                        return _res(cd["close"], "ST_FLIP", i, t, ambiguous)
                    tighten(float(st.supertrend[-1]))
    return _res(c[-1]["close"], "DATA_END", len(c) - 1, t, ambiguous)


def _res(px: float, reason: str, i: int, t: Trade, ambiguous: bool) -> dict:
    return {"r": round(t.sign * (px - t.entry) / t.risk_pts, 4) if t.risk_pts > 0 else 0.0,
            "reason": reason, "exit_idx": i, "hold_min": float(i - t.entry_idx + 1),
            "ambiguous": ambiguous}
