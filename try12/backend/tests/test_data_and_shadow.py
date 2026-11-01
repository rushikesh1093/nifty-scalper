"""
Data and evidence additions (1 Nov 2026): the option archive runs before tick
retention deletes anything, refused signals get a shadow (counterfactual)
outcome after the close, and a 300-point-wing iron fly runs beside the 200 one.
"""
import asyncio
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.trading import shadow_outcomes as so
from app.services.trading.exit_plan import ExitPlan
from tests.helpers import run

IST = timezone(timedelta(hours=5, minutes=30))
DAY = date(2026, 11, 3)


# ── option archive before retention ─────────────────────────────────────────

def test_archive_disabled_returns_none(monkeypatch):
    from app.core.config import settings
    from app.services.market_data import tick_retention as tr
    monkeypatch.setattr(settings, "OPTION_ARCHIVE_PATH", "")
    assert run(tr.archive_options()) is None


def test_archive_runs_before_the_cleanup(monkeypatch, tmp_path):
    from app.core.config import settings
    from app.services.market_data import tick_retention as tr
    calls = []

    class _Proc:
        returncode = 0

        async def communicate(self):
            return b"2026-11-03 postgres: 100 option minutes, 10 spot minutes\n", b""

    async def fake_exec(*cmd, **kw):
        calls.append(("archive", cmd))
        return _Proc()

    async def fake_cleanup(*a, **kw):
        calls.append(("cleanup",))
        return tr.CleanupResult(cutoff=DAY)

    monkeypatch.setattr(settings, "OPTION_ARCHIVE_PATH", str(tmp_path / "a.db"))
    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
    monkeypatch.setattr(tr, "run_cleanup", fake_cleanup)
    run(tr.TickRetention().run_now())
    assert [c[0] for c in calls] == ["archive", "cleanup"]
    cmd = calls[0][1]
    assert "app.research.option_archive" in cmd and str(tmp_path / "a.db") in cmd


# ── shadow outcomes ─────────────────────────────────────────────────────────

def _day(bars, start=DAY):
    t0 = datetime(start.year, start.month, start.day, 9, 15, tzinfo=IST)
    return [{"timestamp": (t0 + timedelta(minutes=i)).isoformat(), "open": o, "high": h, "low": l,
             "close": c, "volume": 0} for i, (o, h, l, c) in enumerate(bars)]


def _plan(**kw):
    base = dict(strategy="ORB_BREAKOUT", side="SELL", spot_entry=22000.0, spot_stop=22030.0,
                spot_target=21940.0)
    base.update(kw)
    return ExitPlan(**base).to_dict()


def test_shadow_hits_the_target_and_nets_the_cost():
    bars = [(22000, 22002, 21998, 22000)] * 60 + [(22000, 22001, 21930, 21935)] + [(21935,) * 4] * 300
    today = so._candles(_day(bars))
    sig_t = datetime(2026, 11, 3, 10, 15, 1, tzinfo=IST)            # the 10:15 candle fills
    res = so.shadow_outcome(_plan(), "ORB_BREAKOUT", sig_t, today, [])
    assert res["exit"] == "TARGET"
    assert 1.9 < res["r_net"] < 2.0                                  # 2R gross minus cost
    assert res["hold_min"] == 0.0                                    # hit inside the fill candle


def test_shadow_takes_the_stop_when_a_candle_touches_both():
    bars = [(22000, 22002, 21998, 22000)] * 60 + [(22000, 22040, 21930, 22000)] + [(22000,) * 4] * 300
    res = so.shadow_outcome(_plan(), "ORB_BREAKOUT", datetime(2026, 11, 3, 10, 15, tzinfo=IST),
                            so._candles(_day(bars)), [])
    assert res["exit"] == "SPOT_SL" and res["r_net"] < -1.0


def test_shadow_after_the_last_candle_is_no_fill():
    bars = [(22000, 22002, 21998, 22000)] * 30
    res = so.shadow_outcome(_plan(), "ORB_BREAKOUT", datetime(2026, 11, 3, 14, 0, tzinfo=IST),
                            so._candles(_day(bars)), [])
    assert res == {"r_net": None, "exit": so.NO_FILL, "hold_min": None}


def test_candles_are_normalised_and_session_only():
    raw = _day([(1, 2, 0, 1)] * 3) + [{"timestamp": "2026-11-03T16:00:00+05:30", "open": 1, "high": 1,
                                        "low": 1, "close": 1}]
    out = so._candles(raw)
    assert len(out) == 3 and isinstance(out[0]["timestamp"], datetime)


