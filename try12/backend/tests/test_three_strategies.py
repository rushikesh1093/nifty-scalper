"""
The three strategies (VWAP_EMA_PULLBACK, ORB_BREAKOUT, SUPERTREND_EMA): entry
rules on synthetic NIFTY candles, their bar exits, and the per-day trade
rules. Engine-side plan exits live in test_exit_plan_engine.py.
"""
from datetime import datetime, time as dtime, timedelta, timezone

import pytest

from app.runtime.context import using
from app.services.trading import exit_plan as xp
from app.strategies.base import Direction
from app.strategies.indicators import compute_all, resample
from app.strategies.strategies import (
    ORBBreakoutStrategy, SupertrendEMAStrategy, VWAPEMAPullbackStrategy, build_strategies,
)
from tests.helpers import FrozenClock, make_test_context, run

IST = timezone(timedelta(hours=5, minutes=30))
OPEN = datetime(2026, 9, 1, 9, 15, tzinfo=IST)      # a Tuesday


def expand(bars, minutes, start=OPEN):
    """(o, h, l, c) bars → 1-min candles; each bar's first minute carries its range."""
    out, t = [], start
    for (o, h, l, c) in bars:
        for k in range(minutes):
            px = (o, h, l, c) if k == 0 else (c, c, c, c)
            out.append(dict(timestamp=t.isoformat(), open=px[0], high=px[1], low=px[2],
                            close=px[3], volume=0))
            t += timedelta(minutes=1)
    return out


def mirror(bars, pivot=44000.0):
    """Reflect bars through *pivot*: an up-move becomes the same down-move."""
    return [(pivot - o, pivot - l, pivot - h, pivot - c) for (o, h, l, c) in bars]


def at(minutes_after_open: int):
    return using(make_test_context(clock=FrozenClock(OPEN + timedelta(minutes=minutes_after_open))))


def evaluate(strat, candles):
    return run(strat.evaluate(candles, compute_all(candles)))


# ── resample ─────────────────────────────────────────────────────────────────

def test_resample_aligns_to_0915_and_drops_the_incomplete_bar():
    c = expand([(1, 2, 0, 1)] * 7, 1)                    # 09:15 … 09:21
    bars = resample(c, 3)
    assert [b["start"].strftime("%H:%M") for b in bars] == ["09:15", "09:18"]
    bars5 = resample(c, 5)
    assert len(bars5) == 1 and bars5[0]["end"].strftime("%H:%M") == "09:20"


def test_build_strategies_is_nifty_only():
    assert [s.name for s in build_strategies("NIFTY")][:3] == [
        "VWAP_EMA_PULLBACK", "ORB_BREAKOUT", "SUPERTREND_EMA"]       # + four more (test_new_strategies)
    assert build_strategies("BANKNIFTY") == []


# ── 1. VWAP + EMA pullback (3-min) ───────────────────────────────────────────

def pullback_bars():
    """A 3-min up-trend (+6 / −3 bars), a pullback bar to EMA 9, then a green
    trigger back above it — closes at 10:33."""
    bars, p = [], 22000.0
    for s in [6, -3] * 12:
        o, c = p, p + s
        bars.append((o, max(o, c) + 1, min(o, c) - 1, c))
        p = c
    bars.append((p, p + 1, 22034.0, p - 4))             # pullback: low within 5 of EMA 9
    bars.append((p - 4, p + 8, p - 5, p + 6))           # trigger: green, back above EMA 9
    return bars


def test_pullback_long_fires_with_spot_plan():
    bars = pullback_bars()
    c = expand(bars, 3)
    with at(3 * len(bars)):
        strat = VWAPEMAPullbackStrategy()
        sig = evaluate(strat, c)
        assert sig is not None and sig.direction == Direction.BUY
        plan = xp.ExitPlan.from_dict(sig.exit_plan)
        assert plan.side == "BUY" and plan.spot_entry == 22042.0
        assert plan.spot_stop == 22031.0                       # pullback bar low (trigger low − 0)
        assert plan.spot_t1 == pytest.approx(22042.0 + 1.5 * 11.0)
        assert sig.sl_pct == 0.20 and sig.target_pct == pytest.approx(0.40)
        assert evaluate(strat, c) is None                      # once per bar


