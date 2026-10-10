"""
Subprocess entry point for tests/test_process_exit.py (not collected: no test_ prefix).

    python -m tests.startup_exit_app --db PATH --scenario {live_mode,live_confirmed,bad_params,ok}

Runs the REAL app.main:app under the REAL uvicorn against a throwaway SQLite
file seeded for the scenario, so the test observes the process exit code a
failed paper guard produces. Isolation is set BEFORE any app import: Postgres
and Redis point at port 1 and the broker credentials are blank, so nothing can
reach the live nifty_scalper DB, the live Redis or AngelOne. Feed, loop and
Redis connect/disconnect calls are stubbed in every scenario; the guard runs
before any of them.
"""
import os

os.environ.update({
    "POSTGRES_HOST": "127.0.0.1",
    "POSTGRES_PORT": "1",
    "POSTGRES_DB": "pytest_blocked",
    "REDIS_HOST": "127.0.0.1",
    "REDIS_PORT": "1",
    "REDIS_DB": "15",
    "ANGEL_API_KEY": "",
    "ANGEL_CLIENT_ID": "",
    "ANGEL_PASSWORD": "",
    "ANGEL_TOTP_SECRET": "",
})

import argparse  # noqa: E402
import asyncio  # noqa: E402
import sys  # noqa: E402

from sqlalchemy import BigInteger  # noqa: E402
from sqlalchemy.ext.compiler import compiles  # noqa: E402

SCENARIOS = ("live_mode", "live_confirmed", "bad_params", "ok")


@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


def _seed_overrides(scenario: str) -> dict:
    from app.db.models.paper_trade import TradingMode
    return {
        "live_mode": {"mode": TradingMode.LIVE},
        "live_confirmed": {"live_mode_confirmed": True},
        "bad_params": {"max_lots_per_trade": 1},
        "ok": {"max_lots_per_trade": 2, "max_daily_loss": 500.0},
    }[scenario]


def _build_db(path: str):
    """The conftest.sqlite_db schema on *path*; returns the async session factory."""
    from sqlalchemy import create_engine
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    from app.db.base import Base
    from app.db.models.option_chain import OptionChainSnapshot
    from app.db.models.paper_trade import (
        DailyPnLSummary, PaperPosition, StrategyPerf, StrategySignal, TradingConfig,
    )
    from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
    from app.db.models.replay import ReplayRun
    from app.db.models.risk import CycleStats, NoTradeDecision, PositionEvent, RiskHalt

    sync_engine = create_engine(f"sqlite:///{path}")
    Base.metadata.create_all(
        sync_engine,
        tables=[m.__table__ for m in (
            TradingConfig, PaperPosition, StrategySignal, DailyPnLSummary,
            StrategyPerf, RegimeStateModel, RegimePerformanceModel,
            NoTradeDecision, RiskHalt, PositionEvent, CycleStats, ReplayRun,
            OptionChainSnapshot,
        )],
    )
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{path}", poolclass=NullPool)
    return sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _patch_session_factory(factory) -> None:
    """Replace AsyncSessionLocal in every loaded app module (conftest's logic)."""
    for name, mod in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and mod is not None \
                and "AsyncSessionLocal" in getattr(mod, "__dict__", {}):
            setattr(mod, "AsyncSessionLocal", factory)


def _stub_side_effects() -> None:
    from app.services.cache.redis_client import redis_cache
    from app.services.market_data.market_manager import market_manager
    from app.services.market_data.study_collector import study_collector
    from app.services.trading.strategy_runner import strategy_runner

    async def _noop(*a, **k):
        return None

    for obj, names in ((redis_cache, ("connect", "disconnect")),
                       (market_manager, ("startup", "shutdown")),
                       (strategy_runner, ("start", "stop")),
                       (study_collector, ("start", "stop"))):
        for n in names:
            setattr(obj, n, _noop)


def _serve_until_started(app) -> int:
    """Positive control: a real uvicorn.Server that shuts itself down once started."""
    import uvicorn

    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=0, lifespan="on",
                                           log_level="info"))

    async def _main():
        async def _stop_when_started():
            while not server.started:
                await asyncio.sleep(0.05)
            server.should_exit = True
        stopper = asyncio.create_task(_stop_when_started())
        try:
            await server.serve()
        finally:
            stopper.cancel()

    asyncio.run(_main())
    return 0 if server.started else 3


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", required=True)
    ap.add_argument("--scenario", required=True, choices=SCENARIOS)
    a = ap.parse_args(argv)

    import app.main  # noqa: F401  (loads every module whose AsyncSessionLocal is patched)
    from tests.helpers import seed_config

    Session = _build_db(a.db)
    asyncio.run(seed_config(Session, **_seed_overrides(a.scenario)))
    _patch_session_factory(Session)
    _stub_side_effects()

    if a.scenario == "ok":
        return _serve_until_started(app.main.app)
    import uvicorn
    uvicorn.run(app.main.app, host="127.0.0.1", port=0, lifespan="on", log_level="info")
    return 0                       # only reached if uvicorn returned instead of exiting


if __name__ == "__main__":
    sys.exit(main())
