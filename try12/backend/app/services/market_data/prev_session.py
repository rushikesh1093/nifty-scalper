"""
The previous trading session's high / low / close (the PIVOT_BOUNCE floor pivots).

Resolved once a day, before the open (and on a mid-session start), and kept
apart from the rolling 500-candle cache, so the levels last the whole session.

Sources, most reliable first:
  broker_daily  AngelOne's ONE_DAY candle of the index (live only, never in
                replay). The exchange's own daily bar: complete whatever the
                app's uptime was, and it only has real sessions, so weekends and
                NSE holidays are skipped without a holiday calendar.
  minute_1m     the previous session rebuilt from 1-minute candles (the cache
                and the ohlc table, merged on the instant). Used only when that
                session is complete (09:15 and 15:29 present, at least
                MIN_COMPLETE_CANDLES of 375); a partial session gives no levels.

market_daily_snapshots is not used: its OHLC is built at 15:35 from the last
500 cached candles, which span two days, so its high/low mix sessions.

"The previous session" is the latest day before today that any source has data
for. An older day is never used when a newer one is known to have traded.
"""
from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, time as dtime, timedelta, timezone
from typing import Iterable, Optional

from app.services.market_data.timestamps import IST, try_normalize_candle_timestamp

logger = logging.getLogger(__name__)

SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
LAST_MINUTE = dtime(15, 29)
FULL_SESSION_CANDLES = 375
MIN_COMPLETE_CANDLES = 370          # at most 5 missing minutes
LOOKBACK_DAYS = 10                  # calendar days searched (a long weekend + holidays)
BROKER_TIMEOUT_S = 20.0

BROKER_DAILY = "broker_daily"
MINUTE_1M = "minute_1m"


@dataclass(frozen=True)
class PrevSession:
    day: date
    high: float
    low: float
    close: float
    source: str
    detail: str = ""


@dataclass(frozen=True)
class PrevSessionResult:
    levels: Optional[PrevSession]
    reason: Optional[str] = None              # why there are no levels
    note: Optional[str] = None                # e.g. weekdays assumed to be holidays


def _ok(h: float, l: float, c: float) -> bool:
    return all(math.isfinite(x) and x > 0 for x in (h, l, c)) and l <= c <= h


