"""
Replay data sources, one trading day at a time (design §14.3).

  spot candles          ohlc (spot token, ONE_MINUTE, the IST day)
  option ticks          ticks (the day's universe tokens)
  chain context / marks option_chain_snapshots (the day; observed cadence 300 s)
  VIX                   vix_data: the day's OPEN (known at 09:15), else the
                        nearest prior day's close — never the same day's close,
                        which would be look-ahead
  instruments           replay_instruments: OPTIDX rows whose token traded that
                        day, ignoring is_active and expiry >= today (§10.4)
  previous-day levels   read by the production _pre_market_setup itself

intraday_market_features is deliberately unused: indicators are recomputed
from ohlc through the production compute_all (R13.1).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Optional

from sqlalchemy import desc, distinct, select

from app.core.config import settings
from app.db.models.instrument import Instrument, InstrumentType
from app.db.models.market_study import VIXData
from app.db.models.ohlc import OHLC
from app.db.models.option_chain import OptionChainSnapshot
from app.db.models.tick import Tick
from app.services.trading.session_times import trading_day_bounds_utc

SPOT_TOKENS = {"NIFTY": settings.NIFTY_TOKEN, "BANKNIFTY": settings.BANKNIFTY_TOKEN}


def spot_token(underlying: str) -> str:
    return SPOT_TOKENS[underlying.upper()]


@dataclass
class DayData:
    day: date
    candles: dict[str, list[dict]] = field(default_factory=dict)     # spot token -> rows
    ticks: dict[str, list[dict]] = field(default_factory=dict)       # option token -> rows
    snapshots: dict[str, list[dict]] = field(default_factory=dict)   # option token -> rows
    chains: dict[tuple[str, str], list] = field(default_factory=dict)  # (und, expiry) -> [(ts, strikes)]
    vix: Optional[float] = None
    instruments: dict[str, list] = field(default_factory=dict)       # underlying -> Instrument rows


async def replay_instruments(db, underlying: str, day: date) -> list[Instrument]:
    """Instruments that actually traded on *day*. No is_active, no expiry >= today."""
    start, end = trading_day_bounds_utc(day)
    tokens = select(distinct(Tick.token)).where(Tick.timestamp >= start, Tick.timestamp < end)
    return list((await db.execute(select(Instrument).where(
        Instrument.name == underlying.upper(),
        Instrument.instrument_type == InstrumentType.OPTIDX,
        Instrument.token.in_(tokens),
    ).order_by(Instrument.expiry, Instrument.strike, Instrument.token))).scalars().all())


async def spot_candles(db, underlying: str, day: date) -> list[dict]:
    start, end = trading_day_bounds_utc(day)
    tok = spot_token(underlying)
    rows = (await db.execute(select(OHLC).where(
        OHLC.token == tok, OHLC.interval == "ONE_MINUTE",
        OHLC.timestamp >= start, OHLC.timestamp < end,
    ).order_by(OHLC.timestamp))).scalars().all()
    return [{"token": r.token, "timestamp": r.timestamp, "open": r.open, "high": r.high,
             "low": r.low, "close": r.close, "volume": r.volume, "oi": r.oi} for r in rows]


async def vix_for(db, day: date) -> Optional[float]:
    row = (await db.execute(select(VIXData).where(VIXData.date == day))).scalar_one_or_none()
    if row is not None and row.open:
        return float(row.open)
    prior = (await db.execute(select(VIXData).where(VIXData.date < day)
                              .order_by(desc(VIXData.date)).limit(1))).scalar_one_or_none()
    return float(prior.close) if prior is not None and prior.close else None


async def load_day(db, day: date, underlyings) -> DayData:
    data = DayData(day=day)
    start, end = trading_day_bounds_utc(day)
    tokens: set[str] = set()
    for u in underlyings:
        data.candles[spot_token(u)] = await spot_candles(db, u, day)
        insts = await replay_instruments(db, u, day)
        for i in insts:
            db.expunge(i)
        data.instruments[u.upper()] = insts
        tokens |= {i.token for i in insts}

    if tokens:
        for t in (await db.execute(select(Tick).where(
                Tick.token.in_(sorted(tokens)), Tick.timestamp >= start, Tick.timestamp < end,
        ).order_by(Tick.timestamp, Tick.id))).scalars().all():
            data.ticks.setdefault(t.token, []).append({
                "token": t.token, "timestamp": t.timestamp, "ltp": t.ltp, "open": t.open,
                "high": t.high, "low": t.low, "close": t.close, "volume": t.volume,
                "oi": t.oi, "best_bid": t.best_bid, "best_ask": t.best_ask,
            })

    by_set: dict[tuple, list[dict]] = {}
    for s in (await db.execute(select(OptionChainSnapshot).where(
            OptionChainSnapshot.underlying.in_([u.upper() for u in underlyings]),
            OptionChainSnapshot.snapshot_time >= start, OptionChainSnapshot.snapshot_time < end,
    ).order_by(OptionChainSnapshot.snapshot_time, OptionChainSnapshot.id))).scalars().all():
        row = {"token": s.token, "strike": s.strike,
               "option_type": getattr(s.option_type, "value", s.option_type),
               "ltp": s.ltp, "bid": s.bid, "ask": s.ask, "iv": s.iv, "oi": s.oi,
               "volume": s.volume, "snapshot_time": s.snapshot_time}
        data.snapshots.setdefault(s.token, []).append(row)
        by_set.setdefault((s.underlying.upper(), s.expiry.isoformat(), s.snapshot_time), []).append(
            {k: v for k, v in row.items() if k != "snapshot_time"})
    for (u, expiry, ts), strikes in by_set.items():
        data.chains.setdefault((u, expiry), []).append((ts, strikes))

    data.vix = await vix_for(db, day)
    return data
