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

IST = timezone(timedelta(hours=5, minutes=30))

CFG_DEFAULTS: dict = dict(
    id=1,
    mode=TradingMode.PAPER,
    capital=10000.0,
    # Nullable display cache since migration 0013 (design §9.5): None means
    # "derive from fixed_fraction_pct × capital". A stored non-null value that
    # diverges from that derivation by more than ₹500 fails startup check 6,
    # so tests that want a specific legacy trade_amount must pass it explicitly
    # together with a consistent fixed_fraction_pct.
    trade_amount=None,
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


def reset_singletons() -> None:
    """Clear every in-memory singleton state — the conftest reset around each
    test, and the 'simulated restart' in the derived-state tests (§9.2-§9.4)."""
    from app.main import app as fastapi_app
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.paper_engine import paper_engine
    from app.services.trading.regime_detector import regime_detector
    from app.services.trading.strategy_orchestrator import strategy_orchestrator
    from app.services.trading.strategy_runner import strategy_runner
    from app.strategies.base import signal_aggregator

    mode_manager._halted = False
    paper_engine._last_known.clear()
    paper_engine._last_persist.clear()
    paper_engine._stale_warned.clear()
    paper_engine._stale_deferred.clear()
    paper_engine._square_off_cache = None
    regime_detector._regime_history.clear()
    regime_detector._last_log_time.clear()
    strategy_orchestrator._regime_cooldown.clear()
    strategy_orchestrator._flag_state.clear()
    strategy_orchestrator._current_regime.clear()
    getattr(strategy_orchestrator, "_daily_halts", {}).clear()
    strategy_runner._seen.clear()
    strategy_runner._seen_day = None
    strategy_runner._strategy_gate_last.clear()
    signal_aggregator.reset()
    try:
        from app.services.trading import risk_gate
        risk_gate._sqlite_locks.clear()
    except ImportError:            # fail-before runs against the pre-FEAT-005 tree
        pass
    from app.services.trading import option_selector as _os_mod
    if hasattr(_os_mod, "rejection_counters"):     # absent on the pre-item-7 tree
        _os_mod.rejection_counters().reset()
    fastapi_app.state.runtime_context = None    # a lifespan test must not leak its context


def with_cfg(rc, **overrides):
    """*rc* with RunConfig fields overridden, for ``using(...)``."""
    import dataclasses
    return dataclasses.replace(rc, cfg=dataclasses.replace(rc.cfg, **overrides))


def with_params(rc, **overrides):
    """*rc* with TradingParams fields overridden, for ``using(...)``."""
    import dataclasses
    from app.runtime.live import StaticParams
    return dataclasses.replace(
        rc, params=StaticParams(dataclasses.replace(rc.params.current(), **overrides)))


class FakeInstruments:
    """InstrumentPort over a fixed row list (the selector's universe)."""

    def __init__(self, rows):
        self.rows = list(rows)

    async def instruments_for(self, db, underlying):
        return [r for r in self.rows if r.name == underlying.upper()]

    def invalidate(self):
        pass


def option_row(strike, expiry, *, name="NIFTY", kind="CE", token=None, lot_size=65):
    """An OPTIDX instrument row (a SimpleNamespace with the Instrument columns the
    selector reads)."""
    from app.db.models.instrument import InstrumentType, OptionType
    return SimpleNamespace(
        name=name, instrument_type=InstrumentType.OPTIDX, option_type=OptionType[kind],
        expiry=expiry, strike=float(strike), token=token or f"{name}-{kind}-{int(strike)}",
        symbol=f"{name}{expiry:%d%b%y}{int(strike)}{kind}".upper(), lot_size=lot_size)


def with_instruments(rc, rows):
    import dataclasses
    return dataclasses.replace(rc, instruments=FakeInstruments(rows))


async def add_closed(Session, *, net: float, at: datetime, strategy: str = "VWAP_EMA_PULLBACK",
                     run_id: str = "PAPER", **overrides) -> int:
    """A closed position whose exit (and entry, 5 minutes earlier) is at *at*."""
    fields = dict(is_open=False, net_pnl=float(net), gross_pnl=float(net), exit_time=at,
                  entry_time=at - timedelta(minutes=5), exit_reason="SL" if net < 0 else "TARGET",
                  strategy_name=strategy, run_id=run_id)
    fields.update(overrides)
    return await add_position(Session, **fields)


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


def position_fields(**overrides) -> dict:
    """Column values for one open 1-lot position (entry 100, SL 85, target 130)."""
    fields = dict(
        strategy_name="VWAP_EMA_PULLBACK",
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
    return fields


async def add_position(Session, **overrides) -> int:
    fields = position_fields(**overrides)
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


def make_signal(strategy="VWAP_EMA_PULLBACK", underlying="NIFTY", direction=Direction.BUY,
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


def regime_state(regime="BULL_TREND_NORMAL_VOL", underlying="NIFTY", price=22610.0,
                 vwap=22600.0, cooldown=0, ts: Optional[datetime] = None):
    from app.services.trading.regime_indicators import RegimeIndicators
    from app.services.trading.regime_persistence import RegimeState
    ind = RegimeIndicators(
        adx=25.0, atr=5.0, atr_percentile=50.0, vwap=vwap, price=price, vwap_cross_count=0,
        volume=0.0, avg_volume=0.0, orb_high=None, orb_low=None, pdh=None, pdl=None,
    )
    return RegimeState(regime=regime, underlying=underlying,
                       timestamp=ts or datetime.now(timezone.utc), indicators=ind,
                       transition_cooldown_remaining=cooldown)


def allow_regime(monkeypatch, regime="BULL_TREND_NORMAL_VOL", underlying="NIFTY",
                 user_flags: Optional[dict] = None):
    """Put the orchestrator in *regime* for *underlying*, as one detected
    candle would: get_current_regime returns that state, the merged flags are
    computed, and the multiplier is the rule's. handle_signal fails closed
    without this (design §6.4)."""
    from app.services.trading.strategy_orchestrator import strategy_orchestrator

    state = regime_state(regime, underlying)

    async def _current(u):
        return state if u == underlying else None

    monkeypatch.setattr(strategy_orchestrator, "get_current_regime", _current)
    flags = asyncio.run(strategy_orchestrator.on_regime_detected(state))
    strategy_orchestrator.merge_flags(underlying, flags, user_flags or {})
    return state


def fake_candidate(ltp=40.0, lot_size=65, bid=0.0, ask=0.0, token="OPT1",
                   tick_age_s=None, tick_open=0.0) -> SimpleNamespace:
    return SimpleNamespace(
        token=token, symbol="NIFTY06OCT2622600CE", strike=22600.0, option_type="CE",
        expiry=date(2026, 10, 13), ltp=ltp, bid=bid, ask=ask, lot_size=lot_size,
        delta_est=0.5, tick_age_s=tick_age_s, tick_open=tick_open,
    )


def selection_result(candidate=None, rejections=None, expiry=None):
    """What option_selector.select returns, around a (fake) candidate."""
    from app.services.trading.option_selector import ExpiryChoice, SelectionResult
    exp = expiry or ExpiryChoice(date(2026, 10, 13), 42, "PREFERRED", "PREFERRED band")
    return SelectionResult(
        candidate=candidate, rejections=dict(rejections or {}),
        evaluated=1 if candidate is not None else 0, expiry=exp, ltp_only_fallbacks=0,
        score_table=[], ranked=[candidate] if candidate is not None else [])


def stub_select(monkeypatch, candidate, rejections=None):
    """Make option_selector.select return *candidate* (None = no option)."""
    from app.services.trading.option_selector import option_selector

    async def _select(**kwargs):
        return selection_result(candidate, rejections)

    monkeypatch.setattr(option_selector, "select", _select)


def pin_ist_time(monkeypatch, t):
    """Pin the IST wall time seen by the risk gate's entry-cutoff check and the
    paper engine, independent of the ctx clock."""
    from app.services.trading import paper_engine as pe_mod, risk_gate
    monkeypatch.setattr(pe_mod, "_ist_time", lambda: t)
    monkeypatch.setattr(risk_gate, "_ist_time", lambda: t)


class FrozenClock:
    """Clock port for tests: time only moves when the test moves it."""

    def __init__(self, at: datetime):
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        self._now = at.astimezone(timezone.utc)

    def now_utc(self) -> datetime:
        return self._now

    def now_ist(self) -> datetime:
        return self._now.astimezone(IST)

    def trading_day(self) -> date:
        return self.now_ist().date()

    def advance(self, seconds: float = 0.0, **kw) -> None:
        self._now = self._now + timedelta(seconds=seconds, **kw)

    def set(self, at: datetime) -> None:
        self._now = (at if at.tzinfo else at.replace(tzinfo=timezone.utc)).astimezone(timezone.utc)


class _LazySessions:
    """Session factory resolved at call time from app.db.base, so the conftest
    blocking factory and the sqlite_db fixture both apply to ctx().sessions."""

    def __call__(self):
        import app.db.base as base
        return base.AsyncSessionLocal()


def make_test_context(*, clock=None, market=None, sessions=None, params=None,
                      user_flags=None, mode: str = "PAPER", run_id: str = "PAPER",
                      instruments=None, cfg=None):
    """A live-shaped RuntimeContext for tests. Defaults: wall clock, the (patchable)
    redis_cache, the (patchable) AsyncSessionLocal, default TradingParams, no
    user flag overrides."""
    from app.core.trading_params import TradingParams
    from app.runtime.context import RunConfig, RuntimeContext
    from app.runtime.live import (
        DbRegimeConfig, DbRunControl, FrozenUserFlags, LiveClock, LiveInstruments,
        RedisMarketData, StaticParams,
    )
    from app.services.cache.redis_client import redis_cache

    p = params or TradingParams()
    if cfg is None:
        cfg = RunConfig(
            mode=mode, capital=p.capital, max_trades_per_day=p.max_trades_per_day,
            max_open_positions=p.max_open_positions, max_lots_per_trade=p.max_lots_per_trade,
            max_daily_loss=p.max_daily_loss, max_daily_loss_pct=p.max_daily_loss_pct,
            open_exposure_cap_pct=p.open_exposure_cap_pct, square_off_time="15:15",
            entry_cutoff_buffer_min=p.entry_cutoff_buffer_min,
        )
    return RuntimeContext(
        run_id=run_id,
        clock=clock or LiveClock(),
        market=market or RedisMarketData(redis_cache),
        sessions=sessions or _LazySessions(),
        regime_config=DbRegimeConfig(),
        params=StaticParams(p),
        cfg=cfg,
        user_flags=user_flags or FrozenUserFlags({}),
        instruments=instruments or LiveInstruments(),
        control=DbRunControl(sessions or _LazySessions()),
        is_replay=False,
    )


class FakeRedis:
    """In-memory stand-in for the redis_cache methods the trading code uses."""

    def __init__(self):
        self.kv: dict[str, Any] = {}
        self.published: list[tuple[str, dict]] = []
        self.fail_ltp = False
        self.clock = None          # set by runtime_ctx so tick ages follow the FrozenClock

    # helpers for tests
    def put_tick(self, token: str, ltp: Optional[float], bid: float = 0.0, ask: float = 0.0,
                 age_s: float = 0.0, open_: Optional[float] = None, oi: Optional[int] = None,
                 volume: Optional[int] = None, iv: Optional[float] = None) -> None:
        now = self.clock.now_utc() if self.clock is not None else datetime.now(timezone.utc)
        ts = (now - timedelta(seconds=age_s)).replace(tzinfo=None)
        if ltp is None:
            self.kv.pop(f"ltp:{token}", None)
        else:
            self.kv[f"ltp:{token}"] = float(ltp)
        self.kv[f"tick:{token}"] = {
            "token": token, "ltp": ltp or 0.0, "open": open_ if open_ is not None else (ltp or 0.0),
            "best_bid": bid, "best_ask": ask, "ts": ts.isoformat(),
        }
        for key, val in (("oi", oi), ("volume", volume), ("iv", iv)):
            if val is not None:
                self.kv[f"tick:{token}"][key] = val

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
