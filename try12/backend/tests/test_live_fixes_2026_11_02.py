"""
Live check, Monday 2 Nov 2026:
  * the 15:35 collector had written market_daily_snapshots rows for Sat 10 Oct
    and Sun 1 Nov from stray off-hours ticks; the Sunday row became "the
    previous day" (the chart's PDL read 22809.2, Friday's real low 22905.1);
  * the diagnostics regime probe raised NameError '_params' on every call;
  * a refused CPR_VWAP call (puts only) left the day open for a later put,
    which the backtested rule (the day's first break only) never takes.
"""
from datetime import date, datetime, time as dtime, timedelta
from types import SimpleNamespace

from app.services.market_data import prev_session as ps
from tests.helpers import run


def _snapshots_table(Session):
    from app.db.models.market_study import MarketDailySnapshot

    async def go():
        async with Session() as db:
            await db.run_sync(lambda s: MarketDailySnapshot.__table__.create(s.connection()))
    run(go())
    return MarketDailySnapshot


def test_the_previous_snapshot_skips_weekend_rows(sqlite_db):
    Snap = _snapshots_table(sqlite_db)

    async def go():
        async with sqlite_db() as db:
            for d, low in ((date(2026, 10, 30), 22905.1), (date(2026, 11, 1), 22809.2)):
                db.add(Snap(date=d, underlying="NIFTY", open=23000.0, high=23118.1, low=low,
                            close=23053.1))
            await db.commit()
            friday = await ps.prev_session_snapshot(db, "NIFTY", date(2026, 11, 2))
            none = await ps.prev_session_snapshot(db, "NIFTY", date(2026, 10, 30))
            return friday, none
    friday, none = run(go())
    assert (friday.date, friday.low) == (date(2026, 10, 30), 22905.1)
    assert none is None


def test_diagnostics_regime_probe_does_not_raise(runtime_ctx):
    from app.api.routes.trading import get_signal_diagnostics
    body = run(get_signal_diagnostics())
    blockers = body.blockers if hasattr(body, "blockers") else body["blockers"]
    details = [getattr(b, "detail", None) or (b.get("detail") if isinstance(b, dict) else "")
               for b in blockers]
    assert not any("_params" in (d or "") for d in details), details


def _bars_day(day, closes):
    """1-min candles from 09:15 whose 5-min closes walk through *closes*."""
    out, t = [], datetime.combine(day, dtime(9, 15))
    for c in closes:
        for _ in range(5):
            out.append({"timestamp": t.strftime("%Y-%m-%d %H:%M:%S"), "open": c, "high": c + 1,
                        "low": c - 1, "close": c, "volume": 100})
            t += timedelta(minutes=1)
    return out


def test_cpr_vwap_takes_only_the_days_first_break(runtime_ctx):
    from app.runtime.context import trading_day
    from app.strategies.strategies import CPRVWAP

    s = CPRVWAP()
    day = trading_day()
    # yesterday H/L/C give a narrow CPR around 23025 (|TC-BC| ~ 0.12 %)
    s.set_prev_session(day, SimpleNamespace(high=23118.1, low=22905.1, close=23053.1))
    assert s.prev_session_problem() is None

    up = _bars_day(day, [23030, 23060, 23090, 23110, 23120])     # opens above: a call break
    sig = None
    for k in range(5, len(up) + 1, 5):
        sig = run(s.evaluate(up[:k], {})) or sig
    assert sig is not None and sig.direction.value == "BUY"
    assert s._traded_day == day                         # the day is used, refused or not

    down = up + _bars_day(day, [22990, 22950, 22900])[: 15]     # later falls through the CPR
    for k in range(len(up) + 5, len(down) + 1, 5):
        assert run(s.evaluate(down[:k], {})) is None
