"""The single pre-trade gate (design §9.1): check order, every code reachable, sizing
only after selection and never on a halted day, the multiplier applied once, and
the (run_id, trading_day) lock."""
import asyncio
from datetime import time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import PaperPosition, StrategyPerf
from app.db.models.risk import NoTradeDecision
from app.runtime.context import using
from app.services.trading import risk_gate
from app.services.trading.mode_manager import mode_manager
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests.helpers import (
    add_closed, add_position, allow_regime, fake_candidate, make_signal, pin_ist_time,
    position_fields, regime_state, run, seed_config, selection_result, stub_select, with_cfg,
    with_params,
)


@pytest.fixture
def day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    return runtime_ctx


def _phase1(Session, signal=None):
    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(
                db, signal or make_signal(), regime_state())
    return run(_go())


def _phase2(Session, candidate, *, base=20_000.0, mult=1.0, signal=None):
    fraction = risk_gate.FractionDecision(method="FIXED_FRACTION", applied_fraction=0.1,
                                          base_amount=base)

    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_sizing(
                db, signal or make_signal(), candidate, fraction=fraction,
                regime_multiplier=mult)
    return run(_go())


def _positions(Session):
    async def _go():
        async with Session() as db:
            return (await db.execute(select(PaperPosition))).scalars().all()
    return run(_go())


def _decisions(Session, *, include_degradation=False):
    async def _go():
        async with Session() as db:
            rows = (await db.execute(select(NoTradeDecision))).scalars().all()
            return [r for r in rows if include_degradation or r.stage != "DEGRADATION"]
    return run(_go())


# ── order ────────────────────────────────────────────────────────────────────

def test_check_zero_runs_before_any_risk_read(day):
    """A sub-1:2 signal is refused as malformed before one DB or Redis read."""
    class _Exploding:
        async def execute(self, *a, **k):
            raise AssertionError("check 0 must not read risk state")
        def get_bind(self):
            raise AssertionError("check 0 must not touch the DB")

    mode_manager._halted = True                          # would be KILL_SWITCH, check 2
    sig = make_signal(sl_pct=0.10, target_pct=0.15)

    d = run(risk_gate.evaluate_pre_selection(_Exploding(), sig, regime_state()))
    assert (d.stage.value, d.code.value) == ("SIGNAL_GATE", "RR_INSUFFICIENT")
    assert d.context["rr"] == pytest.approx(1.5)


def test_rr_epsilon_lets_the_canonical_setup_through(day, sqlite_db):
    # CPython evaluates 0.30 / 0.15 as exactly 2.0, so the epsilon is exercised with a
    # float-noise-sized shortfall: a ratio a hair under 2 must still pass the 1:2 floor.
    assert (0.30 - 1e-9) / 0.15 < 2.0
    assert _phase1(sqlite_db, make_signal(sl_pct=0.15, target_pct=0.30)).allowed is True
    assert _phase1(sqlite_db, make_signal(sl_pct=0.15, target_pct=0.30 - 1e-9)).allowed is True
    assert _phase1(sqlite_db, make_signal(sl_pct=0.15, target_pct=0.2999)).code.value == \
        "RR_INSUFFICIENT"
    assert _phase1(sqlite_db, make_signal(sl_pct=0.10, target_pct=0.15)).code.value == \
        "RR_INSUFFICIENT"


def test_checks_fire_in_table_order(day, sqlite_db, monkeypatch):
    now = day.clock.now_utc()
    # state that fails kill switch, entry cutoff and the daily loss cap at once
    mode_manager._halted = True
    pin_ist_time(monkeypatch, dtime(15, 30))
    run(add_closed(sqlite_db, net=-7000.0, at=now - timedelta(minutes=5)))
    assert _phase1(sqlite_db).code.value == "KILL_SWITCH"                  # 2 before 3
    mode_manager._halted = False
    assert _phase1(sqlite_db).code.value == "AFTER_ENTRY_CUTOFF"           # 3 before 4
    pin_ist_time(monkeypatch, dtime(10, 0))
    assert _phase1(sqlite_db).code.value == "DAILY_LOSS_CAP"               # 4 before 5-8


