"""Fix 5 (B5): regime_states / regime_performance written through real sessions."""
import gc
import warnings

from sqlalchemy import func, select

from app.db.models.paper_trade import DailyPnLSummary
from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
from app.services.trading import regime_detector as rd_mod
from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from app.services.trading.strategy_runner import strategy_runner
from tests.helpers import add_position, get_position, run, seed_config
from tests.test_regime_state_machine import _ind


def _stub_classifier(monkeypatch, regime="BULL_TREND_NORMAL_VOL"):
    monkeypatch.setattr(rd_mod, "classify_regime", lambda *a, **k: regime)
    monkeypatch.setattr(rd_mod, "build_regime_indicators", lambda *a, **k: _ind())


async def _count(Session, model):
    async with Session() as db:
        return (await db.execute(select(func.count()).select_from(model))).scalar()


def test_detect_and_merge_persists_regime_state(sqlite_db, monkeypatch):
    _stub_classifier(monkeypatch)
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        flags, state = run(strategy_runner._detect_and_merge_regime(
            "NIFTY", [], {}, False, {"VWAP_PULLBACK": True},
        ))
        gc.collect()
    assert state.regime == "BULL_TREND_NORMAL_VOL"
    assert flags["VWAP_PULLBACK"] is True
    assert run(_count(sqlite_db, RegimeStateModel)) == 1
    never_awaited = [w for w in caught if issubclass(w.category, RuntimeWarning)
                     and "never awaited" in str(w.message)]
    assert never_awaited == []


def test_detect_and_merge_survives_commit_failure(monkeypatch):
    _stub_classifier(monkeypatch)

    class _BadSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        def add(self, obj):
            pass

        async def flush(self):
            pass

        async def commit(self):
            raise RuntimeError("db write failed")

    import app.db.base as base
    monkeypatch.setattr(base, "AsyncSessionLocal", lambda: _BadSession())
    flags, state = run(strategy_runner._detect_and_merge_regime("NIFTY", [], {}, False, {}))
    assert state is not None
    assert state.regime == "BULL_TREND_NORMAL_VOL"


def test_close_position_records_regime_performance(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, entry_regime="BULL_TREND_NORMAL_VOL"))
    run(paper_engine._close_position(pid, 84.0, "SL"))

    async def _perf():
        async with sqlite_db() as db:
            return (await db.execute(select(RegimePerformanceModel))).scalars().all()

    rows = run(_perf())
    assert len(rows) == 1
    assert rows[0].total_trades == 1
    assert rows[0].losing_trades == 1
    assert run(_count(sqlite_db, DailyPnLSummary)) == 1


def test_close_position_continues_when_regime_write_fails(sqlite_db, fake_redis, monkeypatch):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, entry_regime="BULL_TREND_NORMAL_VOL"))

    async def _boom(**kwargs):
        raise RuntimeError("regime perf write failed")

    monkeypatch.setattr(strategy_orchestrator, "record_trade_result", _boom)
    run(paper_engine._close_position(pid, 84.0, "SL"))
    assert run(get_position(sqlite_db, pid)).is_open is False
    assert len(fake_redis.events("POSITION_CLOSED")) == 1
