"""
Test isolation. This block MUST run before any ``app`` import.

OS environment variables override .env in pydantic-settings, so pointing
Postgres/Redis at port 1 guarantees an accidental connection fails fast
instead of reaching the production ``nifty_scalper`` DB or the live Redis.
Broker credentials are blanked so nothing can log in to AngelOne.
"""
import os
import sys

os.environ.update({
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "1",
    "POSTGRES_DB": "pytest_blocked",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "1",
    "ANGEL_API_KEY": "",
    "ANGEL_CLIENT_ID": "",
    "ANGEL_PASSWORD": "",
    "ANGEL_TOTP_SECRET": "",
})

import pytest  # noqa: E402
from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402


@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):
    # SQLite only autoincrements "INTEGER PRIMARY KEY"
    return "INTEGER"


# Import the modules whose AsyncSessionLocal binding we must patch.
import app.db.base  # noqa: E402,F401
import app.services.trading.mode_manager  # noqa: E402,F401
import app.services.trading.paper_engine  # noqa: E402,F401
import app.services.trading.strategy_runner  # noqa: E402,F401

from tests.helpers import FakeRedis  # noqa: E402


def _blocked_session(*args, **kwargs):
    raise RuntimeError("DB access blocked in tests")


def _patch_session_factory(monkeypatch, factory) -> None:
    """Replace AsyncSessionLocal in app.db.base and every loaded app module."""
    for name, mod in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and mod is not None \
                and "AsyncSessionLocal" in getattr(mod, "__dict__", {}):
            monkeypatch.setattr(mod, "AsyncSessionLocal", factory)


@pytest.fixture(autouse=True)
def _block_db(monkeypatch):
    _patch_session_factory(monkeypatch, _blocked_session)
    yield


@pytest.fixture(autouse=True)
def _reset_singletons():
    """Singletons keep in-memory state; reset it around every test."""
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.paper_engine import paper_engine
    from app.services.trading.regime_detector import regime_detector
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.strategies.base import signal_aggregator

    def _reset():
        mode_manager._halted = False
        paper_engine._last_known.clear()
        paper_engine._last_persist.clear()
        paper_engine._stale_warned.clear()
        paper_engine._square_off_cache = None
        regime_detector._regime_history.clear()
        regime_detector._last_log_time.clear()
        strategy_orchestrator._transition_cooldown.clear()
        strategy_orchestrator._current_regime.clear()
        signal_aggregator.reset()

    _reset()
    yield
    _reset()


@pytest.fixture
def sqlite_db(tmp_path, monkeypatch):
    """Throwaway SQLite DB (file under pytest tmp dir) wired in as AsyncSessionLocal."""
    pytest.importorskip("aiosqlite")
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.db.base import Base
    from app.db.models.paper_trade import (
        DailyPnLSummary, PaperPosition, StrategyPerf, StrategySignal, TradingConfig,
    )
    from app.db.models.regime import RegimePerformanceModel, RegimeStateModel

    db_file = tmp_path / "t.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    Base.metadata.create_all(
        sync_engine,
        tables=[m.__table__ for m in (
            TradingConfig, PaperPosition, StrategySignal, DailyPnLSummary,
            StrategyPerf, RegimeStateModel, RegimePerformanceModel,
        )],
    )
    sync_engine.dispose()

    async_engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Session = sessionmaker(async_engine, class_=AsyncSession, expire_on_commit=False)
    _patch_session_factory(monkeypatch, Session)
    yield Session


@pytest.fixture
def fake_redis(monkeypatch):
    """Replace redis_cache methods with an in-memory fake. redis_cache._redis stays None."""
    from app.services.cache.redis_client import redis_cache

    fake = FakeRedis()
    for name in ("get_ltp", "set_ltp", "get_multiple_ltp", "get_tick", "set_tick",
                 "get", "set", "delete", "publish_signal", "get_candles",
                 "get_option_chain", "health"):
        monkeypatch.setattr(redis_cache, name, getattr(fake, name))
    return fake
