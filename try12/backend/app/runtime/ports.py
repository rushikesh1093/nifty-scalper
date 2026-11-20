"""
Ports — the complete surface trading code may use for time, market data,
configuration and the instrument universe (design §14.1).

After the §14.2 conversion no module under app/services/trading/ or
app/strategies/ references ``redis_cache`` or ``._r(`` at all; MarketDataPort
is the whole cache surface (tests/test_market_port_scan.py).
"""
from __future__ import annotations

from datetime import date, datetime
from typing import TYPE_CHECKING, Any, Optional, Protocol

if TYPE_CHECKING:
    from app.core.trading_params import TradingParams
    from app.db.models.instrument import Instrument
    from app.services.trading.regime_config import RegimeConfig


class Clock(Protocol):
    def now_utc(self) -> datetime: ...
    def now_ist(self) -> datetime: ...
    def trading_day(self) -> date: ...


class MarketDataPort(Protocol):           # THE COMPLETE cache surface for trading code
    async def get_ltp(self, token: str) -> float | None: ...
    async def get_tick(self, token: str) -> dict | None: ...
    async def get_candles(self, token: str, interval: str, count: int) -> list[dict]: ...
    async def get_option_chain(self, underlying: str, expiry: str) -> dict | None: ...
    async def get(self, key: str) -> Any | None: ...
    async def set(self, key: str, value: Any, ttl: int = 60) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def publish_signal(self, strategy: str, signal: dict) -> None: ...   # matches redis_client
    # Optional, live only (RedisMarketData): get_daily_candles(token, day) -> the
    # broker's ONE_DAY rows before *day*, or None. The runner reads it with getattr,
    # so the replay port, which has none, never reaches the broker.


class RegimeConfigPort(Protocol):
    def for_underlying(self, underlying: str) -> "RegimeConfig": ...


class ParamsPort(Protocol):
    def current(self) -> "TradingParams": ...      # live: TTL-cached DB overlay. replay: frozen


class UserFlagsPort(Protocol):
    def current(self) -> dict[str, bool]: ...      # live: TTL-cached strategy_flags. replay: frozen


class RunControlPort(Protocol):
    """The operator run controls a decision reads: the persisted kill switch,
    study_phase and square_off_time. Live: the trading_config row, read at
    every call, so an operator toggle applies at once (today's semantics).
    Replay: the frozen RunConfig, never the shared row (review R1)."""
    async def persisted_halt(self) -> tuple[bool, str]: ...
    async def study_phase(self) -> bool: ...
    async def square_off_time(self) -> str | None: ...


class InstrumentPort(Protocol):                    # §10.4: universe resolution, no wall clock
    async def instruments_for(self, db, underlying: str) -> list["Instrument"]: ...
    def invalidate(self) -> None: ...
    # Nearest option expiry on or after the trading day, from the instrument
    # MASTER (not only contracts that traded): what "is today an expiry day"
    # must be answered from. Optional: the runner falls back to instruments_for.
    async def nearest_expiry(self, db, underlying: str) -> Optional["date"]: ...