def test_pullback_short_is_the_mirror():
    bars = mirror(pullback_bars())
    c = expand(bars, 3)
    with at(3 * len(bars)):
        sig = evaluate(VWAPEMAPullbackStrategy(), c)
    assert sig is not None and sig.direction == Direction.SELL
    plan = xp.ExitPlan.from_dict(sig.exit_plan)
    assert plan.spot_stop > plan.spot_entry > plan.spot_t1


def test_pullback_outside_the_trading_windows_is_ignored():
    bars = [(22000.0, 22001.0, 21999.0, 22000.0)] * 40 + pullback_bars()   # trigger at 12:33
    c = expand(bars, 3)
    with at(3 * len(bars)):
        assert evaluate(VWAPEMAPullbackStrategy(), c) is None


def test_pullback_needs_no_close_below_ema21():
    bars = pullback_bars()
    o, h, l, c_ = bars[-2]
    bars[-2] = (o, h, 21990.0, 21995.0)                      # pullback closes through EMA 21
    c = expand(bars, 3)
    with at(3 * len(bars)):
        assert evaluate(VWAPEMAPullbackStrategy(), c) is None


def test_pullback_bar_exits_vwap_then_ema_trail_after_t1():
    strat = VWAPEMAPullbackStrategy()
    bars = pullback_bars()
    trigger = (OPEN + timedelta(minutes=3 * (len(bars) - 1))).isoformat()

    def plan(**kw):
        return xp.ExitPlan(strategy=strat.name, side="BUY", spot_entry=22042.0,
                           spot_stop=22031.0, spot_t1=22058.5, last_bar_start=trigger, **kw)

    # a bar that closes below EMA 9 but above VWAP: held before T1, exit after T1
    dip = bars + [(22042.0, 22043.0, 22020.0, 22028.0)]
    c = expand(dip, 3)
    with at(3 * len(dip)):
        ind = compute_all(c)
        assert ind["vwap"] < 22028.0
        assert strat.candle_exit(plan(), c, ind) is None
        assert strat.candle_exit(plan(t1_done=True), c, ind) == xp.EMA_TRAIL
        p = plan()
        assert strat.candle_exit(p, c, ind) is None
        assert strat.candle_exit(p, c, dict(ind, vwap=22030.0)) is None   # bar already seen
        # mid-bar (the next 3-min bar has not closed): no new bar, no bar exit
        mid = c + expand([(22028.0,) * 4], 1, OPEN + timedelta(minutes=3 * len(dip)))
        assert strat.candle_exit(plan(t1_done=True), mid, ind) == xp.EMA_TRAIL
        q = plan(t1_done=True)
        strat.candle_exit(q, c, ind)
        assert strat.candle_exit(q, mid, ind) is None


# ── 2. Opening range breakout (5-min) ────────────────────────────────────────

OR_BARS = [(22030, 22060, 22020, 22040), (22040, 22055, 22000, 22010),
           (22010, 22050, 22005, 22045)]                    # OR 22000-22060, width 60


def orb_candles(*after):
    return expand(OR_BARS + list(after), 5)


def test_orb_breakout_long_plan():
    c = orb_candles((22045, 22058, 22030, 22050), (22050, 22080, 22048, 22075))
    with at(25):
        strat = ORBBreakoutStrategy()
        assert strat.opening_range(c) == (22060.0, 22000.0)
        sig = evaluate(strat, c)
    assert sig.direction == Direction.BUY
    plan = xp.ExitPlan.from_dict(sig.exit_plan)
    assert plan.spot_stop == 22045.0                          # OR high - width/4
    assert plan.spot_target == 22120.0                        # OR high + width
    assert plan.breakeven_trigger == pytest.approx(22075 + 0.5 * (22120 - 22075))
    assert plan.time_stop_minutes == 90 and sig.sl_pct == 0.25


def test_orb_width_filter():
    narrow = [(22030, 22040, 22020, 22030)] * 3              # width 20 < 40
    c = expand(narrow + [(22030, 22035, 22025, 22030), (22030, 22060, 22029, 22055)], 5)
    with at(25):
        assert evaluate(ORBBreakoutStrategy(), c) is None


