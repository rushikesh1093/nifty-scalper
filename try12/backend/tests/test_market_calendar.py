"""NSE trading calendar: holidays close the runner, diagnostics and EOD snapshot."""
from datetime import date, datetime, timedelta, timezone

from app.services.trading import market_calendar as mc
from app.services.trading.signal_diagnostics import derive_blockers, session_window_open
from tests.helpers import run

IST = timezone(timedelta(hours=5, minutes=30))


def test_2026_holidays_and_weekends():
    assert mc.is_trading_day(date(2026, 10, 21))                  # Wednesday
    assert not mc.is_trading_day(date(2026, 10, 20))              # Dussehra
    assert not mc.is_trading_day(date(2026, 11, 10))              # Diwali Balipratipada
    assert not mc.is_trading_day(date(2026, 10, 24))              # Saturday
    assert mc.closed_reason(date(2026, 12, 25)) == "NSE holiday (Christmas)"
    assert mc.closed_reason(date(2026, 10, 25)) == "weekend"
    assert all(d.weekday() < 5 for d in mc.NSE_HOLIDAYS)          # weekend holidays not listed


def test_diagnostics_window_is_closed_on_a_holiday():
    from datetime import time
    now = datetime(2026, 10, 20, 11, 0, tzinfo=IST)
    assert session_window_open(now, time(9, 15), time(15, 25)) is False
    state = {"now_ist": now, "session_start": time(9, 15), "session_end": time(15, 25)}
    mc_rows = [b for b in derive_blockers(state) if b["code"] == "MARKET_CLOSED"]
    assert mc_rows and "Dussehra" in mc_rows[0]["detail"]


def test_runner_does_not_evaluate_on_a_holiday(runtime_ctx, sqlite_db, monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner
    from tests.helpers import seed_config
    run(seed_config(sqlite_db))
    runtime_ctx.clock.set(datetime(2026, 10, 20, 11, 0, tzinfo=IST))
    called = []

    async def _never(*a, **k):
        called.append(True)
        return True

    monkeypatch.setattr(strategy_runner, "_run_underlying", _never)
    run(strategy_runner.run_cycle("NIFTY"))
    assert called == []
    runtime_ctx.clock.set(datetime(2026, 10, 21, 11, 0, tzinfo=IST))   # next day trades
    run(strategy_runner.run_cycle("NIFTY"))
    assert called == [True]
