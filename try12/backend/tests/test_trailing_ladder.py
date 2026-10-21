"""Bug 4 (design §8.1-8.3): the trailing ladder and partial exit as the monitor
applies them — monotonic, reachable, ordered, never advanced on a stale price.

test_rising_path_through_check_position_hits_every_tier imports only names
master also has (tests/portable.py), so it runs against master for the
fail-before: there the +25% tier sets a LOWER stop than +20% and is dropped,
and the +35% tier sits beyond the +30% target.
"""
from datetime import time as dtime, timedelta

import pytest

from app.services.trading.paper_engine import paper_engine
from tests import portable


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    return portable.make_sqlite(tmp_path, monkeypatch)


def test_rising_path_through_check_position_hits_every_tier(pdb, monkeypatch):
    redis = portable.PortableRedis(monkeypatch)
    portable.seed_config(pdb, used_capital=6500.0)
    pid = portable.add_position(pdb)                 # entry 100, SL 85, target 130, 1 lot
    stops, ltp = [85.0], 100.0
    while True:
        ltp = round(ltp + 0.5, 2)
        redis.put_tick("OPT1", ltp)
        pos = portable.get_position(pdb, pid)
        if not pos.is_open:
            break
        portable.run(paper_engine._check_position(pos, dtime(11, 0)))
        sl = portable.get_position(pdb, pid).stop_loss
        if sl != stops[-1]:
            assert sl > stops[-1]                    # never moves down
            stops.append(sl)
        assert ltp <= 131, "position never closed"
    assert stops[1:] == [101.5, 112.0, 118.0]        # BREAKEVEN_PLUS_COSTS, LOCK_40, LOCK_60
    closed = portable.get_position(pdb, pid)
    assert closed.exit_reason == "TARGET"


# ── the rest uses the branch fixtures (runtime_ctx: FrozenClock + FakeRedis) ─

def _events(Session, pid):
    from sqlalchemy import select
    from app.db.models.risk import PositionEvent

    async def _q():
        async with Session() as db:
            return (await db.execute(select(PositionEvent).where(
                PositionEvent.position_id == pid).order_by(PositionEvent.id))).scalars().all()
    return portable.run(_q())


def _tick(runtime_ctx, fake_redis, Session, pid, ltp, *, age_s=1.0, bid=0.0, ask=0.0,
          at=dtime(11, 0)):
    from tests.helpers import get_position, run
    fake_redis.put_tick("OPT1", ltp, bid=bid, ask=ask, age_s=age_s)
    pos = run(get_position(Session, pid))
    if pos.is_open:
        run(paper_engine._check_position(pos, at, dtime(15, 15)))
    return run(get_position(Session, pid))


def _two_lot(Session, **kw):
    from tests.helpers import add_position, run
    fields = dict(lots=2, quantity=130, original_quantity=130, original_lots=2,
                  trade_amount_used=13000.0, brokerage=40.0, initial_risk=15.0,
                  r_source="ENTRY")
    fields.update(kw)
    return run(add_position(Session, **fields))


def test_partial_then_lock_40_on_the_same_tick(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pid = _two_lot(sqlite_db)
    _tick(runtime_ctx, fake_redis, sqlite_db, pid, 118.0)          # BREAKEVEN_PLUS_COSTS
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 122.5)    # 0.75: partial, then LOCK_40
    seq = [(e.event_type, e.tier_label) for e in _events(sqlite_db, pid)]
    assert seq == [("TRAIL_ADVANCE", "BREAKEVEN_PLUS_COSTS"), ("PARTIAL_EXIT", None),
                   ("TRAIL_ADVANCE", "LOCK_40")]
    assert pos.stop_loss == 112.0                                  # LOCK_40, not breakeven
    assert pos.partial_closed and pos.quantity == 65 and pos.lots == 1
    assert pos.original_quantity == 130 and pos.original_lots == 2
    assert pos.initial_stop_loss == 85.0 and pos.initial_risk == 15.0 and pos.target == 130.0
    assert pos.trade_amount_used == pytest.approx(6500.0)          # half released, once
    _tick(runtime_ctx, fake_redis, sqlite_db, pid, 123.0)          # 0.75 again: no 2nd partial
    assert [e.event_type for e in _events(sqlite_db, pid)].count("PARTIAL_EXIT") == 1


def test_partial_then_gap_to_breakeven_is_net_positive(runtime_ctx, sqlite_db, fake_redis):
    """R5.9: the banked half pays for the remainder's fall through the stop."""
    from tests.helpers import run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pid = _two_lot(sqlite_db)
    _tick(runtime_ctx, fake_redis, sqlite_db, pid, 116.0)
    _tick(runtime_ctx, fake_redis, sqlite_db, pid, 122.5)
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 100.0)    # gaps through 112 to entry
    assert not pos.is_open and pos.exit_reason == "SL"
    assert pos.exit_fill_source == "LTP_GAP" and pos.actual_exit == 100.0
    assert pos.net_pnl > 0
    assert pos.gross_pnl - pos.brokerage - pos.statutory_charges == pytest.approx(pos.net_pnl,
                                                                                 abs=0.01)