def test_evaluate_day_fills_skipped_signals_only(runtime_ctx, sqlite_db):
    from sqlalchemy import select
    from app.db.models.paper_trade import SignalDirection, SignalStatus, StrategySignal, TradingMode

    async def seed():
        async with sqlite_db() as db:
            for status, plan in ((SignalStatus.SKIPPED, _plan()), (SignalStatus.ENTERED, _plan()),
                                 (SignalStatus.SKIPPED, None)):
                db.add(StrategySignal(strategy_name="ORB_BREAKOUT", underlying="NIFTY",
                                      direction=SignalDirection.SELL, status=status, spot_price=22000.0,
                                      signal_time=datetime(2026, 11, 3, 10, 15, tzinfo=IST),
                                      mode=TradingMode.PAPER, run_id="PAPER", exit_plan=plan,
                                      skip_reason="ORB_BREAKOUT trades puts only"))
            await db.commit()
    run(seed())
    bars = [(22000, 22002, 21998, 22000)] * 60 + [(22000, 22001, 21930, 21935)] + [(21935,) * 4] * 300

    async def go():
        async with sqlite_db() as db:
            n = await so.evaluate_day(db, "PAPER", DAY, _day(bars))
            rows = (await db.execute(select(StrategySignal).order_by(StrategySignal.id))).scalars().all()
            return n, [(r.status, r.shadow_exit) for r in rows]
    n, rows = run(go())
    assert n == 1
    assert rows[0][1] == "TARGET" and rows[1][1] is None and rows[2][1] is None


def test_skip_codes_and_summary():
    assert so.skip_code("ORB_BREAKOUT trades puts only (pe_only_strategies)") == "DIRECTION_POLICY"
    assert so.skip_code("SUPERTREND_EMA is blocked by research evidence") == "EVIDENCE_BLOCKED"
    assert so.skip_code("EMA_RSI_PULLBACK_15M skips entries in LOW volatility") == "LOW_VOL_SKIP"
    assert so.skip_code("Daily trade limit (2) reached.") == "DAILY_TRADE_CAP"
    s = so.summarize([{"strategy": "ORB", "code": "DIRECTION_POLICY", "r_net": -0.5},
                      {"strategy": "ORB", "code": "DIRECTION_POLICY", "r_net": 0.1},
                      {"strategy": "ORB", "code": "X", "r_net": None}])
    assert s == {"ORB": {"DIRECTION_POLICY": {"n": 2, "mean_r_net": -0.2}}}


# ── 300-point-wing iron fly ─────────────────────────────────────────────────

def test_two_iron_fly_books_with_their_own_names_and_wings():
    from app.services.trading import iron_fly as fly
    assert set(fly.IRON_FLY_BOOKS) == {200, 300}
    a, b = fly.IRON_FLY_BOOKS[200], fly.IRON_FLY_BOOKS[300]
    assert (a.strategy, a.wing) == ("EXPIRY_IRON_FLY", 200)
    assert (b.strategy, b.wing) == ("EXPIRY_IRON_FLY_W300", 300)


def test_w300_book_opens_with_300_point_wings(runtime_ctx, fake_redis, sqlite_db):
    from app.core.config import settings
    from app.services.trading import iron_fly as fly
    from tests.test_iron_fly import EXPIRY, SPOT, ist, put_quotes, rows, spreads
    from sqlalchemy import create_engine
    from app.db.base import Base
    from app.db.models.paper_spread import PaperSpread
    from app.runtime.context import using
    from tests.helpers import with_instruments
    url = str(sqlite_db.kw["bind"].url).replace("+aiosqlite", "")
    eng = create_engine(url)
    Base.metadata.create_all(eng, tables=[PaperSpread.__table__])
    eng.dispose()
    rc = with_instruments(runtime_ctx, rows())
    with using(rc):
        runtime_ctx.clock.set(ist(9, 31))
        fake_redis.put_tick(settings.NIFTY_TOKEN, ltp=SPOT)
        put_quotes(fake_redis, {(24000, "CE"): (80.0, 80.5), (24000, "PE"): (70.0, 70.5),
                                (24300, "CE"): (6.0, 6.5), (23700, "PE"): (5.0, 5.5)})
        run(fly.IronFlyPaper(fly.STRATEGY_W300, 300).cycle())
        [row] = run(spreads())
    assert row.strategy_name == "EXPIRY_IRON_FLY_W300"
    assert sorted(l["strike"] for l in row.legs) == [23700.0, 24000.0, 24000.0, 24300.0]
    assert row.max_loss == pytest.approx((300 - row.credit_pts) * row.lot_size)


# ── startup: schema must be migrated ────────────────────────────────────────

def test_schema_head_matches_the_newest_migration():
    from pathlib import Path
    from app.services.trading.startup_checks import SCHEMA_HEAD
    newest = max(p.name[:4] for p in Path("migrations/versions").glob("0*.py"))
    assert SCHEMA_HEAD == newest


def test_a_database_behind_the_code_fails_startup(sqlite_db):
    from sqlalchemy import text
    from app.services.trading.startup_checks import check_schema_revision

    async def setv(v):
        async with sqlite_db() as db:
            await db.execute(text("CREATE TABLE IF NOT EXISTS alembic_version (version_num VARCHAR(32))"))
            await db.execute(text("DELETE FROM alembic_version"))
            await db.execute(text("INSERT INTO alembic_version VALUES (:v)"), {"v": v})
            await db.commit()
    assert run(check_schema_revision(sqlite_db)) == []          # no table: not checked
    run(setv("0016"))
    [v] = run(check_schema_revision(sqlite_db))
    assert "0016" in v and "alembic upgrade head" in v
    run(setv("0017"))
    assert run(check_schema_revision(sqlite_db)) == []
