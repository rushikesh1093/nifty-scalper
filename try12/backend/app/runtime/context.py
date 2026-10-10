"""
RuntimeContext and its accessors (design §14.1).

``ctx()`` deliberately has no default: an unbound read raises LookupError
instead of silently falling back to the wall clock or the live database.

Who sees the binding: tasks created inside the binding task after ``bind()``
inherit it (the lifespan's monitor/candle/feed tasks). HTTP and websocket
request tasks do not; RuntimeContextMiddleware binds the same object for
them from ``app.state.runtime_context`` (design §6.6).
"""
from __future__ import annotations

from contextlib import contextmanager
from contextvars import ContextVar, Token
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import TYPE_CHECKING, Callable, Iterator, Optional

if TYPE_CHECKING:
    from sqlalchemy.ext.asyncio import AsyncSession

    from app.core.trading_params import TradingParams
    from app.runtime.ports import (
        Clock, InstrumentPort, MarketDataPort, ParamsPort, RegimeConfigPort, RunControlPort,
        UserFlagsPort,
    )


@dataclass(frozen=True)
class RunConfig:
    """Every operator-owned number a decision may read that must NOT drift
    mid-run, resolved once (live: from the trading_config row after the paper
    preflight; replay: from --params-file). Never a TradingConfig ORM row."""
    mode: str                                 # always "PAPER"; read by gate check 1
    capital: float
    max_trades_per_day: int
    max_open_positions: int
    max_lots_per_trade: int
    max_daily_loss: float
    max_daily_loss_pct: float
    open_exposure_cap_pct: float
    square_off_time: str                      # "HH:MM" IST
    entry_cutoff_buffer_min: int
    user_strategy_flags: dict[str, bool] = field(default_factory=dict)
    underlyings: tuple[str, ...] = ("NIFTY", "BANKNIFTY")
    # Run controls read through ctx().control. Replay's FrozenRunControl serves
    # these (and square_off_time) from --params-file, so they are in params_hash.
    # Live's DbRunControl reads the row at every call and ignores them.
    study_phase: bool = False
    trading_halted: bool = False


@dataclass(frozen=True)
class RuntimeContext:
    run_id: str
    clock: "Clock"
    market: "MarketDataPort"
    sessions: Callable[[], "AsyncSession"]    # async context manager factory
    regime_config: "RegimeConfigPort"
    params: "ParamsPort"                      # provider, not a value
    cfg: RunConfig
    user_flags: "UserFlagsPort"
    instruments: "InstrumentPort"
    control: "RunControlPort"                 # kill switch, study_phase, square_off_time
    is_replay: bool


_ctx: ContextVar[Optional[RuntimeContext]] = ContextVar("runtime_context")


def ctx() -> RuntimeContext:
    """The bound context. Raises LookupError if none is bound — no silent default."""
    rc = _ctx.get(None)
    if rc is None:
        raise LookupError("RuntimeContext is not bound in this task")
    return rc


def is_bound() -> bool:
    """Used by RuntimeContextMiddleware (design §6.6)."""
    return _ctx.get(None) is not None


def bind(context: RuntimeContext) -> Token:
    return _ctx.set(context)


def unbind(token: Token) -> None:
    _ctx.reset(token)


@contextmanager
def using(context: Optional[RuntimeContext]) -> Iterator[Optional[RuntimeContext]]:
    """Bind *context* for the duration of the block (None = explicitly unbound)."""
    token = _ctx.set(context)
    try:
        yield context
    finally:
        _ctx.reset(token)


# ── convenience accessors used at call sites ─────────────────────────────────

def now_utc() -> datetime:
    return ctx().clock.now_utc()


def now_ist() -> datetime:
    return ctx().clock.now_ist()


def trading_day() -> date:
    return ctx().clock.trading_day()


def market() -> "MarketDataPort":
    return ctx().market


def sessions() -> Callable[[], "AsyncSession"]:
    """The session factory; call sites use ``async with sessions()() as db``."""
    return ctx().sessions


def run_id() -> str:
    return ctx().run_id


def params() -> "TradingParams":
    return ctx().params.current()        # resolves through the port
