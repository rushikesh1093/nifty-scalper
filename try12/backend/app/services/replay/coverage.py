"""
Data coverage, measured before any metrics (design §14.4, D11).

A day is TRADEABLE only when every criterion passes; otherwise it is SKIPPED
with the FIRST failing cause, in table order:

  spot candles          >= 300 in the session        NO_SPOT_CANDLES / INSUFFICIENT_CANDLES
  option tick coverage  >= replay_min_option_tick_minutes distinct session
                        minutes AND >= 20 distinct option tokens     NO_OPTION_DATA
  instrument universe   non-empty, a banded expiry, >= replay_min_strikes CE
                        and PE strikes within ±5 strikes of the open
                                    NO_INSTRUMENT_MASTER (data) / NO_BANDED_EXPIRY (policy)
  VIX                   present, or the VIX gate is recorded not-evaluated  NO_VIX

``CoverageCause`` deliberately shares a spelling with ``ReasonCode``
(INSUFFICIENT_CANDLES) at a different granularity: this
enum classifies a whole day before it is replayed; the ReasonCode member is a
per-candle CYCLE decision written by run_cycle. They are separate types.

The previous-day snapshot is still reported (``prev_snapshot``) but no longer
skips a day: no strategy reads previous-day levels, and live trading never
required one, so requiring it here only made replay refuse real recorded days.
Older stored reports may still name the retired NO_PREV_DAY_SNAPSHOT cause.

Also here: the per-candle regime histogram over the run and the D7 finding
when UNCLASSIFIED exceeds 40% of session candles.
"""
from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from enum import Enum
from typing import Optional

from sqlalchemy import distinct, func, select

from app.core.config import settings
from app.db.models.instrument import Instrument, OptionType
from app.db.models.market_study import MarketDailySnapshot
from app.db.models.option_chain import OptionChainSnapshot
from app.db.models.regime import RegimeStateModel
from app.db.models.tick import Tick
from app.services.replay.data_loader import (
    SPOT_TOKENS, replay_instruments, spot_candles, vix_for,
)
from app.services.replay.ports_impl import IST, utc
from app.services.trading.option_selector import choose_expiry
from app.services.trading.session_times import trading_day_bounds_utc


class CoverageCause(str, Enum):
    """Day-level SKIPPED causes; one per day, first failing row wins. Shares a
    spelling with no_trade.ReasonCode on purpose (see the module docstring)."""
    NO_SPOT_CANDLES = "NO_SPOT_CANDLES"            # zero spot candles (holiday, feed down)
    INSUFFICIENT_CANDLES = "INSUFFICIENT_CANDLES"  # some, but < 300 in the session
    NO_OPTION_DATA = "NO_OPTION_DATA"
    NO_INSTRUMENT_MASTER = "NO_INSTRUMENT_MASTER"  # data gap
    NO_BANDED_EXPIRY = "NO_BANDED_EXPIRY"          # policy exclusion (D9)
    NO_VIX = "NO_VIX"                              # only when the VIX gate cannot be marked not-evaluated


TRADEABLE = "TRADEABLE"
SKIPPED = "SKIPPED"
MIN_SPOT_CANDLES = 300
MIN_OPTION_TOKENS = 20
STRIKE_WINDOW = 5                    # ±5 strikes around the day's opening spot
STRIKE_STEP = {"NIFTY": 50, "BANKNIFTY": 100}
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
# run_cycle skips the VIX gate when no VIX price exists, so a missing VIX is
# recorded as "not evaluated" rather than skipping the day.
VIX_GATE_SKIPPABLE = True
NO_TRADE_REGIMES = ("UNCLASSIFIED", "DETECTOR_ERROR", "HIGH_VOL_CHOP", "REGIME_TRANSITION",
                    "EXPIRY_DAY")
UNCLASSIFIED_FINDING_SHARE = 0.40