# ── every code reachable ─────────────────────────────────────────────────────

def test_not_paper_mode_is_reachable_and_logs_error(day, sqlite_db, caplog):
    with using(with_cfg(day, mode="LIVE")):
        d = _phase1(sqlite_db)
    assert d.code.value == "NOT_PAPER_MODE" and d.stage.value == "RISK_GATE"
    assert any(r.levelname == "ERROR" for r in caplog.records)


def test_persisted_kill_switch_is_reachable(day, sqlite_db):
    run(mode_manager.set_trading_halted(True, "ops"))
    mode_manager._halted = False                       # only the persisted flag remains
    d = _phase1(sqlite_db)
    assert d.code.value == "KILL_SWITCH" and "ops" in d.reason


def test_trade_cap_open_positions_and_regime_zero_are_reachable(day, sqlite_db, monkeypatch):
    with using(with_cfg(day, max_trades_per_day=1)):
        run(add_closed(sqlite_db, net=5.0, at=day.clock.now_utc() - timedelta(minutes=40)))
        d = _phase1(sqlite_db)
    assert d.code.value == "DAILY_TRADE_CAP" and d.context["max_trades_per_day"] == 1

    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", lambda u: 0.0)
    assert _phase1(sqlite_db).code.value == "REGIME_MULTIPLIER_ZERO"


def test_a_multiplier_lookup_failure_fails_closed(day, sqlite_db, monkeypatch):
    def _boom(u):
        raise RuntimeError("orchestrator down")
    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", _boom)
    d = _phase1(sqlite_db)
    assert d.allowed is False and d.code.value == "REGIME_UNAVAILABLE"       # never 1.0
    degraded = _decisions(sqlite_db, include_degradation=True)
    assert [r.code for r in degraded if r.stage == "DEGRADATION"] == ["REGIME_MULTIPLIER_ERROR"]


def test_unreadable_risk_state_fails_closed(day):
    class _Down:
        def get_bind(self):
            raise RuntimeError("db down")

        async def execute(self, *a, **k):
            raise RuntimeError("db down")
    d = run(risk_gate.evaluate_pre_selection(_Down(), make_signal(), regime_state()))
    assert d.allowed is False and d.code.value == "CONFIG_INVALID"


def test_sizing_codes_are_reachable(day, sqlite_db):
    # LOT_UNAFFORDABLE: the contract is too dear for this trade's ₹20,000 budget
    assert _phase2(sqlite_db, fake_candidate(ltp=700.0)).code.value == "LOT_UNAFFORDABLE"
    # SIZED_TO_ZERO: affordable, but max_lots_per_trade clamps it to nothing
    with using(with_cfg(day, max_lots_per_trade=0)):
        d = _phase2(sqlite_db, fake_candidate(ltp=40.0))
    assert d.code.value == "SIZED_TO_ZERO" and d.context["binding_clamp"] == "max_lots_per_trade"
    # INITIAL_RISK_NON_POSITIVE: no stop distance
    d = _phase2(sqlite_db, fake_candidate(ltp=40.0), signal=make_signal(sl_pct=0.0))
    assert d.code.value == "INITIAL_RISK_NON_POSITIVE"
    # COSTS_EXCEED_EDGE: 3 × 1-lot at ₹10 earns ₹9 gross at target; costs are ₹120+
    d = _phase2(sqlite_db, fake_candidate(ltp=10.0, lot_size=1))
    assert d.code.value == "COSTS_EXCEED_EDGE" and d.context["cost"] > d.context["edge"] * 0.33


