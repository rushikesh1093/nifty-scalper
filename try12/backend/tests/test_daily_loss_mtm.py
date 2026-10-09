"""Bug 8 (design §9.3, R9): the daily loss cap is realised P&L (derived from closed
positions) plus staleness-aware MTM of open positions net of exit charges, ratcheted
for the day in risk_halts, so an unrealised loss alone stops new entries, a recovery
does not lift the halt, and a restart cannot clear it."""
from datetime import datetime, time as dtime, timedelta, timezone

import pytest
from sqlalchemy import select

from app.db.models.risk import NoTradeDecision, RiskHalt
from app.runtime.context import using
from app.services.trading import risk_gate, risk_state
from app.services.trading.paper_engine import paper_engine
from tests.helpers import (
    add_closed, add_position, allow_regime, get_position, make_signal, pin_ist_time,
    regime_state, reset_singletons, run, seed_config, with_cfg,
)


def _figure(Session):
    async def _go():
        async with Session() as db:
            return await risk_state.daily_loss_figure(db)
    return run(_go())


def _halts(Session):
    async def _go():
        async with Session() as db:
            return (await db.execute(select(RiskHalt))).scalars().all()
    return run(_go())


def _gate(Session):
    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(db, make_signal(), regime_state())
    return run(_go())


@pytest.fixture
def day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    return runtime_ctx


def test_unrealised_loss_alone_breaches_the_cap(day, sqlite_db, fake_redis):
    """R9.8: zero realised P&L, one open position marked 40% below entry."""
    pid = run(add_position(sqlite_db))                 # entry 100 × 65
    fake_redis.put_tick("OPT1", 60.0, age_s=1)         # −40%: −2,600 before exit charges
    with using(with_cfg(day, max_daily_loss=2000.0)):
        loss = _figure(sqlite_db)
        assert loss.realised == 0.0
        assert loss.unrealised < -2600.0               # net of the charges an exit would incur
        assert loss.breached and loss.breach_source == "ABS"
        gate = _gate(sqlite_db)
    assert gate.allowed is False and gate.code.value == "DAILY_LOSS_CAP"
    assert gate.context["breach_source"] == "RATCHET"   # the figure above already engaged the halt
    # open positions keep their own exits: the stop still closes the position
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(10, 0), dtime(15, 15)))
    assert not run(get_position(sqlite_db, pid)).is_open


def test_percentage_threshold_breaches_too(day, sqlite_db, fake_redis):
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 60.0, age_s=1)
    with using(with_cfg(day, max_daily_loss=100_000.0, max_daily_loss_pct=1.0)):
        loss = _figure(sqlite_db)                      # ≈ −2.7k of ₹200k = 1.35%
    assert loss.breached and loss.breach_source == "PCT"


def test_ratchet_holds_after_the_mtm_recovers(day, sqlite_db, fake_redis):
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 60.0, age_s=1)
    with using(with_cfg(day, max_daily_loss=2000.0)):
        assert _figure(sqlite_db).breach_source == "ABS"
        fake_redis.put_tick("OPT1", 105.0, age_s=1)    # fully recovered
        again = _figure(sqlite_db)
    assert again.combined > -2000.0
    assert again.breached and again.breach_source == "RATCHET"
    (halt,) = _halts(sqlite_db)
    assert (halt.halt_type, halt.run_id, halt.scope) == ("DAILY_LOSS", "PAPER", None)


def test_halt_survives_a_restart_and_clears_the_next_day(day, sqlite_db, fake_redis):
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 60.0, age_s=1)
    with using(with_cfg(day, max_daily_loss=2000.0)):
        assert _figure(sqlite_db).breached
        fake_redis.put_tick("OPT1", 105.0, age_s=1)

        reset_singletons()                             # a restart: every in-memory object gone
        assert _figure(sqlite_db).breach_source == "RATCHET"

        day.clock.advance(days=1)                      # the trading day rolls over
        fake_redis.put_tick("OPT1", 105.0, age_s=1)
        tomorrow = _figure(sqlite_db)
    assert tomorrow.breached is False and tomorrow.breach_source is None
    assert len(_halts(sqlite_db)) == 1                 # yesterday's row stays as the audit trail


def test_a_positive_figure_never_triggers(day, sqlite_db, fake_redis):
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 140.0, age_s=1)        # +2,600
    run(add_closed(sqlite_db, net=500.0, at=day.clock.now_utc() - timedelta(minutes=30)))
    with using(with_cfg(day, max_daily_loss=1.0, max_daily_loss_pct=0.0001)):
        loss = _figure(sqlite_db)
    assert loss.combined > 0 and loss.breached is False
    assert _halts(sqlite_db) == []


def test_realised_is_derived_from_positions_not_the_summary(day, sqlite_db):
    """daily_pnl_summary can miss a close (PERF_WRITE_FAILED); the cap must not."""
    run(add_closed(sqlite_db, net=-3000.0, at=day.clock.now_utc() - timedelta(minutes=30)))
    with using(with_cfg(day, max_daily_loss=2500.0)):
        loss = _figure(sqlite_db)
    assert loss.realised == -3000.0 and loss.realised_cached == 0.0
    assert loss.realised_mismatch is True
    assert loss.breached and loss.breach_source == "ABS"


def test_other_runs_and_other_days_are_not_counted(day, sqlite_db):
    now = day.clock.now_utc()
    run(add_closed(sqlite_db, net=-9000.0, at=now - timedelta(days=1)))
    run(add_closed(sqlite_db, net=-9000.0, at=now - timedelta(minutes=5), run_id="RPL-x"))
    assert _figure(sqlite_db).realised == 0.0


def test_stale_price_marks_at_the_last_known_price(day, sqlite_db, fake_redis):
    pid = run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 50.0, age_s=300)       # stale: must not be used as the mark
    paper_engine._last_known[pid] = (95.0, day.clock.now_utc())
    loss = _figure(sqlite_db)
    assert loss.mtm_degraded and loss.degraded_positions == [pid]
    assert -5 * 65 - 60 < loss.unrealised < -5 * 65      # marked at 95, not at the stale 50


def test_degraded_marks_write_one_row_per_position_per_day(day, sqlite_db, fake_redis):
    """100 degraded passes → exactly one MTM_DEGRADED row (it runs every second)."""
    pid = run(add_position(sqlite_db))                  # no tick and no last-known price

    async def _many():
        async with sqlite_db() as db:
            for _ in range(100):
                loss = await risk_state.daily_loss_figure(db)
            rows = (await db.execute(
                select(NoTradeDecision).where(NoTradeDecision.code == "MTM_DEGRADED")
            )).scalars().all()
            return loss, rows
    loss, rows = run(_many())
    assert loss.mtm_degraded
    assert len(rows) == 1 and rows[0].position_id == pid and rows[0].stage == "DEGRADATION"
    assert rows[0].context["mark_source"] == "ENTRY"

    day.clock.advance(days=1)                           # the next trading day gets a fresh row
    run(_many())

    async def _count():
        async with sqlite_db() as db:
            return len((await db.execute(select(NoTradeDecision).where(
                NoTradeDecision.code == "MTM_DEGRADED"))).scalars().all())
    assert run(_count()) == 2
