"""Fix 7: configured square-off time used everywhere; no entries in the last 10 min."""
from datetime import time as dtime

import pytest

from app.db.models.paper_trade import SignalStatus
from app.services.trading import paper_engine as pe_mod
from app.services.trading.mode_manager import mode_manager
from app.services.trading.paper_engine import paper_engine
from app.services.trading.session_times import (
    DEFAULT_SQUARE_OFF, entry_cutoff, is_after_entry_cutoff, parse_square_off,
)
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests.helpers import add_position, make_cfg, make_signal, run, seed_config


@pytest.mark.parametrize("value, sq, cutoff", [
    ("15:15", dtime(15, 15), dtime(15, 5)),
    ("14:30", dtime(14, 30), dtime(14, 20)),
    ("bad", DEFAULT_SQUARE_OFF, dtime(15, 5)),
    (None, DEFAULT_SQUARE_OFF, dtime(15, 5)),
])
def test_parse_and_cutoff(value, sq, cutoff):
    assert parse_square_off(value) == sq
    assert entry_cutoff(value) == cutoff


def test_is_after_cutoff():
    assert not is_after_entry_cutoff(dtime(15, 4), "15:15")
    assert is_after_entry_cutoff(dtime(15, 5), "15:15")
    assert is_after_entry_cutoff(dtime(14, 25), "14:30")


@pytest.fixture
def gate(monkeypatch, fake_redis):
    saved, checks = [], []

    async def _cfg():
        return make_cfg()

    async def _save(signal, status, reason="", **kw):
        saved.append((status, reason))
        return 1

    async def _pre(underlying):
        checks.append(underlying)
        return False, "stop here"

    async def _none(u):
        return None

    monkeypatch.setattr(mode_manager, "get_config", _cfg)
    monkeypatch.setattr(mode_manager, "pre_trade_check", _pre)
    monkeypatch.setattr(paper_engine, "_save_signal", _save)
    monkeypatch.setattr(strategy_orchestrator, "get_current_regime", _none)
    return saved, checks


def test_handle_signal_blocked_after_cutoff(gate, monkeypatch):
    saved, checks = gate
    monkeypatch.setattr(pe_mod, "_ist_time", lambda: dtime(15, 6))
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert saved[-1][0] == SignalStatus.SKIPPED
    assert "Entry cutoff 15:05" in saved[-1][1]
    assert checks == []


def test_handle_signal_allowed_before_cutoff(gate, monkeypatch):
    saved, checks = gate
    monkeypatch.setattr(pe_mod, "_ist_time", lambda: dtime(15, 4))
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert checks == ["NIFTY"]
    assert saved[-1][1] == "stop here"


def test_check_position_uses_configured_square_off(sqlite_db, fake_redis, monkeypatch):
    from tests.helpers import get_position

    run(seed_config(sqlite_db, used_capital=6500.0, square_off_time="14:30"))
    pid = run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 101.0)
    closed = []

    async def _spy(pos_id, price, reason):
        closed.append((pos_id, price, reason))

    monkeypatch.setattr(paper_engine, "_close_position", _spy)
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(14, 29), parse_square_off("14:30")))
    assert closed == []
    run(paper_engine._check_position(pos, dtime(14, 31), parse_square_off("14:30")))
    assert closed == [(pid, 101.0, "EOD")]


def test_monitor_square_off_read_from_config(sqlite_db):
    run(seed_config(sqlite_db, square_off_time="14:30"))
    assert run(paper_engine._get_square_off()) == dtime(14, 30)
