"""Bug 9 (design §9.4, R10): losing-streak halts (3 global, 2 per strategy) and the
15-minute post-loss cooldown, all derived from the day's closed positions so a
restart cannot clear them; a win resets, a flat trade (|net| <= ₹1) is neutral."""
from datetime import time as dtime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.risk import RiskHalt
from app.runtime.context import using
from app.services.trading import risk_gate, risk_state
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests.helpers import (
    add_closed, add_position, allow_regime, make_signal, pin_ist_time, regime_state,
    reset_singletons, run, seed_config, with_cfg, with_params,
)


@pytest.fixture
def day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    return runtime_ctx


def _closed(Session, ctx, nets, strategies=None, minutes_ago=60, step=5):
    """Closed trades, oldest first, the last one *minutes_ago* before the clock."""
    now = ctx.clock.now_utc()
    n = len(nets)
    for i, net in enumerate(nets):
        strat = (strategies[i] if strategies else "VWAP_EMA_PULLBACK")
        run(add_closed(Session, net=net, strategy=strat,
                       at=now - timedelta(minutes=minutes_ago + (n - 1 - i) * step)))


def _streak(Session, ctx):
    async def _go():
        async with Session() as db:
            return await risk_state.streak_state(db, "PAPER", ctx.clock.trading_day())
    return run(_go())


def _gate(Session, strategy="VWAP_EMA_PULLBACK"):
    async def _go():
        async with Session() as db:
            return await risk_gate.evaluate_pre_selection(
                db, make_signal(strategy=strategy), regime_state())
    return run(_go())


def test_three_consecutive_losses_halt_the_day(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100, -100], strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP"])
    s = _streak(sqlite_db, day)
    assert s.consecutive_losses == 3 and s.global_halt
    gate = _gate(sqlite_db, "ORB_BREAKOUT")
    assert gate.allowed is False and gate.code.value == "LOSING_STREAK_HALT"
    assert gate.context["consecutive_losses"] == 3
    (halt,) = [h for h in _halts(sqlite_db) if h.halt_type == "LOSING_STREAK"]
    assert halt.context["position_ids"] == s.triggering_position_ids


def _halts(Session):
    async def _go():
        async with Session() as db:
            return (await db.execute(select(RiskHalt))).scalars().all()
    return run(_go())


def test_two_losses_do_not_halt_the_day(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100], strategies=["EMA_VWAP", "OI_SPIKE"])
    assert _streak(sqlite_db, day).global_halt is False
    assert _gate(sqlite_db, "ORB_BREAKOUT").allowed is True


def test_a_win_resets_the_streak(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100, 250, -100],
            strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP", "ORB_BREAKOUT"])
    s = _streak(sqlite_db, day)
    assert s.consecutive_losses == 1 and not s.global_halt
    assert _gate(sqlite_db, "PDH_PDL").allowed is True


def test_a_flat_trade_is_neutral(day, sqlite_db):
    """abs(net) <= ₹1 neither increments nor resets (R10.5)."""
    _closed(sqlite_db, day, [-100, 0.5, -100, -1.0, -100],
            strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP", "ORB_BREAKOUT", "PDH_PDL"])
    s = _streak(sqlite_db, day)
    assert s.consecutive_losses == 3 and s.global_halt        # the flats neither broke nor added
    run(add_closed(sqlite_db, net=1.0, strategy="RSI_VWAP",
                   at=day.clock.now_utc() - timedelta(minutes=20)))   # a flat after the streak
    assert _streak(sqlite_db, day).consecutive_losses == 3


def test_a_breakeven_exit_that_cost_money_is_a_loss(day, sqlite_db):
    """Gross R ≈ 0 but net −₹60 after charges: R-flat for Kelly, a LOSS for the streak."""
    _closed(sqlite_db, day, [-60, -60], strategies=["EMA_VWAP", "OI_SPIKE"])
    assert _streak(sqlite_db, day).consecutive_losses == 2


def test_cooldown_blocks_for_15_minutes_then_expires(day, sqlite_db):
    _closed(sqlite_db, day, [-100], minutes_ago=5)
    s = _streak(sqlite_db, day)
    assert s.cooldown_until == s.last_loss_exit_at + timedelta(minutes=15)
    blocked = _gate(sqlite_db)
    assert blocked.allowed is False and blocked.code.value == "POST_LOSS_COOLDOWN"
    day.clock.advance(minutes=9)                       # 14 minutes after the loss
    assert _gate(sqlite_db).code.value == "POST_LOSS_COOLDOWN"
    day.clock.advance(minutes=2)                       # 16 minutes after the loss
    assert _gate(sqlite_db).allowed is True


def test_two_losses_in_one_strategy_disable_only_that_strategy(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100], strategies=["VWAP_EMA_PULLBACK", "VWAP_EMA_PULLBACK"])
    s = _streak(sqlite_db, day)
    assert s.halted_strategies == frozenset({"VWAP_EMA_PULLBACK"}) and not s.global_halt
    halted = _gate(sqlite_db, "VWAP_EMA_PULLBACK")
    assert halted.allowed is False and halted.code.value == "STRATEGY_DAILY_HALT"
    assert _gate(sqlite_db, "SUPERTREND_EMA").allowed is True
    # and the strategy map reports it with the closed-set reason
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "VWAP_EMA_PULLBACK") == \
        (False, "daily_strategy_halt")
    assert strategy_orchestrator.is_strategy_enabled("NIFTY", "SUPERTREND_EMA")[0] is True
    assert [h.scope for h in _halts(sqlite_db) if h.halt_type == "STRATEGY_STREAK"] == \
        ["VWAP_EMA_PULLBACK"]