def test_single_lot_skips_the_partial_once(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import add_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, initial_risk=15.0, r_source="ENTRY",
                           original_quantity=65, original_lots=1))
    for px in (122.5, 123.0, 123.5):
        pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, px)
    kinds = [e.event_type for e in _events(sqlite_db, pid)]
    assert kinds.count("PARTIAL_SKIPPED_SINGLE_LOT") == 1
    assert pos.partial_skipped is True and pos.partial_closed is False
    assert pos.quantity == 65 and pos.stop_loss == 112.0


def test_stale_tick_defers_the_stop_and_the_target(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import add_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, initial_risk=15.0, r_source="ENTRY",
                           original_quantity=65, original_lots=1))
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 123.0, age_s=120)   # trail level
    assert pos.stop_loss == 85.0                                   # not advanced
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 131.0, age_s=120)   # above target
    assert pos.is_open                                             # no TARGET on a stale price
    defers = [e for e in _events(sqlite_db, pid) if e.event_type == "STALE_DEFER"]
    assert len(defers) == 1                                        # at most once per minute
    runtime_ctx.clock.advance(61)
    _tick(runtime_ctx, fake_redis, sqlite_db, pid, 131.0, age_s=120)
    assert [e.event_type for e in _events(sqlite_db, pid)].count("STALE_DEFER") == 2
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 131.0, age_s=1)     # fresh again
    assert not pos.is_open and pos.exit_reason == "TARGET"


def test_timeout_uses_max_hold_minutes(runtime_ctx, sqlite_db, fake_redis):
    from dataclasses import replace

    from app.core.trading_params import TradingParams
    from app.runtime.context import using
    from app.runtime.live import StaticParams
    from tests.helpers import add_position, run, seed_config

    run(seed_config(sqlite_db, used_capital=6500.0))
    entry_time = runtime_ctx.clock.now_utc()
    pid = run(add_position(sqlite_db, strategy_name="ORB_VWAP", entry_time=entry_time,
                           initial_risk=15.0, r_source="ENTRY", original_quantity=65))
    p = replace(TradingParams(), max_hold_minutes={"DEFAULT": 10})
    with using(replace(runtime_ctx, params=StaticParams(p))):
        runtime_ctx.clock.advance(minutes=10)
        assert _tick(runtime_ctx, fake_redis, sqlite_db, pid, 101.0).is_open   # minute 10
        runtime_ctx.clock.advance(minutes=1)
        pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 101.0)            # minute 11
    assert not pos.is_open and pos.exit_reason == "TIMEOUT"
    assert pos.hold_minutes == pytest.approx(11.0)


def test_missing_target_writes_data_gap_and_leaves_the_stop(runtime_ctx, sqlite_db, fake_redis):
    from sqlalchemy import select

    from app.db.models.risk import NoTradeDecision
    from tests.helpers import add_position, run, seed_config

    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, target=None, initial_risk=15.0, r_source="ENTRY"))
    pos = _tick(runtime_ctx, fake_redis, sqlite_db, pid, 120.0)
    assert pos.is_open and pos.stop_loss == 85.0

    async def _gaps():
        async with sqlite_db() as db:
            return (await db.execute(select(NoTradeDecision).where(
                NoTradeDecision.code == "DATA_GAP"))).scalars().all()
    gaps = run(_gaps())
    assert len(gaps) == 1 and gaps[0].position_id == pid


def test_handle_signal_writes_entry_risk_once(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    from app.services.trading import paper_engine as pe_mod
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.option_selector import option_selector
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.runtime.context import using
    from tests.helpers import allow_regime, fake_candidate, get_position, make_signal, \
        pin_ist_time, run, seed_config, stub_select, with_cfg

    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 30))
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))
    allow_regime(monkeypatch)

    # ₹30,000 capital × 10% = ₹3,000 budget
    with using(with_cfg(runtime_ctx, capital=30_000.0)):
        assert run(paper_engine.handle_signal(make_signal())) is True
    opened = fake_redis.events("POSITION_OPENED")
    assert len(opened) == 1
    pos = run(get_position(sqlite_db, opened[0]["pos_id"]))
    assert pos.initial_stop_loss == pos.stop_loss
    assert pos.initial_stop_loss == pytest.approx(40.08 * 0.85, abs=0.1)
    assert pos.initial_risk == pytest.approx(round(pos.actual_entry - pos.initial_stop_loss, 2))
    assert pos.r_source == "ENTRY"
    assert (pos.original_quantity, pos.original_lots) == (pos.quantity, pos.lots)
    assert pos.entry_fill_source == "LTP_FALLBACK"      # no quote on the fake candidate


def test_check_position_trails_with_new_ladder(sqlite_db, fake_redis):
    from tests.helpers import add_position, get_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))       # entry 100, SL 85, target 130
    fake_redis.put_tick("OPT1", 123.0)
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    pos = run(get_position(sqlite_db, pid))
    assert pos.is_open
    assert pos.stop_loss == 112.0 and pos.trailing_sl == 112.0
    assert pos.initial_stop_loss == 85.0     # never overwritten


def test_default_target_fraction_is_gone():
    from app.services.trading import paper_engine as pe_mod
    for name in ("DEFAULT_TARGET_FRACTION", "TRAIL_TIERS", "_trailing_stop"):
        assert not hasattr(pe_mod, name), name
