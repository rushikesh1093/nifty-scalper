"""
The four strategies added 23 Oct and the shared risk rules that came with them:
09:15 candle breakout, 15-min EMA/RSI pullback, pivot bounce, 5-min momentum
burst; a per-plan square-off time; the 11:30-12:15 entry blackout; and the
"ATM or one strike OTM" strike limit.
"""
from datetime import time as dtime, timedelta

import numpy as np
import pytest

from app.services.trading import exit_plan as xp
from app.strategies import strategies as strat_mod
from app.strategies.base import Direction
from app.strategies.strategies import (
    EMARSIPullback15mStrategy, FirstCandleBreakoutStrategy, MomentumBurstStrategy,
    PivotBounceStrategy, build_strategies,
)
from tests.helpers import run
from tests.test_three_strategies import OPEN, at, evaluate, expand


def test_seven_strategies_are_registered():
    names = [s.name for s in build_strategies("NIFTY")]
    assert names[-4:] == ["FIRST_CANDLE_BREAKOUT", "EMA_RSI_PULLBACK_15M", "PIVOT_BOUNCE",
                          "MOMENTUM_BURST"]
    from app.services.trading.regime_rules import REGIME_RULES
    enabled = set().union(*(r.enabled_strategies for r in REGIME_RULES.values()))
    assert set(names) <= enabled                     # every strategy can trade somewhere


# ── A. 09:15 candle breakout ─────────────────────────────────────────────────

FIRST = (22000.0, 22040.0, 21980.0, 22010.0)          # range 60


def test_first_candle_breakout_long_plan():
    c = expand([FIRST, (22010.0, 22060.0, 22005.0, 22055.0)], 5)     # close > 22040 + 10
    with at(10):
        sig = evaluate(FirstCandleBreakoutStrategy(), c)
    assert sig.direction == Direction.BUY
    plan = xp.ExitPlan.from_dict(sig.exit_plan)
    assert plan.spot_stop == 22055.0 - 50.0                         # 09:15 low capped at 50 pts
    assert plan.spot_target == 22055.0 + 1.5 * 60
    assert plan.exit_at == "14:45" and sig.sl_pct == 0.25


def test_first_candle_needs_the_10_point_buffer_and_ends_at_1000():
    near = expand([FIRST, (22010.0, 22048.0, 22005.0, 22045.0)], 5)  # inside the buffer
    with at(10):
        assert evaluate(FirstCandleBreakoutStrategy(), near) is None
    late = expand([FIRST] + [(22010.0, 22030.0, 21990.0, 22010.0)] * 8
                  + [(22010.0, 22080.0, 22005.0, 22075.0)], 5)       # the 10:00-10:05 bar
    with at(50):
        assert evaluate(FirstCandleBreakoutStrategy(), late) is None


def test_first_candle_short_once_per_direction():
    c = expand([FIRST, (22000.0, 22002.0, 21960.0, 21965.0)], 5)     # close < 21980 - 10
    with at(10):
        s = FirstCandleBreakoutStrategy()
        sig = evaluate(s, c)
        assert sig.direction == Direction.SELL and sig.exit_plan["spot_stop"] == 21965.0 + 50.0
        s.on_position_opened(sig)
        c2 = c + expand([(21965.0, 21970.0, 21940.0, 21945.0)], 5, OPEN + timedelta(minutes=10))
    with at(15):
        assert evaluate(s, c2) is None


# ── B. 15-min EMA + RSI pullback ─────────────────────────────────────────────

def _fifteen():
    """15-min bars over two earlier sessions (25 a day) trending up, then today:
    four more up bars, a pullback that closes below EMA 9, and a close back above."""
    up, p = [], 21500.0
    for _ in range(54):
        up.append((p, p + 12, p - 4, p + 10)); p += 10
    pull = (p, p + 2, p - 80, p - 70)                    # pullback candle (low p-80)
    trig = (p - 70, p + 15, p - 72, p + 10)              # back above EMA 9
    return up, pull, trig


def _fifteen_candles():
    up, pull, trig = _fifteen()
    hist = expand(up[:25], 15, OPEN - timedelta(days=2)) + expand(up[25:50], 15, OPEN - timedelta(days=1))
    today = expand(up[50:] + [pull, trig], 15)           # 09:15 ... 10:45
    return hist, today, pull


