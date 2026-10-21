"""Research telemetry: every completed candle the runner processes leaves one
trend_decisions row (decision, NO TRADE reason code, score, regime, the gate that
stopped the live path), and later candles fill the underlying's +15/+30/+60 min
prices. Recording is independent of execution and never stops the cycle."""
from dataclasses import replace
from datetime import datetime, timedelta

import pytest
from sqlalchemy import select

from app.db.models.research import TrendDecisionRecord
from app.services.trading.decision_telemetry import (
    decision_telemetry, forward_path, session_candles,
)
from tests.helpers import IST, run, seed_config

START = datetime(2026, 9, 1, 9, 15)       # naive IST, like the feed


def _c(i, px=None):
    ts = START + timedelta(minutes=i)
    p = 22600.0 + i if px is None else px
    return {"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": p, "high": p + 3,
            "low": p - 3, "close": p + 1, "volume": 0}


def _series(n, now_min=None):
    now = (START + timedelta(minutes=n if now_min is None else now_min)).replace(tzinfo=IST)
    return session_candles([_c(i) for i in range(n)], START.date(), now)


# ── pure forward path ────────────────────────────────────────────────────────

def test_forward_path_horizons_and_entry_fields():
    s = _series(120)
    t0 = s[30][0]                                     # the 09:45 candle
    f = forward_path(t0, s)
    assert f["next_open"] == 22631.0                  # open of 09:46
    assert (f["next2_high"], f["next2_low"]) == (22635.0, 22628.0)
    assert f["px_15"] == 22646.0                      # close of the 10:00 candle
    assert f["px_30"] == 22661.0 and f["px_60"] == 22691.0
    assert (f["high_60"], f["low_60"]) == (22693.0, 22628.0)
    assert f["complete"] is True


def test_forward_path_incomplete_until_horizon():
    s = _series(50)
    f = forward_path(s[30][0], s)
    assert "px_15" in f and "px_30" not in f and "px_60" not in f
    assert "high_60" not in f and f["complete"] is False


def test_forward_path_tolerates_a_missing_minute_only_briefly():
    rows = [_c(i) for i in range(80) if i != 45]       # 10:00 candle missing
    now = (START + timedelta(minutes=80)).replace(tzinfo=IST)
    s = session_candles(rows, START.date(), now)
    f = forward_path(s[30][0], s)
    assert f["px_15"] == 22645.0                      # as-of the 09:59 close
    rows = [_c(i) for i in range(80) if not 41 <= i <= 45]
    s = session_candles(rows, START.date(), now)
    assert "px_15" not in forward_path(s[30][0], s)    # > 2 minutes stale: left empty


def test_session_end_completes_late_decisions():
    rows = [_c(i) for i in range(375)]                 # 09:15 … 15:29
    now = datetime(2026, 9, 1, 15, 31, tzinfo=IST)
    s = session_candles(rows, START.date(), now)
    t0 = datetime(2026, 9, 1, 15, 0, tzinfo=IST)
    f = forward_path(t0, s)
    assert f["px_15"] is not None and "px_60" not in f
    assert f["complete"] is True and f["high_60"] >= f["low_60"]


def test_session_candles_drops_the_forming_candle():
    now = (START + timedelta(minutes=10, seconds=20)).replace(tzinfo=IST)
    s = session_candles([_c(i) for i in range(11)], START.date(), now)
    assert len(s) == 10                               # 09:25 is still forming


# ── through the runner ───────────────────────────────────────────────────────

class _NoInstruments:
    async def instruments_for(self, db, underlying):
        return []

    def invalidate(self):
        pass


