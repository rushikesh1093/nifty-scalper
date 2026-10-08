"""
Live implementations of the runtime ports (design §14.1) and ``live_context()``.

This is the one place the live process reads the wall clock for decisions
(LiveClock) and the one place trading code's Redis access is routed through
(RedisMarketData). app/runtime/ is outside the §14.2 clock scan for that
reason.
"""
from __future__ import annotations

import asyncio
import dataclasses
import logging
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Callable, Optional

from sqlalchemy import select

from app.core.trading_params import TradingParams
from app.runtime.context import RunConfig, RuntimeContext

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))
LIVE_RUN_ID = "PAPER"

# trading_config columns that overlay TradingParams defaults, computed from the
# ORM. Since migration 0013 trade_amount is a nullable display cache (NULL =
# derived from fixed_fraction_pct × capital), so it overlays like any other
# column: a stored value that diverges by more than ₹500 fails check 6.
def _overlay_columns() -> tuple[str, ...]:
    from app.db.models.paper_trade import TradingConfig
    cols = {c.key for c in TradingConfig.__table__.columns}
    return tuple(sorted(cols & TradingParams.field_names()))


def params_from_row(row) -> TradingParams:
    """TradingParams defaults overlaid by the row's same-named, non-null columns."""
    overrides = {}
    for k in _overlay_columns():
        v = getattr(row, k, None)
        if v is not None:
            overrides[k] = v
    return dataclasses.replace(TradingParams(), **overrides)


def run_config_from(row) -> RunConfig:
    """Freeze the run's capital and caps from the trading_config row.
    Only called after the paper preflight has proven the row's mode parseable."""
    defaults = TradingParams()
    mode = getattr(row.mode, "value", row.mode)
    return RunConfig(
        mode=str(mode),
        capital=float(row.capital),
        max_trades_per_day=int(row.max_trades_per_day),
        max_open_positions=int(row.max_open_positions),
        max_lots_per_trade=int(row.max_lots_per_trade),
        max_daily_loss=float(row.max_daily_loss),
        max_daily_loss_pct=float(row.max_daily_loss_pct),
        open_exposure_cap_pct=float(getattr(row, "open_exposure_cap_pct", None)
                                    or defaults.open_exposure_cap_pct),
        square_off_time=str(row.square_off_time or "15:15"),
        entry_cutoff_buffer_min=int(getattr(row, "entry_cutoff_buffer_min", None)
                                    or defaults.entry_cutoff_buffer_min),
        user_strategy_flags=dict(row.strategy_flags or {}),
    )


# ── Clock ────────────────────────────────────────────────────────────────────

class LiveClock:
    def now_utc(self) -> datetime:
        return datetime.now(timezone.utc)

    def now_ist(self) -> datetime:
        return self.now_utc().astimezone(IST)

    def trading_day(self) -> date:
        return self.now_ist().date()


# ── Market data ──────────────────────────────────────────────────────────────

class RedisMarketData:
    """Pass-through to redis_cache. Attribute lookup happens per call, so a
    patched cache (tests) is always the one used. ``set``/``get`` own all JSON
    (de)serialisation: callers pass and receive Python objects."""

    def __init__(self, cache) -> None:
        self._cache = cache

    async def get_ltp(self, token: str) -> Optional[float]:
        return await self._cache.get_ltp(token)

    async def get_tick(self, token: str) -> Optional[dict]:
        return await self._cache.get_tick(token)

    async def get_candles(self, token: str, interval: str, count: int = 100) -> list[dict]:
        return await self._cache.get_candles(token, interval, count)

    async def get_option_chain(self, underlying: str, expiry: str) -> Optional[dict]:
        return await self._cache.get_option_chain(underlying, expiry)

    async def get(self, key: str) -> Optional[Any]:
        return await self._cache.get(key)

    async def set(self, key: str, value: Any, ttl: int = 60) -> None:
        await self._cache.set(key, value, ttl=ttl)

    async def delete(self, key: str) -> None:
        await self._cache.delete(key)

    async def publish_signal(self, strategy: str, signal: dict) -> None:
        await self._cache.publish_signal(strategy, signal)


# ── TTL-cached DB reads (params, user flags) ─────────────────────────────────

class _TtlRowValue:
    """A value derived from the trading_config row, refreshed in the background
    once older than ``ttl_s``. ``current()`` never blocks and never raises: it
    returns the last good value and schedules one refresh task if stale."""

    def __init__(self, session_factory: Callable, derive: Callable, initial, ttl_s: float) -> None:
        self._sessions = session_factory
        self._derive = derive
        self._value = initial
        self._ttl_s = ttl_s
        self._loaded_at = _time.monotonic()
        self._task: Optional[asyncio.Task] = None

    @property
    def age_s(self) -> float:
        return _time.monotonic() - self._loaded_at

    def current(self):
        if self.age_s >= self._ttl_s and (self._task is None or self._task.done()):
            try:
                loop = asyncio.get_running_loop()
            except RuntimeError:
                return self._value
            self._task = loop.create_task(self.refresh())
        return self._value

    async def refresh(self) -> None:
        from app.db.models.paper_trade import TradingConfig
        try:
            async with self._sessions() as db:
                row = (await db.execute(
                    select(TradingConfig).where(TradingConfig.id == 1)
                )).scalar_one_or_none()
            if row is not None:
                self._value = self._derive(row)
            self._loaded_at = _time.monotonic()
        except Exception as exc:
            logger.warning("trading_config refresh failed (keeping last value): %s", exc)
            self._loaded_at = _time.monotonic()