def test_sizing_uses_the_fill_and_the_lot_size(day, sqlite_db):
    quoted = fake_candidate(ltp=100.0, lot_size=50, bid=99.5, ask=100.5, tick_age_s=2.0)
    d = _phase2(sqlite_db, quoted, base=20_000.0, mult=1.0)
    assert d.allowed and d.sizing.expected_entry_fill == pytest.approx(100.60)   # ask + 0.05%, rounded UP to the ₹0.05 tick
    assert d.context["fill_source"] == "ASK"
    assert d.sizing.lot_size == 50
    assert d.sizing.lots == 3 and d.sizing.capital_used == pytest.approx(3 * 100.60 * 50)
    # without a valid quote the same contract is sized on the dearer LTP fallback
    lone = _phase2(sqlite_db, fake_candidate(ltp=100.0, lot_size=50))
    assert lone.sizing.expected_entry_fill == pytest.approx(100.2)
    assert lone.context["fill_source"] == "LTP_FALLBACK"


# ── the order handle_signal follows ──────────────────────────────────────────

def test_sizing_and_selection_are_never_reached_on_a_halted_day(day, sqlite_db, monkeypatch):
    reached = []

    async def _select(**kw):
        reached.append("select")
        return selection_result(fake_candidate())

    async def _sizing(*a, **k):
        reached.append("sizing")
        raise AssertionError("sizing must not run on a halted day")

    monkeypatch.setattr(option_selector, "select", _select)
    monkeypatch.setattr(risk_gate, "evaluate_sizing", _sizing)
    mode_manager._halted = True
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert reached == [] and _positions(sqlite_db) == []
    (d,) = _decisions(sqlite_db)
    assert (d.stage, d.code) == ("RISK_GATE", "KILL_SWITCH")


def test_selection_receives_the_unadjusted_base_and_the_multiplier(day, sqlite_db, monkeypatch):
    seen = {}

    async def _select(**kw):
        seen.update(kw)
        return selection_result(fake_candidate(ltp=40.0, lot_size=65))

    monkeypatch.setattr(option_selector, "select", _select)
    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", lambda u: 0.5)
    assert run(paper_engine.handle_signal(make_signal())) is True
    assert seen["base_amount"] == 20_000.0 and seen["regime_multiplier"] == 0.5
    (pos,) = _positions(sqlite_db)
    assert pos.sizing_detail["trade_amount"] == 10_000.0       # 0.5 applied once, not squared
    assert pos.entry_regime_multiplier == 0.5
    assert pos.lots == 3 and pos.trade_amount_used == pytest.approx(3 * 40.08 * 65, abs=0.01)


def test_every_refusal_records_one_row_and_a_skipped_signal(day, sqlite_db, monkeypatch):
    from app.db.models.paper_trade import SignalStatus, StrategySignal
    stub_select(monkeypatch, None, rejections={"OI_UNAVAILABLE": 11})
    assert run(paper_engine.handle_signal(make_signal())) is False

    async def _sigs():
        async with sqlite_db() as db:
            return (await db.execute(select(StrategySignal))).scalars().all()
    (sig,) = run(_sigs())
    (d,) = _decisions(sqlite_db)
    assert sig.status == SignalStatus.SKIPPED and sig.no_trade_decision_id == d.id
    assert (d.stage, d.code) == ("SELECTION", "NO_OPTION_AVAILABLE")
    assert d.context["rejections"] == {"OI_UNAVAILABLE": 11}
    assert sig.selection_rejections == {"OI_UNAVAILABLE": 11}


def test_regime_multiplier_exception_opens_no_position(day, sqlite_db, monkeypatch):
    """Fault injection (§20.3): the old code restored full size on this exception."""
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))

    def _boom(u):
        raise RuntimeError("boom")
    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", _boom)
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert _positions(sqlite_db) == []


# ── the entry section and its lock ───────────────────────────────────────────

