"""
Audit D1 (23 Oct): replay must recognise an expiry day the way live does.

Replay's InstrumentPort returns only contracts that traded that day, and on an
expiry day the expiring series often has no recorded ticks (the fixture
generator, like the real-path DB, quotes only the 2+ DTE series). The runner
used that universe to decide "is today an expiry day", so replay never saw one
and traded days live refuses (EXPIRY_DAY is a no-trade regime).
"""
from datetime import date

from sqlalchemy import select

from app.db.models.paper_trade import PaperPosition
from app.db.models.regime import RegimeStateModel as RegimeState
from app.runtime.context import using
from app.services.replay.ports_impl import ReplayInstruments
from tests.helpers import make_test_context, FrozenClock
from tests.replay_fixtures import (
    assert_complete, fill_db, isolate_singletons, make_db, minute_index, replay, run,
    scripted_factory, scripted_quote,
)

EXPIRY_TUE = date(2026, 9, 8)
NEXT_WED = date(2026, 9, 9)
EXPIRIES = [date(2026, 9, 8), date(2026, 9, 15), date(2026, 9, 22)]


def _expiry_aware_regime(mp):
    """Like pin_regime, but keeps the classifier's first rule: is_expiry_day -> EXPIRY_DAY."""
    import app.services.trading.regime_detector as rd

    def _classify(indicators, underlying, is_expiry_day, previous_regime, config):
        return "EXPIRY_DAY" if is_expiry_day else "BULL_TREND_NORMAL_VOL"
    mp.setattr(rd, "classify_regime", _classify)


def _db(tmp_path):
    Session, engine, url = make_db(tmp_path / "expiry.db")
    fill_db(url, [EXPIRY_TUE, NEXT_WED], scripted_quote({}), expiries=EXPIRIES)
    return Session, engine


def test_the_expiring_series_has_no_ticks_but_nearest_expiry_still_finds_it(tmp_path):
    Session, engine = _db(tmp_path)
    try:
        from datetime import datetime, time, timezone, timedelta
        at = datetime.combine(EXPIRY_TUE, time(10, 0), timezone(timedelta(hours=5, minutes=30)))
        port = ReplayInstruments()
        with using(make_test_context(clock=FrozenClock(at))):
            async def _go():
                async with Session() as db:
                    traded = {i.expiry for i in await port.instruments_for(db, "NIFTY")}
                    return traded, await port.nearest_expiry(db, "NIFTY")
            traded, nearest = run(_go())
        assert EXPIRY_TUE not in traded          # the condition that hid the expiry day
        assert nearest == EXPIRY_TUE             # answered from the instrument master
    finally:
        run(engine.dispose())


def test_replay_takes_no_trade_on_an_expiry_day(tmp_path, monkeypatch):
    _expiry_aware_regime(monkeypatch)
    isolate_singletons(monkeypatch)
    Session, engine = _db(tmp_path)
    try:
        schedule = {"SUPERTREND_EMA": {"10:00": "BUY"}}
        r = replay(Session, EXPIRY_TUE, NEXT_WED, scripted_factory(schedule))
        assert_complete(r)

        async def _go():
            async with Session() as db:
                pos = (await db.execute(select(PaperPosition).where(
                    PaperPosition.run_id == r.run_id))).scalars().all()
                regimes = (await db.execute(select(RegimeState.timestamp, RegimeState.regime).where(
                    RegimeState.run_id == r.run_id))).all()
                return pos, regimes
        pos, regimes = run(_go())
        assert [p.entry_time.date() for p in pos] == [NEXT_WED]       # Tuesday refused, Wednesday traded
        tue = {reg for ts, reg in regimes if ts.date() == EXPIRY_TUE}
        wed = {reg for ts, reg in regimes if ts.date() == NEXT_WED}
        assert tue == {"EXPIRY_DAY"} and "EXPIRY_DAY" not in wed
    finally:
        run(engine.dispose())