@dataclass
class DayCoverage:
    underlying: str
    day: date
    status: str = SKIPPED
    cause: Optional[str] = None
    spot_candles: int = 0
    prev_snapshot: Optional[str] = None
    option_tokens_with_ticks: int = 0
    option_tick_minutes: int = 0
    option_ticks: int = 0
    unattributed_tokens: int = 0              # tick tokens with no instruments row (§26 item 4)
    universe_size: int = 0
    expiries: list = field(default_factory=list)   # [(iso, dte)] relative to this day
    nearest_dte: Optional[int] = None
    chosen_expiry: Optional[str] = None
    expiry_reason: Optional[str] = None
    ce_strikes: int = 0
    pe_strikes: int = 0
    snapshot_count: int = 0
    snapshot_cadence_s: Optional[float] = None
    vix: Optional[float] = None
    vix_gate: str = "not_evaluated"
    dte_bands: list = field(default_factory=list)


@dataclass
class CoverageReport:
    days: list[DayCoverage] = field(default_factory=list)

    @property
    def tradeable(self) -> list[DayCoverage]:
        return [d for d in self.days if d.status == TRADEABLE]

    def tradeable_days(self) -> list[date]:
        return sorted({d.day for d in self.tradeable})

    def summary(self) -> dict:
        out: dict = {}
        for d in self.days:
            u = out.setdefault(d.underlying, {"session_days": 0, "tradeable": 0, "skipped": {},
                                              "option_ticks_tradeable": 0})
            u["session_days"] += 1
            if d.status == TRADEABLE:
                u["tradeable"] += 1
                u["option_ticks_tradeable"] += d.option_ticks
            else:
                u["skipped"][d.cause] = u["skipped"].get(d.cause, 0) + 1
        for u in out.values():
            n = u.pop("option_ticks_tradeable")
            u["mean_option_ticks_per_tradeable_day"] = (n / u["tradeable"]) if u["tradeable"] else None
        return out

    def as_dict(self) -> dict:
        from dataclasses import asdict
        return {"summary": self.summary(),
                "days": [{**asdict(d), "day": d.day.isoformat()} for d in self.days]}


def session_days(date_from: date, date_to: date) -> list[date]:
    """Weekdays in range. There is no holiday calendar: a holiday has no data and
    is classified SKIPPED(NO_SPOT_CANDLES) (§26 item 6)."""
    out, d = [], date_from
    while d <= date_to:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _in_session(ts: datetime) -> bool:
    t = utc(ts).astimezone(IST).time()
    return SESSION_OPEN <= t < SESSION_CLOSE