@pytest.fixture
def engine_runner(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    from app.core.config import settings
    from app.runtime.context import using
    from app.services.trading import strategy_runner as sr
    from app.strategies.names import ALL_STRATEGY_NAMES
    from app.strategies.trend_engine import TrendFollowingEngine
    from tests.helpers import with_params

    run(seed_config(sqlite_db))
    monkeypatch.setattr(sr, "MIN_TODAY_CANDLES", 1)
    monkeypatch.setattr(sr, "atr_floor", lambda price: 0.0)
    runner = sr.StrategyRunner()
    engine = TrendFollowingEngine("NIFTY")
    runner._strategies["NIFTY"] = [engine]

    async def _merge(underlying, candles, indicators, is_expiry_day, flags):
        return {n: True for n in ALL_STRATEGY_NAMES}, None

    monkeypatch.setattr(runner, "_detect_and_merge_regime", _merge)
    state = {"rc": with_params(replace(runtime_ctx, instruments=_NoInstruments()),
                               decision_engine="UNIFIED")}

    def feed(i):
        fake_redis.kv[f"ohlc:{settings.NIFTY_TOKEN}:ONE_MINUTE"] = [_c(k) for k in range(i + 1)]
        runtime_ctx.clock.set((START + timedelta(minutes=i + 1, seconds=3)).replace(tzinfo=IST))

    with using(state["rc"]):
        yield runner, engine, feed, state


def _rows(sqlite_db):
    async def _q():
        async with sqlite_db() as db:
            return (await db.execute(select(TrendDecisionRecord)
                                     .order_by(TrendDecisionRecord.candle_ts))).scalars().all()
    return run(_q())


def test_every_candle_is_recorded_once_with_forward_prices(engine_runner, sqlite_db):
    runner, engine, feed, _ = engine_runner
    for i in range(80):
        feed(i)
        run(runner.run_cycle("NIFTY"))
    run(runner.run_cycle("NIFTY"))                     # same candle again: no duplicate row
    rows = _rows(sqlite_db)
    assert len(rows) == 80
    first = rows[0]
    assert first.decision in ("BUY CE", "BUY PE", "NO TRADE")
    assert first.engine_evaluated is True and first.gate is None
    assert first.reason_code is not None              # early candles: NO TRADE with a code
    assert first.spot == pytest.approx(22601.0)
    # the 09:15 decision: +15 / +30 / +60 closes are filled from later candles
    assert first.px_15 == pytest.approx(22616.0)
    assert first.px_30 == pytest.approx(22631.0)
    assert first.px_60 == pytest.approx(22661.0)
    assert first.next_open == pytest.approx(22601.0) and first.fwd_complete is True
    scored = [r for r in rows if r.score is not None]
    assert scored and all(r.features and r.direction in (1, -1) for r in scored)
    late = rows[-1]
    assert late.px_15 is None and late.fwd_complete is False


def test_gated_candles_still_record_the_engine_decision(engine_runner, sqlite_db, monkeypatch):
    from app.services.trading import strategy_runner as sr

    runner, engine, feed, _ = engine_runner
    monkeypatch.setattr(sr, "atr_floor", lambda price: 1e9)     # ATR gate always fires
    for i in range(40):
        feed(i)
        run(runner.run_cycle("NIFTY"))
    rows = _rows(sqlite_db)
    assert len(rows) == 40
    assert {r.gate for r in rows} == {"ATR_BELOW_FLOOR"}
    assert all(r.engine_evaluated is False and r.signal_emitted is False for r in rows)
    assert all(r.decision in ("BUY CE", "BUY PE", "NO TRADE") for r in rows)


def test_legacy_mode_records_the_engine_as_research_only(engine_runner, sqlite_db):
    from app.runtime.context import using
    from tests.helpers import with_params

    runner, engine, feed, state = engine_runner
    with using(with_params(state["rc"], decision_engine="LEGACY")):
        for i in range(30):
            feed(i)
            run(runner.run_cycle("NIFTY"))
    rows = _rows(sqlite_db)
    assert len(rows) == 30 and {r.gate for r in rows} == {"LEGACY_MODE"}


def test_telemetry_failure_never_stops_the_cycle(engine_runner, sqlite_db, monkeypatch, caplog):
    runner, engine, feed, _ = engine_runner

    def _boom(*a, **k):
        raise RuntimeError("db down")

    monkeypatch.setattr(TrendDecisionRecord, "__init__", _boom)
    feed(30)
    run(runner.run_cycle("NIFTY"))                     # must not raise
    assert _rows(sqlite_db) == []
    assert any("telemetry not recorded" in r.getMessage() for r in caplog.records)


def test_table_is_created_on_first_use(runtime_ctx, tmp_path, monkeypatch):
    """A database that has not run migration 0014 gets the table on first write."""
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.runtime.context import using
    from app.strategies.trend_engine import TrendFollowingEngine

    eng = create_async_engine(f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}", poolclass=NullPool)
    Session = sessionmaker(eng, class_=AsyncSession, expire_on_commit=False)
    runtime_ctx.clock.set(datetime(2026, 9, 1, 10, 0, 3, tzinfo=IST))
    with using(replace(runtime_ctx, sessions=Session)):
        rid = run(decision_telemetry.record(
            underlying="NIFTY", engine=TrendFollowingEngine("NIFTY"),
            candles=[_c(i) for i in range(45)], indicators={"atr": 5.0}, gate=None))
    assert rid is not None