class DbTradingParams:
    """ParamsPort: code defaults overlaid by trading_config, TTL-cached
    (params_cache_ttl_s). An operator edit takes effect within the TTL."""

    def __init__(self, session_factory: Callable, initial_row=None) -> None:
        initial = params_from_row(initial_row) if initial_row is not None else TradingParams()
        self._cache = _TtlRowValue(session_factory, params_from_row, initial,
                                   ttl_s=initial.params_cache_ttl_s)

    def current(self) -> TradingParams:
        return self._cache.current()

    @property
    def cache_age_s(self) -> float:
        return self._cache.age_s


class DbUserFlags:
    """UserFlagsPort: TTL-cached trading_config.strategy_flags (R4 finding 8)."""

    def __init__(self, session_factory: Callable, initial_row=None, ttl_s: float = 30) -> None:
        def _flags(row) -> dict[str, bool]:
            return dict(row.strategy_flags or {})
        initial = _flags(initial_row) if initial_row is not None else {}
        self._cache = _TtlRowValue(session_factory, _flags, initial, ttl_s=ttl_s)

    def current(self) -> dict[str, bool]:
        return dict(self._cache.current())


class FrozenUserFlags:
    """UserFlagsPort over a fixed snapshot (replay, tests)."""

    def __init__(self, flags: Optional[dict[str, bool]] = None) -> None:
        self._flags = dict(flags or {})

    def current(self) -> dict[str, bool]:
        return dict(self._flags)


class StaticParams:
    """ParamsPort over a fixed TradingParams (replay, tests)."""

    def __init__(self, params: Optional[TradingParams] = None) -> None:
        self._params = params or TradingParams()

    def current(self) -> TradingParams:
        return self._params


# ── Instruments ──────────────────────────────────────────────────────────────

class LiveInstruments:
    """InstrumentPort: active OPTIDX rows with expiry >= the ctx trading day,
    all expiries, cached per (underlying, trading_day). Rows are expunged from
    the caller's session so a later commit there cannot expire them."""

    def __init__(self) -> None:
        self._cache: dict[tuple[str, date], list] = {}

    async def instruments_for(self, db, underlying: str) -> list:
        from app.db.models.instrument import Instrument, InstrumentType
        from app.runtime.context import trading_day

        as_of = trading_day()
        key = (underlying.upper(), as_of)
        hit = self._cache.get(key)
        if hit is not None:
            return list(hit)
        rows = (await db.execute(
            select(Instrument).where(
                Instrument.name == underlying.upper(),
                Instrument.instrument_type == InstrumentType.OPTIDX,
                Instrument.is_active == True,  # noqa: E712
                Instrument.expiry >= as_of,
            ).order_by(Instrument.expiry, Instrument.strike)
        )).scalars().all()
        for r in rows:
            db.expunge(r)
        if rows:                                   # never cache an empty universe
            self._cache[key] = list(rows)
        return list(rows)

    def invalidate(self) -> None:
        self._cache.clear()


# ── Regime config ────────────────────────────────────────────────────────────

class DbRegimeConfig:
    """RegimeConfigPort over the existing TTL cache + loader (no-session → defaults)."""

    def for_underlying(self, underlying: str):
        from app.services.trading.regime_config import reload_config_if_stale
        return reload_config_if_stale(underlying, db_session=None)


# ── script_context / live_context ────────────────────────────────────────────

def script_context(session_factory: Optional[Callable] = None) -> RuntimeContext:
    """Live-shaped PAPER context for operator scripts that drive trading code
    directly: wall clock, redis_cache, TradingParams defaults, and no DB read at
    build time. Scripts bind it explicitly; nothing falls back to it."""
    from app.services.cache.redis_client import redis_cache
    if session_factory is None:
        from app.db.base import AsyncSessionLocal as session_factory
    p = TradingParams()
    return RuntimeContext(
        run_id=LIVE_RUN_ID,
        clock=LiveClock(),
        market=RedisMarketData(redis_cache),
        sessions=session_factory,
        regime_config=DbRegimeConfig(),
        params=StaticParams(p),
        cfg=RunConfig(
            mode="PAPER", capital=p.capital, max_trades_per_day=p.max_trades_per_day,
            max_open_positions=p.max_open_positions, max_lots_per_trade=p.max_lots_per_trade,
            max_daily_loss=p.max_daily_loss, max_daily_loss_pct=p.max_daily_loss_pct,
            open_exposure_cap_pct=p.open_exposure_cap_pct, square_off_time="15:15",
            entry_cutoff_buffer_min=p.entry_cutoff_buffer_min,
        ),
        user_flags=FrozenUserFlags({}),
        instruments=LiveInstruments(),
        is_replay=False,
    )


def live_context(row, session_factory: Callable) -> RuntimeContext:
    """The live paper-trading context. *row* is the trading_config row that
    ensure_config returned, read after the paper preflight passed."""
    from app.services.cache.redis_client import redis_cache

    params = DbTradingParams(session_factory, initial_row=row)
    return RuntimeContext(
        run_id=LIVE_RUN_ID,
        clock=LiveClock(),
        market=RedisMarketData(redis_cache),
        sessions=session_factory,
        regime_config=DbRegimeConfig(),
        params=params,
        cfg=run_config_from(row),
        user_flags=DbUserFlags(session_factory, initial_row=row,
                               ttl_s=params.current().params_cache_ttl_s),
        instruments=LiveInstruments(),
        is_replay=False,
    )