async def classify_day(db, underlying: str, day: date, params) -> DayCoverage:
    u = underlying.upper()
    cov = DayCoverage(underlying=u, day=day,
                      dte_bands=[list(b) for b in params.dte_bands.get(u, ())])
    start, end = trading_day_bounds_utc(day)

    # facts first (all of them are reported, whichever criterion fails) ------
    candles = [c for c in await spot_candles(db, u, day) if _in_session(c["timestamp"])]
    cov.spot_candles = len(candles)
    from app.services.market_data.prev_session import prev_session_snapshot
    prev = getattr(await prev_session_snapshot(db, u, day), "date", None)
    cov.prev_snapshot = prev.isoformat() if prev else None

    universe = await replay_instruments(db, u, day)
    cov.universe_size = len(universe)
    day_tokens = set((await db.execute(select(distinct(Tick.token)).where(
        Tick.timestamp >= start, Tick.timestamp < end))).scalars().all())
    known = set((await db.execute(select(Instrument.token).where(
        Instrument.token.in_(sorted(day_tokens))))).scalars().all()) if day_tokens else set()
    index_tokens = set(SPOT_TOKENS.values()) | {settings.VIX_TOKEN}
    unattributed = day_tokens - known - index_tokens
    cov.unattributed_tokens = len(unattributed)
    option_tokens = {i.token for i in universe} | unattributed
    if option_tokens:
        stamps = (await db.execute(select(Tick.token, Tick.timestamp).where(
            Tick.token.in_(sorted(option_tokens)), Tick.timestamp >= start,
            Tick.timestamp < end))).all()
        in_session = [(tok, ts) for tok, ts in stamps if _in_session(ts)]
        cov.option_ticks = len(in_session)
        cov.option_tokens_with_ticks = len({tok for tok, _ in in_session})
        cov.option_tick_minutes = len({utc(ts).replace(second=0, microsecond=0)
                                       for _, ts in in_session})

    times = sorted({utc(t) for t in (await db.execute(
        select(distinct(OptionChainSnapshot.snapshot_time)).where(
            OptionChainSnapshot.underlying == u, OptionChainSnapshot.snapshot_time >= start,
            OptionChainSnapshot.snapshot_time < end))).scalars().all()})
    cov.snapshot_count = len(times)
    if len(times) >= 2:
        cov.snapshot_cadence_s = statistics.median(
            (b - a).total_seconds() for a, b in zip(times, times[1:]))

    choice = choose_expiry([i.expiry for i in universe if i.expiry], today=day,
                           now_t=SESSION_OPEN, underlying=u, tp=params)
    cov.expiries = [list(o) for o in choice.observed]
    cov.nearest_dte = choice.observed[0][1] if choice.observed else None
    cov.chosen_expiry = choice.expiry.isoformat() if choice.expiry else None
    cov.expiry_reason = choice.reason
    if choice.expiry is not None and candles:
        step = STRIKE_STEP.get(u, 50)
        atm = round(float(candles[0]["open"]) / step) * step
        lo, hi = atm - STRIKE_WINDOW * step, atm + STRIKE_WINDOW * step
        window = [i for i in universe if i.expiry == choice.expiry and i.strike is not None
                  and lo <= i.strike <= hi]
        cov.ce_strikes = len({i.strike for i in window if i.option_type == OptionType.CE})
        cov.pe_strikes = len({i.strike for i in window if i.option_type == OptionType.PE})

    cov.vix = await vix_for(db, day)
    cov.vix_gate = "evaluated" if cov.vix is not None else "not_evaluated"

    # criteria, in table order ----------------------------------------------
    cause: Optional[CoverageCause] = None
    if cov.spot_candles == 0:
        cause = CoverageCause.NO_SPOT_CANDLES
    elif cov.spot_candles < MIN_SPOT_CANDLES:
        cause = CoverageCause.INSUFFICIENT_CANDLES
    elif (cov.option_tick_minutes < params.replay_min_option_tick_minutes
          or cov.option_tokens_with_ticks < MIN_OPTION_TOKENS):
        cause = CoverageCause.NO_OPTION_DATA
    elif not universe:
        cause = CoverageCause.NO_INSTRUMENT_MASTER
    elif choice.expiry is None:
        cause = (CoverageCause.NO_BANDED_EXPIRY if choice.cause == "NO_BANDED_EXPIRY"
                 else CoverageCause.NO_INSTRUMENT_MASTER)
    elif min(cov.ce_strikes, cov.pe_strikes) < params.replay_min_strikes:
        cause = CoverageCause.NO_INSTRUMENT_MASTER          # strikes missing: a data gap
    elif cov.vix is None and not VIX_GATE_SKIPPABLE:
        cause = CoverageCause.NO_VIX
    cov.status = SKIPPED if cause else TRADEABLE
    cov.cause = cause.value if cause else None
    return cov


async def report(db, underlyings, date_from: date, date_to: date, params) -> CoverageReport:
    rep = CoverageReport()
    for day in session_days(date_from, date_to):
        for u in underlyings:
            rep.days.append(await classify_day(db, u, day, params))
    return rep


# ── regime histogram (§18 item 2a, D7) ───────────────────────────────────────

async def regime_histogram(db, run_id: str) -> dict:
    rows = (await db.execute(select(RegimeStateModel.underlying, RegimeStateModel.regime,
                                    func.count(RegimeStateModel.id))
                             .where(RegimeStateModel.run_id == run_id)
                             .group_by(RegimeStateModel.underlying, RegimeStateModel.regime))).all()
    out: dict = {}
    for und, regime, n in rows:
        u = out.setdefault(und, {"counts": {}, "total": 0})
        u["counts"][regime] = int(n)
        u["total"] += int(n)
    for und, u in out.items():
        ineligible = sum(u["counts"].get(r, 0) for r in NO_TRADE_REGIMES)
        u["structurally_ineligible"] = ineligible
        share = u["counts"].get("UNCLASSIFIED", 0) / u["total"] if u["total"] else 0.0
        u["unclassified_share"] = share
        u["finding"] = (f"FINDING: UNCLASSIFIED dominates the window "
                        f"({u['counts'].get('UNCLASSIFIED', 0)} of {u['total']} candles, "
                        f"{share:.0%})") if share > UNCLASSIFIED_FINDING_SHARE else None
    return out