def test_ema_rsi_pullback_long(monkeypatch):
    hist, today, pull = _fifteen_candles()
    real_rsi = strat_mod.rsi

    def fake_rsi(cl, n=14):
        r = real_rsi(cl, n)
        r[-2] = 45.0                                     # RSI touched 40-50 on the pullback
        return r
    monkeypatch.setattr(strat_mod, "rsi", fake_rsi)
    with at(90):
        s = EMARSIPullback15mStrategy()
        s.history = hist + today
        sig = run(s.evaluate(today, {}))
    assert sig is not None and sig.direction == Direction.BUY
    plan = xp.ExitPlan.from_dict(sig.exit_plan)
    risk = plan.spot_entry - plan.spot_stop
    assert plan.spot_stop == pytest.approx(pull[2])      # the pullback candle's low
    assert plan.spot_t1 == pytest.approx(plan.spot_entry + risk, abs=0.01)
    assert plan.spot_target == pytest.approx(plan.spot_entry + 2 * risk, abs=0.01)
    assert plan.exit_at == "14:45"


def test_ema_rsi_pullback_needs_the_rsi_touch(monkeypatch):
    hist, today, _ = _fifteen_candles()
    monkeypatch.setattr(strat_mod, "rsi", lambda cl, n=14: np.full(len(cl), 65.0))
    with at(90):
        s = EMARSIPullback15mStrategy()
        s.history = hist + today
        assert run(s.evaluate(today, {})) is None


# ── C. Pivot bounce ──────────────────────────────────────────────────────────

def _prev_day():
    """Yesterday: high 22100, low 21900, close 22000 -> PP 22000, R1 22100, S1 21900."""
    rows = []
    t = OPEN - timedelta(days=1)
    for k in range(375):
        px = 22000.0
        h, l = (22100.0, px) if k == 50 else ((px, 21900.0) if k == 100 else (px, px))
        rows.append(dict(timestamp=(t + timedelta(minutes=k)).isoformat(), open=px, high=h,
                         low=l, close=px, volume=0))
    return rows


def _pivot(today_candles=()):
    """A PivotBounceStrategy given yesterday's levels the way the runner does."""
    from app.runtime.context import trading_day
    from app.services.market_data import prev_session as ps

    s = PivotBounceStrategy()
    res = ps.resolve(trading_day(), _prev_day())
    s.set_prev_session(trading_day(), res.levels, res.reason)
    s.history = _prev_day() + list(today_candles)
    return s


def test_pivots_from_the_previous_session():
    with at(30):
        assert _pivot().pivots() == {"S1": 21900.0, "PP": 22000.0, "R1": 22100.0}


def test_pivot_bounce_long_at_pp_targets_r1():
    bars = [(22020.0, 22030.0, 22012.0, 22020.0)] * 6 + \
           [(22020.0, 22022.0, 22008.0, 22010.0),                    # prev close 22010 > PP
            (22004.0, 22016.0, 21995.0, 22012.0)]                    # dips to PP, closes up
    c = expand(bars, 3)
    with at(24):
        s = _pivot(c)
        sig = run(s.evaluate(c, {}))
    assert sig.direction == Direction.BUY
    plan = sig.exit_plan
    assert plan["spot_stop"] == 21985.0 and plan["spot_target"] == 22100.0
    assert plan["exit_at"] == "14:45"


def test_pivot_rejection_short_at_r1_targets_pp():
    bars = [(22080.0, 22090.0, 22070.0, 22080.0)] * 6 + \
           [(22080.0, 22095.0, 22078.0, 22090.0),                    # prev close < R1
            (22096.0, 22105.0, 22080.0, 22085.0)]                    # pokes R1, closes down
    c = expand(bars, 3)
    with at(24):
        s = _pivot(c)
        sig = run(s.evaluate(c, {}))
    assert sig.direction == Direction.SELL
    assert sig.exit_plan["spot_stop"] == 22115.0 and sig.exit_plan["spot_target"] == 22000.0


# ── D. Momentum burst ────────────────────────────────────────────────────────

def _burst_bars():
    hist = [(22000.0, 22015.0, 21985.0, 22000.0)] * 24                # ATR ~30
    today = [(22000.0, 22015.0, 21985.0, 22000.0)] * 3 + [
        (22000.0, 22030.0, 21998.0, 22025.0),                         # close > prev high 22015
        (22025.0, 22050.0, 22020.0, 22045.0),
        (22045.0, 22070.0, 22040.0, 22068.0)]
    return hist, today


