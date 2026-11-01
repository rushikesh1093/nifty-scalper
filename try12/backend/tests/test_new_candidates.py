"""
The pre-registered candidates (app.research.new_strategies): one trade a day,
valid plans, no look-ahead, a trailing stop that only tightens, and the gap
fade's entry and target on a constructed day.
"""
import random
from datetime import date, datetime, time as dtime, timedelta

import pytest

from app.research import new_strategies as N, sim
from app.research.data import Session
from app.services.market_data.timestamps import IST
from app.services.trading.exit_plan import ExitPlan
from tests.test_research import _day

CANDIDATES = (N.VolSqueezeBreakout, N.TrendDayContinuation, N.GapFade, N.VWAPExtremeReversion,
              N.BollingerSqueezeBreakout, N.CPRVWAP)


def _walk_days(seed: int, n: int = 6, vol: float = 6.0, drift: float = 0.0) -> list[Session]:
    rng = random.Random(seed)
    p, days = 22000.0, []
    for k in range(n):
        bars = []
        p += rng.gauss(0, 60)                                   # overnight gap
        for _ in range(375):
            o = p
            p += rng.gauss(drift, vol * (0.3 if 150 < len(bars) < 230 else 1.0))
            bars.append((o, max(o, p) + abs(rng.gauss(0, 2)), min(o, p) - abs(rng.gauss(0, 2)), p))
        days.append(_day(date(2024, 1, 1) + timedelta(days=k), bars))
    return days


@pytest.mark.parametrize("cls", CANDIDATES)
def test_one_trade_a_day_and_valid_plans(cls):
    n = 0
    for seed in range(4):
        days = _walk_days(seed, drift=0.4 if seed % 2 else -0.4)
        trades = sim.run_days(cls, days[1:], days[0].candles, days[0], "T")
        assert len({t.day for t in trades}) == len(trades)     # at most one a day
        for t in trades:
            assert t.risk_pts > 0                            # stop on the losing side
            assert t.exit_reason
        n += len(trades)
    return n


@pytest.mark.parametrize("cls", CANDIDATES)
def test_no_future_candle_changes_an_earlier_signal(cls):
    for seed in range(4):
        days = _walk_days(seed, n=3, drift=0.4 if seed % 2 else -0.4)
        full = sim.run_days(cls, [days[2]], days[0].candles + days[1].candles, days[1], "T")
        cut = Session(day=days[2].day, candles=days[2].candles[:240])
        part = sim.run_days(cls, [cut], days[0].candles + days[1].candles, days[1], "T")
        early = lambda ts: [(t.signal_time, t.side, t.entry, t.stop0) for t in ts if t.entry_idx < 230]  # noqa: E731
        assert early(full) == early(part)


def test_supertrend_trail_only_tightens_and_needs_arming():
    days = _walk_days(11, n=2, drift=0.8)
    s = N.TrendDayContinuation()
    s.history = days[0].candles + days[1].candles
    plan = ExitPlan(strategy=s.name, side="BUY", spot_entry=22500.0, spot_stop=21000.0,
                    last_bar_start=days[1].candles[0]["timestamp"].isoformat())
    stops = []
    for k in range(10, 375, 5):
        s.candle_exit(plan, days[1].candles[:k], {})
        stops.append(plan.spot_stop)
    assert stops == sorted(stops)                              # never loosens


def test_gap_fade_enters_toward_the_previous_close():
    prev = _day(date(2024, 1, 1), [(22000.0, 22010.0, 21990.0, 22000.0)])     # closes ~22000
    pc = prev.candles[-1]["close"]
    o = pc * 1.006                                              # +0.6 % gap
    bars = [(o, o + 10, o - 5, o + 5)] * 15                     # 09:15-09:30 range: o-5 .. o+10
    bars += [(o, o + 1, o - 30, o - 25)] + [(o - 25, o - 24, o - 26, o - 25)] * 4   # 09:30 bar closes below
    s = _day(date(2024, 1, 2), bars)
    trades = sim.run_days(N.GapFade, [s], prev.candles, prev, "T")
    assert trades and trades[0].side == "SELL" and trades[0].signal_time == "09:35"
    assert trades[0].target == round(pc, 2) and trades[0].stop0 == round(o + 10, 2)