def _weekdays_between(a: date, b: date) -> list[date]:
    out, d = [], a + timedelta(days=1)
    while d < b:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _sessions(candles: Iterable[dict], before: date) -> dict[date, dict[datetime, dict]]:
    """In-session 1-min candles before *before*, per IST weekday, de-duplicated on the minute.

    Weekend dates are dropped: stray off-hours ticks stamped on a Sunday (2 Nov
    2026: 4 candles on 1 Nov) would otherwise become "the previous session" and
    hide Friday's complete one. A special weekend session still counts through
    the broker daily candle, which is not filtered."""
    out: dict[date, dict[datetime, dict]] = {}
    for c in candles:
        ts = try_normalize_candle_timestamp(c.get("timestamp"))
        if ts is None or ts.date() >= before or ts.date().weekday() >= 5:
            continue
        if not (SESSION_OPEN <= ts.time().replace(tzinfo=None) < SESSION_CLOSE):
            continue
        try:
            h, l, cl = float(c["high"]), float(c["low"]), float(c["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if not _ok(h, l, cl):
            continue
        out.setdefault(ts.date(), {})[ts.replace(second=0, microsecond=0)] = \
            {"high": h, "low": l, "close": cl}
    return out


def _daily(rows: Iterable, before: date) -> dict[date, tuple[float, float, float]]:
    """Broker ONE_DAY rows ([ts, o, h, l, c, v]) before *before*, per IST date."""
    out: dict[date, tuple[float, float, float]] = {}
    for r in rows or []:
        try:
            ts = try_normalize_candle_timestamp(r[0])
            h, l, c = float(r[2]), float(r[3]), float(r[4])
        except (IndexError, TypeError, ValueError):
            continue
        if ts is None or ts.date() >= before or not _ok(h, l, c):
            continue
        out[ts.date()] = (h, l, c)
    return out


def resolve(day: date, minute_candles: Iterable[dict],
            daily_rows: Optional[Iterable] = None) -> PrevSessionResult:
    """The previous session's H/L/C for trading day *day*. Pure.

    *daily_rows*: the broker's ONE_DAY rows, or None when the broker was not
    asked (replay) or failed."""
    minutes = _sessions(minute_candles, day)
    daily = _daily(daily_rows, day) if daily_rows is not None else {}
    known = set(minutes) | set(daily)
    if not known:
        return PrevSessionResult(None, f"no previous-session data before {day} "
                                       f"(no broker daily candle, no 1-min candles in the last "
                                       f"{LOOKBACK_DAYS} days)")
    prev = max(known)

    if prev in daily:
        h, l, c = daily[prev]
        return PrevSessionResult(PrevSession(prev, h, l, c, BROKER_DAILY, "AngelOne ONE_DAY candle"))

    note = None
    gap = _weekdays_between(prev, day)
    if gap:
        note = (f"weekday(s) {', '.join(d.isoformat() for d in gap)} have no data and are "
                f"assumed to be exchange holidays (no broker daily candle to confirm)")
    sess = minutes[prev]
    stamps = sorted(sess)
    first, last = stamps[0].time().replace(tzinfo=None), stamps[-1].time().replace(tzinfo=None)
    n = len(stamps)
    shape = f"{n}/{FULL_SESSION_CANDLES} one-minute candles, {first:%H:%M}-{last:%H:%M}"
    if n < MIN_COMPLETE_CANDLES or first != SESSION_OPEN or last != LAST_MINUTE:
        why = ("broker daily candle unavailable" if daily_rows is None
               else f"broker daily candle has no row for {prev}")
        return PrevSessionResult(None, f"previous session {prev} is incomplete ({shape}) and the "
                                       f"{why}: its true high/low/close are unknown", note)
    h = max(r["high"] for r in sess.values())
    l = min(r["low"] for r in sess.values())
    c = sess[stamps[-1]]["close"]
    return PrevSessionResult(PrevSession(prev, h, l, c, MINUTE_1M, shape), None, note)


# ── I/O ──────────────────────────────────────────────────────────────────────

async def db_minute_candles(db, token: str, day: date) -> list[dict]:
    """1-min ohlc rows of *token* for the LOOKBACK_DAYS calendar days before *day*.
    Stored timestamps are UTC; naive ones (SQLite) are read as UTC."""
    from sqlalchemy import select

    from app.db.models.ohlc import OHLC
    from app.services.trading.session_times import trading_day_bounds_utc

    start = trading_day_bounds_utc(day - timedelta(days=LOOKBACK_DAYS))[0]
    end = trading_day_bounds_utc(day)[0]
    rows = (await db.execute(select(OHLC.timestamp, OHLC.high, OHLC.low, OHLC.close).where(
        OHLC.token == token, OHLC.interval == "ONE_MINUTE",
        OHLC.timestamp >= start, OHLC.timestamp < end,
    ))).all()
    out = []
    for ts, h, l, c in rows:
        ts = ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
        out.append({"timestamp": ts.astimezone(IST).isoformat(), "high": h, "low": l, "close": c})
    return out


async def broker_daily_rows(token: str, day: date) -> Optional[list]:
    """The index's ONE_DAY candles for the LOOKBACK_DAYS before *day* from AngelOne,
    or None when there is no valid broker session or the call fails. Never logs in."""
    from app.services.broker.angel_client import _session, angel_client
    from app.services.market_data.market_manager import INDEX_HISTORICAL_TOKENS

    if not _session.is_valid():
        logger.info("Previous-session levels: no AngelOne session, broker daily candle skipped")
        return None
    try:
        return await asyncio.wait_for(angel_client.get_candle_data(
            exchange="NSE", symbol_token=INDEX_HISTORICAL_TOKENS.get(token, token),
            interval="ONE_DAY",
            from_date=f"{day - timedelta(days=LOOKBACK_DAYS)} 09:15",
            to_date=f"{day - timedelta(days=1)} 15:30",
        ), BROKER_TIMEOUT_S)
    except Exception as exc:
        logger.warning("Previous-session levels: AngelOne ONE_DAY fetch failed for %s: %s",
                       token, exc)
        return None