def test_momentum_burst_long_plan():
    hist, today = _burst_bars()
    h = expand(hist, 5, OPEN - timedelta(days=1))
    t = expand(today, 5)
    with at(30):
        s = MomentumBurstStrategy()
        s.history = h + t
        sig = run(s.evaluate(t, {}))
    assert sig.direction == Direction.BUY
    plan = sig.exit_plan
    assert plan["spot_stop"] == 22040.0                               # third candle's low
    assert plan["spot_target"] > 22068.0 + 15 * 1.5
    assert plan["exit_at"] == "14:30"


def test_momentum_burst_needs_atr_above_15():
    hist = [(22000.0, 22004.0, 21996.0, 22000.0)] * 24                # ATR ~8
    today = [(22000.0, 22004.0, 21996.0, 22000.0)] * 3 + [
        (22000.0, 22010.0, 21999.0, 22008.0), (22008.0, 22016.0, 22007.0, 22014.0),
        (22014.0, 22022.0, 22013.0, 22020.0)]
    with at(30):
        s = MomentumBurstStrategy()
        t = expand(today, 5)
        s.history = expand(hist, 5, OPEN - timedelta(days=1)) + t
        assert run(s.evaluate(t, {})) is None


# ── shared: square-off time, blackout, strike limit ──────────────────────────

def test_plan_square_off_time_closes_the_position():
    plan = xp.ExitPlan(strategy="MOMENTUM_BURST", side="BUY", spot_entry=22000.0,
                       spot_stop=21950.0, exit_at="14:30")
    assert xp.tick_action(plan, 22010.0, 30.0, dtime(14, 29)) is None
    assert xp.tick_action(plan, 22010.0, 30.0, dtime(14, 30)) == xp.SESSION_EXIT


@pytest.mark.parametrize("hm,blocked", [("11:29", False), ("11:30", True), ("12:14", True),
                                        ("12:15", False)])
def test_entry_blackout_window(runtime_ctx, sqlite_db, monkeypatch, hm, blocked):
    from app.services.trading import risk_gate
    from tests.helpers import make_signal, regime_state, seed_config
    run(seed_config(sqlite_db))
    h, m = (int(x) for x in hm.split(":"))
    monkeypatch.setattr(risk_gate, "_ist_time", lambda: dtime(h, m))

    async def _go():
        async with sqlite_db() as db:
            return await risk_gate.evaluate_pre_selection(db, make_signal(), regime_state())
    d = run(_go())
    is_blackout = (not d.allowed) and "blackout" in (d.reason or "")
    assert is_blackout is blocked


@pytest.mark.parametrize("strike,kind,ok", [(22650, "CE", True), (22700, "CE", True),
                                            (22750, "CE", False), (22600, "CE", False),
                                            (22650, "PE", True), (22600, "PE", True),
                                            (22550, "PE", False), (22700, "PE", False)])
def test_only_atm_or_one_strike_otm(runtime_ctx, strike, kind, ok):
    from datetime import date, datetime, timezone
    from types import SimpleNamespace
    from app.db.models.instrument import OptionType
    from app.services.trading.option_selector import score_candidate
    inst = SimpleNamespace(strike=float(strike), option_type=OptionType(kind), name="NIFTY",
                           token="T", symbol="S", lot_size=65, expiry=date(2026, 9, 8))
    tick = {"ltp": 150.0, "best_bid": 149.8, "best_ask": 150.2, "oi": 900_000, "volume": 90_000,
            "ts": runtime_ctx.clock.now_utc().isoformat()}
    cand, fr = score_candidate(inst, 22669.0, tick, date(2026, 9, 8),
                               Direction.BUY if kind == "CE" else Direction.SELL, 50,
                               sl_pct=0.2, target_pct=0.4)
    assert (cand is not None) is ok, fr.reason
    if not ok:
        assert "strikes OTM" in fr.reason


# ── ensure_config keeps an operator's daily loss limit ───────────────────────

@pytest.mark.parametrize("loss,kept", [(5000.0, True), (21000.0, True), (0.0, False),
                                       (30000.0, False)])
def test_ensure_config_keeps_a_valid_daily_loss_limit(sqlite_db, loss, kept):
    from sqlalchemy import select
    from app.db.models.paper_trade import TradingConfig
    from app.services.trading.mode_manager import mode_manager
    from tests.helpers import seed_config
    run(seed_config(sqlite_db, capital=21000.0, max_daily_loss=loss, max_daily_loss_pct=24.0))
    cfg = run(mode_manager.ensure_config(session_factory=sqlite_db))
    assert (cfg.max_daily_loss == loss) is kept
    if not kept:
        assert cfg.max_daily_loss == 1000.0 and cfg.max_daily_loss_pct == 5.0
