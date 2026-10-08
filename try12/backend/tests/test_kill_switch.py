"""Fix 9: kill switch (halt + square off + persisted flag) and unrealised P&L in daily loss."""
from app.api.routes import trading as routes
from app.services.trading.mode_manager import ModeManager, mode_manager
from tests.helpers import add_position, get_config_row, get_position, run, seed_config


def test_kill_switch_halts_squares_off_and_persists(sqlite_db, fake_redis):
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
    assert cfg.used_capital == 0.0

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
    ok, _ = run(mode_manager.pre_trade_check("NIFTY"))
    assert ok
    assert run(routes.kill_switch_status())["halted"] is False


def test_kill_switch_without_body(sqlite_db, fake_redis):
    run(seed_config(sqlite_db))
    out = run(routes.kill_switch(None))
    assert out["reason"] == "manual kill switch" and out["closed"] == 0
    run(routes.kill_switch_release())


def test_daily_loss_includes_unrealised(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    run(add_position(sqlite_db))          # entry 100 × 65
    fake_redis.put_tick("OPT1", 90.0)     # unrealised −650 > ₹500 limit
    breached, reason = run(mode_manager._check_daily_loss())
    assert breached
    assert "unrealised ₹-650" in reason
    ok, why = run(mode_manager.pre_trade_check("NIFTY"))
    assert not ok and "Daily loss limit" in why


def test_daily_loss_falls_back_to_realised_on_redis_error(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    run(add_position(sqlite_db))
    fake_redis.put_tick("OPT1", 90.0)
    fake_redis.fail_ltp = True
    breached, _ = run(mode_manager._check_daily_loss())
    assert breached is False
