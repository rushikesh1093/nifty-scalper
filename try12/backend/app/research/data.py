"""
Historical NIFTY data for research, read from a SQLite candle store (the
``candles`` table written by the history backfill: token, kind, symbol, ts in
IST ISO with offset, OHLCV). The file is opened read-only (``mode=ro``), so a
research run can never alter a dataset.

A Session is one trading day of 1-min spot candles in the live candle format
(``timestamp`` is an aware IST datetime, which the live resample accepts
as-is), plus the day's index-futures volume per minute and India VIX.
"""
from __future__ import annotations

import hashlib
import sqlite3
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import date, datetime, time as dtime, timedelta
from pathlib import Path
from typing import Optional

from app.services.market_data.timestamps import IST

SPOT_TOKEN = "99926000"
VIX_TOKEN = "99926017"
SESSION_OPEN, SESSION_CLOSE = dtime(9, 15), dtime(15, 30)
FULL_SESSION_MINUTES = 375
# A day with fewer spot minutes than this is excluded (half days, feed holes);
# every exclusion is listed in Dataset.quality.
MIN_SESSION_MINUTES = 300

# NIFTY weekly expiry weekday: Thursday up to 31 Aug 2025, Tuesday from
# 1 Sep 2025 (NSE circular). A holiday moves it to the previous session.
EXPIRY_SWITCH = date(2025, 9, 1)
# NIFTY lot size by trade date. Verify against NSE circulars before using
# rupee figures: 25 before 20 Nov 2024, 75 until the 30 Dec 2025 series, 65 since.
LOT_SIZE_SCHEDULE = ((date(1900, 1, 1), 25), (date(2024, 11, 20), 75), (date(2025, 12, 30), 65))


def lot_size_on(day: date) -> int:
    size = LOT_SIZE_SCHEDULE[0][1]
    for start, n in LOT_SIZE_SCHEDULE:
        if day >= start:
            size = n
    return size


@dataclass
class Session:
    day: date
    candles: list[dict]                       # 1-min spot, live format, oldest first
    fut_candles: list[dict] = field(default_factory=list)   # same format, real volume
    vix_open: Optional[float] = None
    missing_minutes: int = 0
    is_expiry: bool = False
    ctx: dict = field(default_factory=dict)   # causal day context (features.annotate)


@dataclass
class Dataset:
    path: str
    sha256: str
    sessions: list[Session]
    quality: dict

    @property
    def days(self) -> list[date]:
        return [s.day for s in self.sessions]

    def identifier(self) -> dict:
        return {"file": Path(self.path).name, "sha256": self.sha256,
                "first_day": str(self.days[0]) if self.sessions else None,
                "last_day": str(self.days[-1]) if self.sessions else None,
                "sessions": len(self.sessions)}


def file_sha256(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _candle(ts: str, o, h, l, c, v) -> Optional[dict]:
    dt = datetime.fromisoformat(ts)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=IST)
    dt = dt.astimezone(IST)
    if not (SESSION_OPEN <= dt.time() < SESSION_CLOSE):
        return None
    return {"timestamp": dt, "open": float(o), "high": float(h), "low": float(l),
            "close": float(c), "volume": float(v or 0)}


def expiry_days(days: list[date]) -> set[date]:
    """The weekly expiry session of every week in *days*: the scheduled
    weekday, or the last session before it when that day is a holiday."""
    have = set(days)
    out: set[date] = set()
    if not days:
        return out
    d = days[0] - timedelta(days=days[0].weekday())
    while d <= days[-1] + timedelta(days=7):
        wd = 3 if d < EXPIRY_SWITCH else 1
        exp = d + timedelta(days=wd)
        while exp not in have and exp >= d - timedelta(days=3):
            exp -= timedelta(days=1)
        if exp in have:
            out.add(exp)
        d += timedelta(days=7)
    return out


def load(path: str, start: Optional[date] = None, end: Optional[date] = None,
         hash_file: bool = True) -> Dataset:
    con = sqlite3.connect(f"file:{Path(path).as_posix()}?mode=ro", uri=True)
    try:
        spot: dict[date, list[dict]] = defaultdict(list)
        bad = 0
        for ts, o, h, l, c, v in con.execute(
                "select ts, open, high, low, close, volume from candles where token=? order by ts",
                (SPOT_TOKEN,)):
            cd = _candle(ts, o, h, l, c, v)
            if cd is None:
                continue
            if not (cd["low"] <= min(cd["open"], cd["close"]) and cd["high"] >= max(cd["open"], cd["close"])):
                bad += 1
                continue
            spot[cd["timestamp"].date()].append(cd)

        # Futures: per day, the contract with the most volume that day (the
        # store holds overlapping contracts; the near month dominates volume).
        fut: dict[tuple[date, str], list[dict]] = defaultdict(list)
        for token, ts, o, h, l, c, v in con.execute(
                "select token, ts, open, high, low, close, volume from candles "
                "where kind='FUT' order by ts"):
            cd = _candle(ts, o, h, l, c, v)
            if cd is not None:
                fut[(cd["timestamp"].date(), token)].append(cd)
        fut_by_day: dict[date, list[dict]] = {}
        for (d, _tok), rows in fut.items():
            if d not in fut_by_day or sum(r["volume"] for r in rows) > sum(r["volume"] for r in fut_by_day[d]):
                fut_by_day[d] = rows

        vix_open: dict[date, float] = {}
        for ts, o in con.execute("select ts, open from candles where token=? order by ts", (VIX_TOKEN,)):
            d = datetime.fromisoformat(ts).astimezone(IST).date()
            vix_open.setdefault(d, float(o))
    finally:
        con.close()

    days = sorted(d for d in spot if (start is None or d >= start) and (end is None or d <= end))
    excluded = {str(d): len(spot[d]) for d in days if len(spot[d]) < MIN_SESSION_MINUTES}
    kept = [d for d in days if len(spot[d]) >= MIN_SESSION_MINUTES]
    exp = expiry_days(kept)
    sessions = [Session(day=d, candles=spot[d], fut_candles=fut_by_day.get(d, []),
                        vix_open=vix_open.get(d),
                        missing_minutes=FULL_SESSION_MINUTES - len(spot[d]),
                        is_expiry=d in exp) for d in kept]
    quality = {
        "invalid_ohlc_rows_dropped": bad,
        "sessions_excluded_short": excluded,
        "sessions_with_missing_minutes": sum(1 for s in sessions if s.missing_minutes > 0),
        "missing_minutes_total": sum(max(0, s.missing_minutes) for s in sessions),
        "sessions_without_futures_volume": [str(s.day) for s in sessions
                                            if not any(c["volume"] > 0 for c in s.fut_candles)],
        "sessions_without_vix": [str(s.day) for s in sessions if s.vix_open is None],
        "expiry_sessions": len(exp),
    }
    return Dataset(path=str(path), sha256=file_sha256(path) if hash_file else "",
                   sessions=sessions, quality=quality)