def test_state_survives_a_restart(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100, -100], strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP"])
    before = _streak(sqlite_db, day)
    reset_singletons()                                  # drop every in-memory object
    after = _streak(sqlite_db, day)
    assert (after.consecutive_losses, after.global_halt, after.halted_strategies) == \
        (before.consecutive_losses, before.global_halt, before.halted_strategies)


def test_only_todays_trades_count(day, sqlite_db):
    now = day.clock.now_utc()
    for i in range(3):
        run(add_closed(sqlite_db, net=-100.0, at=now - timedelta(days=1, minutes=i)))
    assert _streak(sqlite_db, day).consecutive_losses == 0


def test_an_operator_release_suppresses_the_halt_for_the_day(day, sqlite_db):
    _closed(sqlite_db, day, [-100, -100, -100], strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP"])
    assert _gate(sqlite_db, "ORB_BREAKOUT").code.value == "LOSING_STREAK_HALT"

    async def _release():
        async with sqlite_db() as db:
            h = (await db.execute(select(RiskHalt).where(
                RiskHalt.halt_type == "LOSING_STREAK"))).scalar_one()
            h.released_at, h.released_by, h.release_note = day.clock.now_utc(), "ops", "reviewed"
            await db.commit()
    run(_release())
    s = _streak(sqlite_db, day)
    assert s.consecutive_losses == 3 and s.global_halt is False   # counts untouched, halt released


# ── §23 "which control binds" ───────────────────────────────────────────────

def test_three_sub_1r_losses_engage_the_streak_halt_before_the_loss_cap(day, sqlite_db):
    """−₹100 each is far below 1R (1.5% of capital = ₹3,000), so only the global
    streak halt can bind: the one scenario in which it fires at the D2 defaults."""
    _closed(sqlite_db, day, [-100, -100, -100], strategies=["EMA_VWAP", "OI_SPIKE", "CPR_VWAP"])
    gate = _gate(sqlite_db, "ORB_BREAKOUT")
    assert gate.code.value == "LOSING_STREAK_HALT"


def test_two_full_losses_engage_the_daily_loss_cap_first(day, sqlite_db):
    _closed(sqlite_db, day, [-3000, -3000], strategies=["EMA_VWAP", "OI_SPIKE"])
    gate = _gate(sqlite_db, "ORB_BREAKOUT")
    assert gate.code.value == "DAILY_LOSS_CAP"
    assert _streak(sqlite_db, day).global_halt is False


def test_exposure_cap_is_unreachable_at_the_defaults_and_reachable_at_five(day, sqlite_db):
    """Same-side positions: the one-per-direction rule (D6) is lifted to reach the caps."""
    day = with_params(day, max_open_per_direction=10)
    for tok in ("A", "B", "C"):
        run(add_position(sqlite_db, token=tok, trade_amount_used=20_000.0))
    # defaults: 3 positions × ₹20,000 = ₹60,000 < the ₹80,000 cap, so the position cap binds
    with using(day):
        assert _gate(sqlite_db).code.value == "MAX_OPEN_POSITIONS"
    run(add_position(sqlite_db, token="D", trade_amount_used=20_000.0))
    with using(with_cfg(day, max_open_positions=5)):
        assert _gate(sqlite_db).code.value == "EXPOSURE_CAP"      # 4 × 20,000 = 80,000
