"""Bug 7 (design §9.2, R8): the daily trade count is derived from persisted
positions, so a restart cannot reset it and a rejected entry consumes no slot."""
from datetime import time as dtime, timedelta

from sqlalchemy import select

from app.db.models.paper_trade import PaperPosition
from tests import portable
from tests.helpers import (
    add_closed, add_position, allow_regime, make_signal, regime_state, reset_singletons, run,
    seed_config,
)


def _freeze_gate_clock(monkeypatch, t=dtime(10, 0)):
    """The gate's entry-cutoff check reads the IST time; pin it where the
    module exists (it does not on master)."""
    try:
        from app.services.trading import risk_gate
    except ImportError:
        return
    monkeypatch.setattr(risk_gate, "_ist_time", lambda: t)


def test_portable_restart_keeps_the_trade_cap(tmp_path, monkeypatch):
    """Runs unchanged on master: 6 trades today at a cap of 6, then the
    pre-market setup a restart runs; the next pre-trade check must refuse."""
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.strategy_runner import strategy_runner

    Session = portable.make_sqlite(tmp_path, monkeypatch)
    portable.PortableRedis(monkeypatch)
    _freeze_gate_clock(monkeypatch)
    portable.seed_config(Session, daily_trades_count=6, max_trades_per_day=6)
    for _ in range(6):
        portable.add_position(Session, is_open=False, net_pnl=0.0)
    monkeypatch.setattr(strategy_runner, "_strategies", {})

    portable.run(strategy_runner._pre_market_setup())       # what a restart runs
    ok, reason = portable.run(mode_manager.pre_trade_check("NIFTY"))
    assert ok is False, "a restart reset the trade count: the 7th entry was allowed"
    assert "trade" in reason.lower()


def _gate(Session, signal=None):
    from app.services.trading import risk_gate

    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(
                db, signal or make_signal(), regime_state())
    return run(_go())


def test_derived_count_survives_a_simulated_restart(runtime_ctx, sqlite_db, monkeypatch):
    from app.services.trading import risk_state
    from app.services.trading.strategy_runner import strategy_runner

    clock = runtime_ctx.clock
    run(seed_config(sqlite_db, daily_trades_count=0))      # the display cache says 0
    now = clock.now_utc()
    for i in range(6):
        run(add_closed(sqlite_db, net=0.0, at=now - timedelta(minutes=60 - i)))
    run(add_closed(sqlite_db, net=0.0, at=now - timedelta(days=1)))       # yesterday
    run(add_closed(sqlite_db, net=0.0, at=now, run_id="RPL-other"))         # another run

    async def _count():
        async with sqlite_db() as db:
            return await risk_state.derived_trade_count(db, "PAPER", clock.trading_day())
    assert run(_count()) == 6

    # restart: every in-memory singleton cleared, then the pre-market setup
    reset_singletons()
    monkeypatch.setattr(strategy_runner, "_strategies", {})
    run(strategy_runner._pre_market_setup())
    allow_regime(monkeypatch)

    assert run(_count()) == 6
    decision = _gate(sqlite_db)
    assert decision.allowed is False
    assert decision.code.value == "DAILY_TRADE_CAP"
    assert decision.context["derived_count"] == 6


def test_pre_market_setup_neither_resets_the_count_nor_reconciles(runtime_ctx, sqlite_db,
                                                                   monkeypatch):
    """§9.2: the only reset is the trading day rolling over, and replay calls
    _pre_market_setup once per replayed day, so it must not write trading_config."""
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.strategy_runner import strategy_runner

    calls = []

    def _spy(name):
        async def _f(*a, **k):
            calls.append(name)
        return _f

    for name in ("reset_daily_counter", "reset_daily_counter_if_new_day",
                 "reconcile_used_capital"):
        monkeypatch.setattr(mode_manager, name, _spy(name))
    monkeypatch.setattr(strategy_runner, "_strategies", {})
    run(strategy_runner._pre_market_setup())
    assert calls == []


def test_rejected_entry_consumes_no_slot(runtime_ctx, sqlite_db, monkeypatch):
    """A signal refused after the gate (no option) and one refused in the entry
    section (capital race) leave the derived count where it was (R8.6)."""
    from app.services.trading import risk_gate, risk_state
    from app.services.trading.option_selector import SelectionResult, ExpiryChoice, option_selector
    from app.services.trading.paper_engine import paper_engine

    clock = runtime_ctx.clock
    run(seed_config(sqlite_db))
    for i in range(5):
        run(add_closed(sqlite_db, net=0.0, at=clock.now_utc() - timedelta(minutes=90 - i)))
    allow_regime(monkeypatch)

    async def _none(*a, **k):
        return SelectionResult(candidate=None, rejections={"TICK_UNAVAILABLE": 11},
                               evaluated=11, expiry=ExpiryChoice(None, None, None,
                                                                 "NO_SUITABLE_EXPIRY"),
                               ltp_only_fallbacks=0, score_table=[])
    monkeypatch.setattr(option_selector, "select", _none)
    assert run(paper_engine.handle_signal(make_signal())) is False

    async def _never(_db):
        raise AssertionError("an aborted entry must not reach the insert")

    async def _race():
        async with sqlite_db() as db:
            return await risk_gate.enter(
                db, run_id="PAPER", trading_day=clock.trading_day(),
                capital_used=10_000_000.0, advisory_used=0.0, insert=_never)
    assert run(_race()).code.value == "CAPITAL_RACE_LOST"

    async def _count():
        async with sqlite_db() as db:
            return (await risk_state.derived_trade_count(db, "PAPER", clock.trading_day()),
                    len((await db.execute(select(PaperPosition))).scalars().all()))
    assert run(_count()) == (5, 5)


def test_open_and_closed_positions_both_count(runtime_ctx, sqlite_db):
    from app.services.trading import risk_state

    clock = runtime_ctx.clock
    run(add_position(sqlite_db, entry_time=clock.now_utc()))                    # open
    run(add_closed(sqlite_db, net=-10.0, at=clock.now_utc()))                    # closed

    async def _count():
        async with sqlite_db() as db:
            return await risk_state.derived_trade_count(db, "PAPER", clock.trading_day())
    assert run(_count()) == 2
