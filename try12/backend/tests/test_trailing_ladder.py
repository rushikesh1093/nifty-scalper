"""Fix 4 (B3/B4): monotonic, reachable trailing ladder + initial_stop_loss for R:R."""
from datetime import time as dtime

import pytest

from app.db.models.paper_trade import PaperPosition, TradingMode
from app.services.trading import paper_engine as pe_mod
from app.services.trading.mode_manager import mode_manager
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import _trailing_stop, paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests.helpers import (
    add_position, fake_candidate, get_position, make_signal, run, seed_config,
)


def _sweep(entry, target, stop):
    sl, history, hit = stop, [], set()
    ltp = entry
    while ltp <= (target or entry * 1.3) + 1e-9:
        new = _trailing_stop(entry, target, ltp, sl)
        if new is not None:
            assert new > sl
            sl = new
            hit.add(new)
        assert sl < ltp or ltp == entry   # SL always below the price that set it
        history.append((round(ltp, 2), sl))
        ltp = round(ltp + 0.5, 2)
    return history, hit


def test_ladder_monotonic_and_reachable():
    history, hit = _sweep(100.0, 130.0, 85.0)
    sls = [s for _, s in history]
    assert sls == sorted(sls)                       # never moves down
    assert hit == {100.0, 112.0, 118.0}             # all 3 tiers hit before +30% target
    at = dict(history)
    assert at[125.0] >= at[120.0]                   # old ladder had 25% tier < 20% tier
    assert at[114.5] == 85.0 and at[115.0] == 100.0  # breakeven at 50% progress
    assert at[122.5] == 112.0 and at[127.0] == 118.0


def test_ladder_never_lowers_sl():
    assert _trailing_stop(100.0, 130.0, 116.0, 105.0) is None


def test_default_r_when_target_missing():
    # R = 30% of entry → breakeven tier at +15%
    assert _trailing_stop(100.0, None, 115.0, 85.0) == 100.0
    assert _trailing_stop(100.0, None, 114.0, 85.0) is None


def test_daily_pnl_uses_initial_stop_loss(sqlite_db, monkeypatch):
    calls = []

    async def _spy(strategy_name, won, achieved_rr):
        calls.append(achieved_rr)

    monkeypatch.setattr(mode_manager, "update_strategy_perf", _spy)
    pos = PaperPosition(
        strategy_name="VWAP_PULLBACK", token="X", symbol="X", underlying="NIFTY",
        lots=1, lot_size=65, quantity=65, entry_price=100.0, actual_entry=100.0,
        stop_loss=112.0, initial_stop_loss=85.0, actual_exit=120.0,
        gross_pnl=1300.0, brokerage=40.0, mode=TradingMode.PAPER,
    )
    run(paper_engine._update_daily_pnl(pos, 1260.0))
    assert calls == [round(20 / 15, 3)]


def test_handle_signal_sets_initial_stop_loss(sqlite_db, fake_redis, monkeypatch):
    run(seed_config(sqlite_db))
    monkeypatch.setattr(pe_mod, "_ist_time", lambda: dtime(10, 30))

    async def _select(**kwargs):
        return fake_candidate(ltp=40.0, lot_size=65)

    async def _none(u):
        return None

    async def _kelly(name, cfg):
        return 3000.0

    monkeypatch.setattr(option_selector, "select", _select)
    monkeypatch.setattr(strategy_orchestrator, "get_current_regime", _none)
    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", lambda u: 1.0)
    monkeypatch.setattr(mode_manager, "kelly_trade_amount", _kelly)

    assert run(paper_engine.handle_signal(make_signal())) is True
    opened = fake_redis.events("POSITION_OPENED")
    assert len(opened) == 1
    pos = run(get_position(sqlite_db, opened[0]["pos_id"]))
    assert pos.initial_stop_loss == pos.stop_loss
    assert pos.initial_stop_loss == pytest.approx(40.08 * 0.85, abs=0.1)


def test_check_position_trails_with_new_ladder(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))       # entry 100, SL 85, target 130
    fake_redis.put_tick("OPT1", 123.0)
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    pos = run(get_position(sqlite_db, pid))
    assert pos.is_open
    assert pos.stop_loss == 112.0 and pos.trailing_sl == 112.0
    assert pos.initial_stop_loss == 85.0     # never overwritten
