"""Shared fakes and builders for the test suite (no network, no real DB/Redis)."""
import asyncio
import copy
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace
from typing import Any, Optional

from app.db.models.paper_trade import (
    PaperPosition, SignalDirection, TradingConfig, TradingMode,
)
from app.strategies.base import Direction, SignalEvent

CFG_DEFAULTS: dict = dict(
    id=1,
    mode=TradingMode.PAPER,
    capital=10000.0,
    trade_amount=3000.0,
    max_lots_per_trade=1,
    max_open_positions=2,
    max_daily_loss=500.0,
    max_daily_loss_pct=5.0,
    slippage_pct=0.2,
    square_off_time="15:15",
    study_phase=False,
    used_capital=0.0,
    daily_trades_count=0,
    max_trades_per_day=10,
    live_mode_confirmed=False,
    strategy_flags={},
)


def run(coro):
    return asyncio.run(coro)


def make_cfg(**overrides) -> SimpleNamespace:
    """TradingConfig-like object for code paths that only read attributes."""
    base = dict(CFG_DEFAULTS, trading_halted=False, halted_reason=None,
                halted_at=None, daily_trades_date=None)
    base.update(overrides)
    return SimpleNamespace(**base)


async def seed_config(Session, **overrides) -> None:
    async with Session() as db:
        db.add(TradingConfig(**dict(CFG_DEFAULTS, **overrides)))
        await db.commit()


async def add_position(Session, **overrides) -> int:
    fields = dict(
        strategy_name="VWAP_PULLBACK",
        token="OPT1",
        symbol="NIFTY06OCT2622600CE",
        underlying="NIFTY",
        strike=22600.0,
        option_type="CE",
        direction=SignalDirection.BUY,
        lots=1,
        lot_size=65,
        quantity=65,
        entry_price=100.0,
        entry_slippage=0.0,
        actual_entry=100.0,
        stop_loss=85.0,
        initial_stop_loss=85.0,
        target=130.0,
        brokerage=20.0,
        statutory_charges=0.0,
        trade_amount_used=6500.0,
        is_open=True,
        partial_closed=False,
        mode=TradingMode.PAPER,
        entry_time=datetime.now(timezone.utc),
    )
    fields.update(overrides)
    async with Session() as db:
        pos = PaperPosition(**fields)
        db.add(pos)
        await db.commit()
        await db.refresh(pos)
        return pos.id


async def get_position(Session, pos_id: int) -> PaperPosition:
    from sqlalchemy import select
    async with Session() as db:
        return (await db.execute(
            select(PaperPosition).where(PaperPosition.id == pos_id)
        )).scalar_one()


async def get_config_row(Session) -> TradingConfig:
    from sqlalchemy import select
    async with Session() as db:
        return (await db.execute(
            select(TradingConfig).where(TradingConfig.id == 1)
        )).scalar_one()


def make_signal(strategy="VWAP_PULLBACK", underlying="NIFTY", direction=Direction.BUY,
                sl_pct=0.15, target_pct=0.30, min_rr=2.0, atr=0.0) -> SignalEvent:
    return SignalEvent(
        strategy_name=strategy,
        underlying=underlying,
        direction=direction,
        spot_price=22600.0,
        signal_time=datetime.now(timezone.utc),
        vwap=22590.0,
        indicators={"atr": atr},
        sl_pct=sl_pct,
        target_pct=target_pct,
        min_rr=min_rr,
    )


def fake_candidate(ltp=40.0, lot_size=65, bid=0.0, ask=0.0, token="OPT1") -> SimpleNamespace:
    return SimpleNamespace(
        token=token, symbol="NIFTY06OCT2622600CE", strike=22600.0, option_type="CE",
        expiry=date(2026, 10, 13), ltp=ltp, bid=bid, ask=ask, lot_size=lot_size,
        delta_est=0.5,
    )


class FakeRedis:
    """In-memory stand-in for the redis_cache methods the trading code uses."""

    def __init__(self):
        self.kv: dict[str, Any] = {}
        self.published: list[tuple[str, dict]] = []
        self.fail_ltp = False

    # helpers for tests
    def put_tick(self, token: str, ltp: Optional[float], bid: float = 0.0, ask: float = 0.0,
                 age_s: float = 0.0, open_: Optional[float] = None) -> None:
        ts = (datetime.now(timezone.utc) - timedelta(seconds=age_s)).replace(tzinfo=None)
        if ltp is None:
            self.kv.pop(f"ltp:{token}", None)
        else:
            self.kv[f"ltp:{token}"] = float(ltp)
        self.kv[f"tick:{token}"] = {
            "token": token, "ltp": ltp or 0.0, "open": open_ if open_ is not None else (ltp or 0.0),
            "best_bid": bid, "best_ask": ask, "ts": ts.isoformat(),
        }

    def drop_price(self, token: str) -> None:
        self.kv.pop(f"ltp:{token}", None)
        self.kv.pop(f"tick:{token}", None)

    # redis_cache API
    async def get_ltp(self, token):
        if self.fail_ltp:
            raise RuntimeError("redis down")
        v = self.kv.get(f"ltp:{token}")
        return float(v) if v is not None else None

    async def set_ltp(self, token, ltp):
        self.kv[f"ltp:{token}"] = float(ltp)

    async def get_multiple_ltp(self, tokens):
        return {t: await self.get_ltp(t) for t in tokens}

    async def get_tick(self, token):
        v = self.kv.get(f"tick:{token}")
        return copy.deepcopy(v) if v is not None else None

    async def set_tick(self, token, tick_data):
        self.kv[f"tick:{token}"] = copy.deepcopy(tick_data)

    async def get(self, key):
        v = self.kv.get(key)
        return copy.deepcopy(v) if v is not None else None

    async def set(self, key, value, ttl=60):
        self.kv[key] = copy.deepcopy(value)

    async def delete(self, key):
        self.kv.pop(key, None)

    async def publish_signal(self, strategy, signal):
        self.published.append((strategy, copy.deepcopy(signal)))

    async def get_candles(self, token, interval, count=100):
        return list(self.kv.get(f"ohlc:{token}:{interval}", []))[-count:]

    async def get_option_chain(self, underlying, expiry):
        return None

    async def health(self):
        return True

    def events(self, name: str) -> list[dict]:
        return [s for _, s in self.published if s.get("event") == name]
