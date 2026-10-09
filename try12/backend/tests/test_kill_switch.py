"""Fix 9: kill switch (halt + square off + persisted flag) and unrealised P&L in daily loss."""
from datetime import time as dtime

from app.api.routes import trading as routes
from app.runtime.context import using
from app.services.trading import risk_state
from app.services.trading.mode_manager import ModeManager, mode_manager
from tests.helpers import (
    add_position, allow_regime, get_config_row, get_position, pin_ist_time, run, seed_config,
    with_cfg,
)


def test_kill_switch_halts_squares_off_and_persists(runtime_ctx, sqlite_db, fake_redis,
                                                    monkeypatch):
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    run(seed_config(sqlite_db, used_capital=13000.0, capital=20000.0))
    a = run(add_position(sqlite_db, token="A"))
    b = run(add_position(sqlite_db, token="B"))
    fake_redis.put_tick("A", 104.0, age_s=1)
    fake_redis.put_tick("B", 97.0, age_s=1)

    out = run(routes.kill_switch(routes.KillSwitchRequest(reason="test halt")))
    assert out["halted"] is True and out["persisted"] is True
    assert out["closed"] == 2 and out["failed"] == []
    for pid in (a, b):
        p = run(get_position(sqlite_db, pid))
        assert not p.is_open and p.exit_reason == "KILL_SWITCH"

    cfg = run(get_config_row(sqlite_db))
    assert cfg.trading_halted is True and cfg.halted_reason == "test halt"
    async def _open_capital():
        async with sqlite_db() as db:
            return await risk_state.open_capital_used(db, "PAPER")
    assert run(_open_capital()) == 0.0        # nothing to release: capital in use is derived

    ok, reason = run(mode_manager.pre_trade_check("NIFTY"))
    assert not ok and reason.startswith("Kill switch active")

    # Simulated restart: fresh instance has _halted False, DB flag still blocks
    fresh = ModeManager()
    assert fresh._halted is False
    ok, reason = run(fresh.pre_trade_check("NIFTY"))
    assert not ok and "Kill switch active" in reason

    # ensure_config (startup) must not clear the halt
    run(fresh.ensure_config(session_factory=sqlite_db))
    assert run(get_config_row(sqlite_db)).trading_halted is True

    status = run(routes.kill_switch_status())
    assert status["halted"] is True and status["open_positions"] == 0

    assert run(routes.kill_switch_release()) == {"halted": False}
    # The squared-off position B closed at a loss, so the post-loss cooldown (not the
    # kill switch) is what may still refuse an entry now.
    ok, why = run(mode_manager.pre_trade_check("NIFTY"))
    assert ok or "post-loss cooldown" in why
    assert "Kill switch" not in why
    assert run(routes.kill_switch_status())["halted"] is False


def test_kill_switch_without_body(sqlite_db, fake_redis):
    run(seed_config(sqlite_db))
    out = run(routes.kill_switch(None))
    assert out["reason"] == "manual kill switch" and out["closed"] == 0
    run(routes.kill_switch_release())


def _loss(sqlite_db):
    async def _go():
        async with sqlite_db() as db:
            return await risk_state.daily_loss_figure(db)
    return run(_go())


def test_daily_loss_includes_unrealised(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    run(seed_config(sqlite_db))
    run(add_position(sqlite_db))          # entry 100 × 65
    fake_redis.put_tick("OPT1", 90.0)     # unrealised −650, net of exit charges, > ₹500 limit
    with using(with_cfg(runtime_ctx, max_daily_loss=500.0)):
        loss = _loss(sqlite_db)
        assert loss.breached and loss.breach_source == "ABS"
        assert loss.realised == 0.0 and loss.unrealised < -650.0
        ok, why = run(mode_manager.pre_trade_check("NIFTY"))
    assert not ok and "Daily loss cap" in why


def test_unreadable_price_degrades_the_mark_instead_of_breaching(runtime_ctx, sqlite_db,
                                                                fake_redis):
    run(seed_config(sqlite_db))
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 90.0)
    fake_redis.fail_ltp = True
    with using(with_cfg(runtime_ctx, max_daily_loss=500.0)):
        loss = _loss(sqlite_db)
    assert loss.breached is False and loss.mtm_degraded is True
