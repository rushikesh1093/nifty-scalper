"""Fix 10: daily_trades_count resets by IST trading date, not on process start."""
from datetime import datetime, timedelta

from app.services.trading.mode_manager import IST, mode_manager
from app.services.trading.strategy_runner import strategy_runner
from tests.helpers import add_position, get_config_row, run, seed_config


def _today():
    return datetime.now(IST).date()


def test_new_day_resets(sqlite_db):
    run(seed_config(sqlite_db, daily_trades_count=7, daily_trades_date=_today() - timedelta(days=1)))
    assert run(mode_manager.reset_daily_counter_if_new_day()) is True
    cfg = run(get_config_row(sqlite_db))
    assert cfg.daily_trades_count == 0 and cfg.daily_trades_date == _today()


def test_same_day_restart_keeps_count(sqlite_db):
    run(seed_config(sqlite_db, daily_trades_count=3, daily_trades_date=_today()))
    assert run(mode_manager.reset_daily_counter_if_new_day()) is False
    assert run(get_config_row(sqlite_db)).daily_trades_count == 3


def test_null_date_counts_todays_positions(sqlite_db):
    run(seed_config(sqlite_db, daily_trades_count=9, daily_trades_date=None))
    run(add_position(sqlite_db, is_open=False))
    run(add_position(sqlite_db, is_open=False))
    run(add_position(sqlite_db, is_open=False,
                     entry_time=datetime.now(IST) - timedelta(days=2)))
    run(mode_manager.reset_daily_counter_if_new_day())
    cfg = run(get_config_row(sqlite_db))
    assert cfg.daily_trades_count == 2 and cfg.daily_trades_date == _today()


def test_manual_reset_stamps_today(sqlite_db):
    run(seed_config(sqlite_db, daily_trades_count=5))
    run(mode_manager.reset_daily_counter())
    cfg = run(get_config_row(sqlite_db))
    assert cfg.daily_trades_count == 0 and cfg.daily_trades_date == _today()


def test_pre_market_setup_does_not_wipe_same_day_count(sqlite_db, monkeypatch):
    run(seed_config(sqlite_db, daily_trades_count=4, daily_trades_date=_today()))
    monkeypatch.setattr(strategy_runner, "_strategies", {})
    run(strategy_runner._pre_market_setup())   # MarketDailySnapshot table absent → logged, ignored
    assert run(get_config_row(sqlite_db)).daily_trades_count == 4
