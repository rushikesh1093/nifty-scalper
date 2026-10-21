"""A trend-invalidation exit closes only on a fresh quote. Without one it is
recorded as pending (with the observed price, its age and the tracked last-known
price) and executed by the position monitor on the first fresh tick; the
stop-loss and EOD rules keep protecting the position meanwhile. The entry price
is never used as a market quote (manual close included)."""
from datetime import time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.risk import PositionEvent
from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_runner import strategy_runner
from app.strategies.trend_engine import TrendFollowingEngine
from tests.helpers import add_position, get_position, run, seed_config

SQ = dtime(15, 15)


def _open(runtime_ctx, sqlite_db, **kw):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, strategy_name="TREND_ENGINE",
                           entry_time=runtime_ctx.clock.now_utc(), **kw))
    return pid, run(get_position(sqlite_db, pid))


def _events(sqlite_db, pid, kind="INVALIDATION_PENDING"):
    async def _q():
        async with sqlite_db() as db:
            return (await db.execute(select(PositionEvent).where(
                PositionEvent.position_id == pid, PositionEvent.event_type == kind))).scalars().all()
    return run(_q())


def test_fresh_quote_closes_at_the_quote(runtime_ctx, sqlite_db, fake_redis):
    pid, pos = _open(runtime_ctx, sqlite_db)
    fake_redis.put_tick("OPT1", 96.0, bid=95.9, ask=96.1, age_s=2)
    assert run(paper_engine.exit_on_invalidation(pos, "close below VWAP")) == "CLOSED"
    closed = run(get_position(sqlite_db, pid))
    assert not closed.is_open and closed.exit_reason == "INVALIDATED"
    assert closed.actual_exit < 96.0 and closed.actual_exit != closed.entry_price


def test_no_price_is_pending_never_the_entry_price(runtime_ctx, sqlite_db, fake_redis):
    pid, pos = _open(runtime_ctx, sqlite_db)
    fake_redis.drop_price("OPT1")
    paper_engine._last_known[pid] = (97.5, runtime_ctx.clock.now_utc() - timedelta(seconds=90))
    assert run(paper_engine.exit_on_invalidation(pos, "close below VWAP")) == "PENDING"
    assert run(get_position(sqlite_db, pid)).is_open
    (ev,) = _events(sqlite_db, pid)
    assert ev.detail["reason"] == "close below VWAP"
    assert ev.detail.get("observed_ltp") is None and ev.detail.get("tick_age_s") is None
    assert ev.detail["last_known"]["price"] == 97.5
    assert ev.detail["last_known"]["age_s"] == pytest.approx(90.0)


def test_stale_quote_is_pending_then_the_monitor_exits_on_a_fresh_tick(runtime_ctx, sqlite_db,
                                                                       fake_redis):
    pid, pos = _open(runtime_ctx, sqlite_db)
    fake_redis.put_tick("OPT1", 95.0, age_s=300)            # 5 minutes old
    assert run(paper_engine.exit_on_invalidation(pos, "trend broke")) == "PENDING"
    (ev,) = _events(sqlite_db, pid)
    assert ev.detail["tick_age_s"] == pytest.approx(300.0) and ev.price == 95.0

    # still stale: the monitor does not act on it (no target/invalidation on old quotes)
    run(paper_engine._check_position(run(get_position(sqlite_db, pid)), dtime(11, 0), SQ))
    assert run(get_position(sqlite_db, pid)).is_open

    fake_redis.put_tick("OPT1", 94.0, bid=93.9, ask=94.1, age_s=1)
    run(paper_engine._check_position(run(get_position(sqlite_db, pid)), dtime(11, 1), SQ))
    closed = run(get_position(sqlite_db, pid))
    assert not closed.is_open and closed.exit_reason == "INVALIDATED"
    assert pid not in paper_engine._pending_invalidation


def test_stop_loss_still_protects_a_pending_position(runtime_ctx, sqlite_db, fake_redis):
    pid, pos = _open(runtime_ctx, sqlite_db)
    fake_redis.put_tick("OPT1", 90.0, age_s=300)
    run(paper_engine.exit_on_invalidation(pos, "trend broke"))
    fake_redis.put_tick("OPT1", 80.0, age_s=120)            # stale, below the 85 stop
    run(paper_engine._check_position(run(get_position(sqlite_db, pid)), dtime(11, 5), SQ))
    closed = run(get_position(sqlite_db, pid))
    assert not closed.is_open and closed.exit_reason == "SL_STALE"


def test_pending_event_is_rate_limited(runtime_ctx, sqlite_db, fake_redis):
    pid, pos = _open(runtime_ctx, sqlite_db)
    fake_redis.drop_price("OPT1")
    run(paper_engine.exit_on_invalidation(pos, "a"))
    run(paper_engine.exit_on_invalidation(pos, "a"))
    assert len(_events(sqlite_db, pid)) == 1
    runtime_ctx.clock.advance(61)
    run(paper_engine.exit_on_invalidation(pos, "a"))
    assert len(_events(sqlite_db, pid)) == 2


def test_runner_invalidation_never_closes_at_entry(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    pid, _ = _open(runtime_ctx, sqlite_db)
    fake_redis.drop_price("OPT1")
    engine = TrendFollowingEngine("NIFTY")
    monkeypatch.setattr(engine, "invalidation", lambda opt, candles: "close below VWAP")
    monkeypatch.setitem(strategy_runner._strategies, "NIFTY", [engine])
    run(strategy_runner._exit_invalidated_engine_positions("NIFTY", []))
    assert run(get_position(sqlite_db, pid)).is_open
    assert pid in paper_engine._pending_invalidation


# ── manual close ─────────────────────────────────────────────────────────────

def test_manual_close_without_any_price_refuses(runtime_ctx, sqlite_db, fake_redis):
    pid, _ = _open(runtime_ctx, sqlite_db)
    fake_redis.drop_price("OPT1")
    assert run(paper_engine.manual_close(pid)) == "no_price"
    assert run(get_position(sqlite_db, pid)).is_open


def test_manual_close_on_stale_price_is_tagged(runtime_ctx, sqlite_db, fake_redis):
    pid, _ = _open(runtime_ctx, sqlite_db)
    fake_redis.put_tick("OPT1", 101.0, age_s=600)
    assert run(paper_engine.manual_close(pid)) == "closed"
    assert run(get_position(sqlite_db, pid)).exit_reason == "MANUAL_STALE"


def test_manual_close_on_fresh_price(runtime_ctx, sqlite_db, fake_redis):
    pid, _ = _open(runtime_ctx, sqlite_db)
    fake_redis.put_tick("OPT1", 101.0, bid=100.9, ask=101.1, age_s=1)
    assert run(paper_engine.manual_close(pid)) == "closed"
    assert run(get_position(sqlite_db, pid)).exit_reason == "MANUAL"


def test_manual_close_unknown_position(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db))
    assert run(paper_engine.manual_close(999)) == "not_found"
