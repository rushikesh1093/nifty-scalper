"""
Review fixes 2-4 (23 Oct):
  2. the 09:15 breakout's spot stop is at most 50 pts from entry;
  3. an exit-plan position's premium stop — and its sizing — follow the plan's
     SPOT stop (buffer x |delta| x distance), bounded by sl_pct and a 5% floor;
  4. one position a day across ORB_BREAKOUT / FIRST_CANDLE_BREAKOUT.
"""
from datetime import time as dtime, timedelta

import pytest

from app.core.trading_params import TradingParams
from app.services.trading import fills, risk_gate
from app.strategies.base import Direction
from app.strategies.strategies import FirstCandleBreakoutStrategy
from tests.helpers import (
    add_position, allow_regime, fake_candidate, make_signal, pin_ist_time, regime_state, run,
    seed_config,
)
from tests.test_three_strategies import OPEN, at, evaluate, expand

P = TradingParams()


# ── 3. premium stop from the spot stop ───────────────────────────────────────

def _plan(dist, side="BUY"):
    entry = 22000.0
    return {"side": side, "spot_entry": entry, "spot_stop": entry - dist if side == "BUY" else entry + dist}


def test_no_plan_keeps_the_strategy_sl_pct():
    assert fills.plan_sl_pct(114.0, 0.15, None, 0.48, P) == 0.15


def test_a_near_spot_stop_gives_a_near_premium_stop():
    # Supertrend line 15 pts away, delta 0.48, Rs114 premium: 1.3 x 0.48 x 15 = 9.36 pts
    frac = fills.plan_sl_pct(114.0, 0.15, _plan(15), 0.48, P)
    assert frac == pytest.approx(9.36 / 114.0, abs=1e-6)
    sl, _, _, _ = fills.entry_levels(114.0, frac, 0.30, 2.0)
    assert 114.0 - sl == pytest.approx(9.4, abs=0.06)              # was 17.1 at sl_pct 0.15


def test_the_plan_stop_is_bounded_by_sl_pct_and_the_floor():
    assert fills.plan_sl_pct(114.0, 0.15, _plan(200), 0.5, P) == 0.15      # never looser
    assert fills.plan_sl_pct(114.0, 0.15, _plan(2), 0.5, P) == 0.05        # never inside noise
    assert fills.plan_sl_pct(114.0, 0.15, _plan(15, "SELL"), -0.5, P) == \
        pytest.approx(1.3 * 0.5 * 15 / 114.0, abs=1e-6)                   # put delta, short side


def test_unknown_delta_uses_half_and_extremes_are_clamped():
    a = fills.plan_sl_pct(150.0, 0.25, _plan(20), None, P)
    b = fills.plan_sl_pct(150.0, 0.25, _plan(20), 0.95, P)
    assert a == pytest.approx(1.3 * 0.5 * 20 / 150.0, abs=1e-6)
    assert b == pytest.approx(1.3 * 0.7 * 20 / 150.0, abs=1e-6)


def test_sizing_uses_the_plan_risk(runtime_ctx, sqlite_db, monkeypatch):
    """The gate sizes from the plan stop: a closer spot stop means a smaller
    initial risk per lot, so the max-loss-per-trade budget allows more lots."""
    run(seed_config(sqlite_db, capital=200_000.0))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    cand = fake_candidate(ltp=114.0, bid=113.9, ask=114.0, tick_age_s=1.0)
    frac = risk_gate.FractionDecision("FIXED_FRACTION", 0.1, 20_000.0)

    def _size(plan):
        sig = make_signal(sl_pct=0.15, target_pct=0.30)
        sig.exit_plan = plan

        async def _go():
            async with sqlite_db() as db:
                return await risk_gate.evaluate_sizing(db, sig, cand, fraction=frac, regime_multiplier=1.0)
        return run(_go())
    far, near = _size(None), _size(_plan(15))
    assert far.allowed and near.allowed
    assert near.context["initial_risk_per_lot"] < far.context["initial_risk_per_lot"]


# ── 2. 09:15 breakout stop cap ───────────────────────────────────────────────

def test_a_wide_first_candle_caps_the_stop_at_50_points():
    wide = (22000.0, 22070.0, 21950.0, 22060.0)                       # 120-pt candle
    c = expand([wide, (22060.0, 22090.0, 22055.0, 22085.0)], 5)        # close > 22070 + 10
    with at(10):
        sig = evaluate(FirstCandleBreakoutStrategy(), c)
    assert sig.direction == Direction.BUY
    assert sig.exit_plan["spot_stop"] == 22085.0 - 50.0               # not the 21950 low
    assert sig.exit_plan["spot_target"] == 22085.0 + 1.5 * 120


def test_a_close_first_candle_stop_is_kept():
    small = (22000.0, 22030.0, 21995.0, 22020.0)                      # 35-pt candle
    c = expand([small, (22020.0, 22045.0, 22018.0, 22041.0)], 5)       # close > 22030 + 10
    with at(10):
        sig = evaluate(FirstCandleBreakoutStrategy(), c)
    assert sig.exit_plan["spot_stop"] == 21995.0                      # 46 pts away: under the cap


# ── 4. one opening breakout a day ────────────────────────────────────────────

def _pre(Session, strategy):
    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(db, make_signal(strategy=strategy),
                                                          regime_state())
    return run(_go())


def test_one_opening_breakout_a_day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    now = runtime_ctx.clock.now_utc()
    run(add_position(sqlite_db, strategy_name="ORB_BREAKOUT", is_open=False, exit_reason="TARGET",
                     entry_time=now - timedelta(minutes=30), exit_time=now - timedelta(minutes=10)))
    blocked = _pre(sqlite_db, "FIRST_CANDLE_BREAKOUT")
    assert not blocked.allowed and "one trade a day" in blocked.reason
    assert _pre(sqlite_db, "SUPERTREND_EMA").allowed                  # other strategies unaffected


def test_yesterdays_breakout_does_not_block_today(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    now = runtime_ctx.clock.now_utc()
    run(add_position(sqlite_db, strategy_name="ORB_BREAKOUT", is_open=False, exit_reason="TARGET",
                     entry_time=now - timedelta(days=1), exit_time=now - timedelta(days=1)))
    assert _pre(sqlite_db, "FIRST_CANDLE_BREAKOUT").allowed
