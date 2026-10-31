"""
Audit D6 (23 Oct): at most max_open_per_direction (1) OPEN positions in one
direction on an underlying. Two strategies agreeing on a move are one bet;
2 x the per-trade risk on one move exceeded the daily loss limit.
"""
from datetime import time as dtime

import pytest

from app.core.trading_params import TradingParams
from app.services.trading import risk_gate
from app.services.trading.no_trade import ReasonCode
from app.strategies.base import Direction
from tests.helpers import (
    add_position, allow_regime, make_signal, pin_ist_time, regime_state, run, seed_config,
)


pytestmark = pytest.mark.usefixtures("no_evidence_gates")   # tests other gates with SUPERTREND_EMA


@pytest.fixture
def day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    return runtime_ctx


def _pre(Session, strategy="SUPERTREND_EMA", direction=Direction.BUY, underlying="NIFTY"):
    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(
                db, make_signal(strategy=strategy, direction=direction, underlying=underlying),
                regime_state(underlying=underlying))
    return run(_go())


def test_a_second_position_in_the_same_direction_is_refused(day, sqlite_db):
    run(add_position(sqlite_db, strategy_name="FIRST_CANDLE_BREAKOUT", option_type="CE"))
    d = _pre(sqlite_db, "SUPERTREND_EMA", Direction.BUY)
    assert not d.allowed and d.code == ReasonCode.MAX_OPEN_POSITIONS
    assert d.context["direction"] == "CE" and d.context["open_same_direction"] == 1


def test_the_other_direction_is_not_blocked_by_the_direction_rule(day, sqlite_db):
    run(add_position(sqlite_db, strategy_name="FIRST_CANDLE_BREAKOUT", option_type="CE"))
    d = _pre(sqlite_db, "SUPERTREND_EMA", Direction.SELL)
    # (the pinned test regime may refuse a SELL for its own reason; not this one)
    assert d.code != ReasonCode.MAX_OPEN_POSITIONS


def test_closed_positions_do_not_count(day, sqlite_db):
    run(add_position(sqlite_db, strategy_name="FIRST_CANDLE_BREAKOUT", option_type="CE",
                     is_open=False, exit_reason="TARGET"))
    assert _pre(sqlite_db, "SUPERTREND_EMA", Direction.BUY).allowed


def test_the_limit_is_configurable_and_validated():
    assert TradingParams().max_open_per_direction == 1
    assert any("max_open_per_direction" in v for v in TradingParams(max_open_per_direction=0).validate())


def test_the_locked_entry_rechecks_the_direction(day, sqlite_db):
    """Two signals can pass pre-selection together; the locked entry step refuses
    the second one and inserts nothing."""
    run(add_position(sqlite_db, strategy_name="FIRST_CANDLE_BREAKOUT", option_type="CE"))
    inserted = []

    async def _insert(db):
        inserted.append(1)

    async def _go():
        async with sqlite_db() as db:
            return await risk_gate.enter(
                db, run_id=day.run_id, trading_day=day.clock.trading_day(), capital_used=5000.0,
                advisory_used=0.0, insert=_insert, duplicate_token="OPT_OTHER",
                direction_key=("NIFTY", "CE"))
    d = run(_go())
    assert not d.allowed and d.code == ReasonCode.MAX_OPEN_POSITIONS and not inserted
