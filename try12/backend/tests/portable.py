"""Self-contained SQLite + Redis stand-ins for the few tests that must also run
against master (fail-before evidence for bugs the baseline merge partly fixed).

Only names that exist on both master and this branch are imported, and every
optional column / table is included only when the model declares it.
"""
import asyncio
import sys
from datetime import datetime, timezone

from sqlalchemy import BigInteger, create_engine
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.ext.compiler import compiles
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import NullPool


@compiles(BigInteger, "sqlite")
def _bigint_as_integer_on_sqlite(type_, compiler, **kw):
    return "INTEGER"


def run(coro):
    return asyncio.run(coro)


def _models():
    from app.db.models.paper_trade import (
        DailyPnLSummary, PaperPosition, StrategyPerf, StrategySignal, TradingConfig,
    )
    from app.db.models.regime import RegimePerformanceModel, RegimeStateModel
    models = [TradingConfig, PaperPosition, StrategySignal, DailyPnLSummary, StrategyPerf,
              RegimeStateModel, RegimePerformanceModel]
    try:
        from app.db.models.risk import CycleStats, NoTradeDecision, PositionEvent, RiskHalt
        models += [NoTradeDecision, RiskHalt, PositionEvent, CycleStats]
    except ImportError:            # master has no risk tables
        pass
    return models


def make_sqlite(tmp_path, monkeypatch):
    db_file = tmp_path / "portable.db"
    sync_engine = create_engine(f"sqlite:///{db_file}")
    from app.db.base import Base
    Base.metadata.create_all(sync_engine, tables=[m.__table__ for m in _models()])
    sync_engine.dispose()
    engine = create_async_engine(f"sqlite+aiosqlite:///{db_file}", poolclass=NullPool)
    Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    for name, mod in list(sys.modules.items()):
        if (name == "app" or name.startswith("app.")) and mod is not None \
                and "AsyncSessionLocal" in getattr(mod, "__dict__", {}):
            monkeypatch.setattr(mod, "AsyncSessionLocal", Session)
    return Session


def _only_columns(model, fields: dict) -> dict:
    cols = set(model.__table__.c.keys())
    return {k: v for k, v in fields.items() if k in cols}


async def _add(Session, obj):
    async with Session() as db:
        db.add(obj)
        await db.commit()
        await db.refresh(obj)
        return obj


def seed_config(Session, **overrides):
    from app.db.models.paper_trade import TradingConfig, TradingMode
    fields = dict(id=1, mode=TradingMode.PAPER, capital=100000.0, trade_amount=10000.0,
                  max_lots_per_trade=3, max_open_positions=3, max_daily_loss=3000.0,
                  max_daily_loss_pct=3.0, slippage_pct=0.2, square_off_time="15:15",
                  study_phase=False, used_capital=0.0, daily_trades_count=0,
                  max_trades_per_day=6, live_mode_confirmed=False, strategy_flags={})
    fields.update(overrides)
    run(_add(Session, TradingConfig(**_only_columns(TradingConfig, fields))))


def add_position(Session, **overrides) -> int:
    from app.db.models.paper_trade import PaperPosition, SignalDirection, TradingMode
    fields = dict(
        strategy_name="VWAP_EMA_PULLBACK", token="OPT1", symbol="NIFTY06OCT2622600CE",
        underlying="NIFTY", strike=22600.0, option_type="CE", direction=SignalDirection.BUY,
        lots=1, lot_size=65, quantity=65, entry_price=100.0, entry_slippage=0.0,
        actual_entry=100.0, stop_loss=85.0, initial_stop_loss=85.0, target=130.0,
        brokerage=20.0, statutory_charges=0.0, trade_amount_used=6500.0, is_open=True,
        partial_closed=False, mode=TradingMode.PAPER, entry_time=datetime.now(timezone.utc),
        initial_risk=15.0, r_source="ENTRY", original_quantity=65, original_lots=1,
    )
    fields.update(overrides)
    return run(_add(Session, PaperPosition(**_only_columns(PaperPosition, fields)))).id


def get_position(Session, pos_id):
    from sqlalchemy import select
    from app.db.models.paper_trade import PaperPosition

    async def _get():
        async with Session() as db:
            return (await db.execute(
                select(PaperPosition).where(PaperPosition.id == pos_id))).scalar_one()
    return run(_get())


class PortableRedis:
    """Patches the redis_cache methods both trees read through."""

    def __init__(self, monkeypatch):
        from app.services.cache.redis_client import redis_cache
        self.kv = {}
        self.published = []
        for name in ("get_ltp", "get_tick", "get", "set", "delete", "publish_signal"):
            monkeypatch.setattr(redis_cache, name, getattr(self, name))

    def put_tick(self, token, ltp, bid=0.0, ask=0.0):
        self.kv[f"ltp:{token}"] = float(ltp)
        self.kv[f"tick:{token}"] = {
            "token": token, "ltp": ltp, "open": ltp, "best_bid": bid, "best_ask": ask,
            "ts": datetime.now(timezone.utc).replace(tzinfo=None).isoformat(),
        }

    async def get_ltp(self, token):
        return self.kv.get(f"ltp:{token}")

    async def get_tick(self, token):
        return dict(self.kv[f"tick:{token}"]) if f"tick:{token}" in self.kv else None

    async def get(self, key):
        return self.kv.get(key)

    async def set(self, key, value, ttl=60):
        self.kv[key] = value

    async def delete(self, key):
        self.kv.pop(key, None)

    async def publish_signal(self, channel, payload):
        self.published.append((channel, payload))