def _enter(Session, ctx, *, capital_used=2_000.0, insert=None, timeout=5.0):
    async def _insert(db):
        db.add(PaperPosition(**position_fields(run_id="PAPER", entry_time=ctx.clock.now_utc(),
                                               trade_amount_used=capital_used)))
        await db.flush()
        return "inserted"

    async def _go():
        async with Session() as db:
            return await risk_gate.enter(
                db, run_id="PAPER", trading_day=ctx.clock.trading_day(),
                capital_used=capital_used, advisory_used=0.0, insert=insert or _insert,
                lock_timeout_s=timeout)
    return _go()


def test_two_concurrent_entries_cannot_both_take_the_last_slot(day, sqlite_db):
    """The cap is exact because the count and the insert are one serialised section."""
    async def _both():
        return await asyncio.gather(_enter(sqlite_db, day), _enter(sqlite_db, day))

    with using(with_cfg(day, max_trades_per_day=1)):
        a, b = run(_both())
    assert sorted([a.allowed, b.allowed]) == [False, True]
    loser = a if not a.allowed else b
    assert loser.code.value == "DAILY_TRADE_CAP" and loser.stage.value == "RISK_GATE"
    assert len(_positions(sqlite_db)) == 1


def test_lock_not_acquired_in_time_is_a_cap_race_and_holds_nothing(day, sqlite_db):
    async def _held():
        lock = risk_gate._sqlite_locks.setdefault(("PAPER", day.clock.trading_day()),
                                                  asyncio.Lock())
        await lock.acquire()                                  # something else holds the section
        try:
            return await _enter(sqlite_db, day, timeout=0.05)
        finally:
            lock.release()
    d = run(_held())
    assert d.allowed is False and d.code.value == "DAILY_TRADE_CAP_RACE"
    assert d.stage.value == "ENTRY"
    assert _positions(sqlite_db) == []


def test_an_insert_exception_rolls_back_and_releases_the_lock(day, sqlite_db):
    async def _bad(db):
        db.add(PaperPosition(**position_fields(run_id="PAPER", trade_amount_used=2_000.0)))
        await db.flush()
        raise RuntimeError("insert failed")

    async def _go():
        with pytest.raises(RuntimeError):
            await _enter(sqlite_db, day, insert=_bad)
        return await _enter(sqlite_db, day)                 # the lock must be free again
    ok = run(_go())
    assert ok.allowed and ok.result == "inserted"
    assert len(_positions(sqlite_db)) == 1                  # only the second, successful entry


def test_an_unexpected_insert_error_in_handle_signal_leaves_no_row(day, sqlite_db, monkeypatch):
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))
    real = risk_gate.enter

    async def _enter_boom(db, **kw):
        async def _insert(d):
            await kw["insert"](d)
            raise RuntimeError("commit failed")
        return await real(db, **{**kw, "insert": _insert})
    monkeypatch.setattr(risk_gate, "enter", _enter_boom)
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert _positions(sqlite_db) == []
    codes = [r.code for r in _decisions(sqlite_db, include_degradation=True)]
    assert "UNEXPECTED_ENTRY_ERROR" in codes


def test_sizing_judges_costs_on_the_real_engine_target(day, sqlite_db):
    """TREND_ENGINE signals carry placeholder 15%/30% levels; the cost-vs-edge
    check must use the delta-converted premium target the position will carry.
    Here the real target (2 premium pts) is too small for the costs, while the
    30% placeholder (30 pts) would have passed."""
    cand = fake_candidate(ltp=100.0, lot_size=65)
    assert _phase2(sqlite_db, cand).allowed is True                    # placeholder levels
    real = lambda fill: (1.0 / fill, 2.0 / fill)                       # stop 1 pt, target 2 pts
    fraction = risk_gate.FractionDecision(method="FIXED_FRACTION", applied_fraction=0.1,
                                          base_amount=20_000.0)

    async def _go():
        async with sqlite_db() as db:
            return await risk_gate.evaluate_sizing(db, make_signal(), cand, fraction=fraction,
                                                   regime_multiplier=1.0, levels=real)
    d = run(_go())
    assert d.allowed is False and d.code.value == "COSTS_EXCEED_EDGE"