def test_orb_no_entry_after_1100():
    flat = [(22030, 22040, 22020, 22030)] * 18               # 09:30 … 11:00 inside the range
    c = orb_candles(*flat, (22030, 22080, 22029, 22075))     # breaks on the 11:00-11:05 bar
    with at(5 * (3 + 19)):
        assert evaluate(ORBBreakoutStrategy(), c) is None


def test_orb_one_trade_per_direction_and_reverse_only_after_a_stop():
    strat = ORBBreakoutStrategy()
    with at(25):
        strat._roll_day()
        assert strat._allowed(Direction.BUY) and strat._allowed(Direction.SELL)
        strat.on_position_opened(type("S", (), {"direction": Direction.BUY})())
        strat.position_closed(Direction.BUY, "TARGET")
        assert not strat._allowed(Direction.BUY) and not strat._allowed(Direction.SELL)

        strat._trades = []
        strat.on_position_opened(type("S", (), {"direction": Direction.BUY})())
        strat.position_closed(Direction.BUY, xp.SPOT_SL)
        assert strat._allowed(Direction.SELL) and not strat._allowed(Direction.BUY)
        strat.on_position_opened(type("S", (), {"direction": Direction.SELL})())
        strat.position_closed(Direction.SELL, xp.SPOT_SL)
        assert not strat._allowed(Direction.BUY) and not strat._allowed(Direction.SELL)


# ── 3. Supertrend + EMA 5/13 (1-min) ─────────────────────────────────────────

def st_bars(rally: int):
    """60 drifting-down 1-min bars, then *rally* +9 bars."""
    bars, p = [], 22100.0
    for i in range(60):
        s = -3 if i % 3 else 1
        o, c = p, p + s
        bars.append((o, max(o, c) + 1, min(o, c) - 1, c))
        p = c
    for _ in range(rally):
        bars.append((p, p + 10, p - 1, p + 9))
        p += 9
    return bars


def test_supertrend_fires_only_on_the_confirmation_candle():
    fired = []
    for k in range(1, 8):
        bars = st_bars(k)
        with at(len(bars)):
            sig = evaluate(SupertrendEMAStrategy(), expand(bars, 1))
        fired.append(sig is not None)
        if sig is not None:
            plan = xp.ExitPlan.from_dict(sig.exit_plan)
            assert sig.direction == Direction.BUY
            assert plan.spot_stop < plan.spot_entry
            assert plan.premium_target_pts == 8.0 and plan.time_stop_minutes == 10
    assert fired.count(True) == 1


def test_supertrend_short_is_the_mirror():
    hits = []
    for k in range(1, 8):
        bars = mirror(st_bars(k))
        with at(len(bars)):
            sig = evaluate(SupertrendEMAStrategy(), expand(bars, 1))
        if sig is not None:
            hits.append(sig.direction)
    assert hits == [Direction.SELL]


def test_supertrend_cooldown_and_daily_cap():
    with at(100):
        strat = SupertrendEMAStrategy()
        strat._roll_day()
        strat._trades_today = 6
        bars = st_bars(3)
        assert run(strat.evaluate(expand(bars, 1), {})) is None   # max 6 a day
        strat._trades_today = 0
        strat.position_closed(Direction.BUY, xp.TARGET)            # exit just now
        strat._last_bar_start = None
        assert run(strat.evaluate(expand(bars, 1), {})) is None   # 3-bar cooldown


def test_supertrend_flip_against_is_a_bar_exit_and_the_stop_trails():
    strat = SupertrendEMAStrategy()
    bars = st_bars(6)
    c = expand(bars, 1)
    seen = (OPEN + timedelta(minutes=len(bars) - 2)).isoformat()     # one new bar to check
    plan = xp.ExitPlan(strategy=strat.name, side="BUY", spot_entry=22050.0,
                       spot_stop=21900.0, premium_target_pts=20.0, time_stop_minutes=10,
                       last_bar_start=seen)
    with at(len(bars)):
        assert strat.candle_exit(plan, c, {}) is None
        assert plan.spot_stop > 21900.0                            # trailed to the ST line
        short = xp.ExitPlan.from_dict(dict(plan.to_dict(), side="SELL", spot_stop=23000.0,
                                           last_bar_start=seen))
        assert strat.candle_exit(short, c, {}) == xp.ST_FLIP
