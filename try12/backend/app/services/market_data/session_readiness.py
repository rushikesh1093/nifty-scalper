"""
Session readiness: is the candle history the strategies will see complete?

Checked at startup and at the 09:00 pre-market setup (StrategyRunner), and
refreshed for GET /trading/signals/diagnostics. Pure: callers pass the candles.

  previous session  its 1-minute candle count against the 375 of a full
                    09:15-15:29 session (the cache and the ohlc table merged),
                    and how many of them are still in the 500-candle cache
  today             in-session minutes closed so far that have no candle in the
                    cache (a late start or a feed outage)
  buffer            candles outside 09:15-15:29 (e.g. pre-open ticks built into
                    candles) that take slots in the 500-candle cache
  levels            whether PIVOT_BOUNCE has the previous session's H/L/C

Every finding is a warning: nothing here stops the runner.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime, time as dtime, timedelta
from typing import Iterable, Optional

from app.services.market_data.prev_session import (
    FULL_SESSION_CANDLES, LAST_MINUTE, SESSION_CLOSE, SESSION_OPEN,
)
from app.services.market_data.timestamps import IST, try_normalize_candle_timestamp

PREV_SESSION_NO_CANDLES = "PREV_SESSION_NO_CANDLES"
PREV_SESSION_INCOMPLETE = "PREV_SESSION_INCOMPLETE"
PREV_SESSION_LEVELS_UNAVAILABLE = "PREV_SESSION_LEVELS_UNAVAILABLE"
TODAY_CANDLE_GAPS = "TODAY_CANDLE_GAPS"
OUT_OF_SESSION_CANDLES_IN_BUFFER = "OUT_OF_SESSION_CANDLES_IN_BUFFER"

MAX_GAP_RANGES_SHOWN = 5


def _in_session(t: dtime) -> bool:
    return SESSION_OPEN <= t < SESSION_CLOSE


def _minutes(candles: Iterable[dict]) -> list[datetime]:
    """Minute-floored IST timestamps of *candles* (unparseable ones dropped)."""
    out = []
    for c in candles or []:
        ts = try_normalize_candle_timestamp(c.get("timestamp"))
        if ts is not None:
            out.append(ts.replace(second=0, microsecond=0))
    return out


def _in_session_count(candles: Iterable[dict], day: date) -> int:
    """Distinct in-session minutes of *day* in *candles*."""
    return len({m for m in _minutes(candles)
                if m.date() == day and _in_session(m.time().replace(tzinfo=None))})


def _ranges(missing: list[datetime]) -> list[str]:
    """Consecutive missing minutes as "HH:MM-HH:MM" (or "HH:MM") ranges."""
    out: list[str] = []
    start = prev = None
    for m in missing:
        if prev is not None and m - prev == timedelta(minutes=1):
            prev = m
            continue
        if start is not None:
            out.append(f"{start:%H:%M}" if start == prev else f"{start:%H:%M}-{prev:%H:%M}")
        start = prev = m
    if start is not None:
        out.append(f"{start:%H:%M}" if start == prev else f"{start:%H:%M}-{prev:%H:%M}")
    return out


@dataclass
class PrevSessionCheck:
    day: Optional[date]                 # the previous session, None when nothing is known
    candles: int                        # distinct in-session minutes (cache + ohlc merged)
    in_buffer: int                      # of those, still in the 500-candle cache
    first: Optional[str] = None         # "HH:MM"
    last: Optional[str] = None


@dataclass
class TodayCheck:
    expected: int                       # in-session minutes closed so far
    candles: int                        # of those, present in the cache
    gaps: list[str] = field(default_factory=list)
    missing: int = 0
    buffer_size: int = 0
    out_of_session_in_buffer: int = 0   # any day, outside 09:15-15:29
    pre_open_today: int = 0             # today, before 09:15


@dataclass
class Readiness:
    underlying: str
    day: date
    checked_at: datetime
    prev: PrevSessionCheck
    today: TodayCheck
    levels: Optional[dict]              # {"day", "high", "low", "close", "source"} or None
    levels_problem: Optional[str]       # why there are no levels (None when there are,
                                        # or when no strategy needs them)
    warnings: list[dict] = field(default_factory=list)   # [{code, message}]

    @property
    def ok(self) -> bool:
        return not self.warnings

    def summary(self) -> str:
        p, t = self.prev, self.today
        if p.day is None:
            prev = "previous session unknown"
        else:
            span = f", {p.first}-{p.last}" if p.first else ""
            prev = (f"previous session {p.day} {p.candles}/{FULL_SESSION_CANDLES} candles"
                    f"{span} ({p.in_buffer} in buffer)")
        today = f"today {t.candles}/{t.expected} candles"
        today += f", {t.missing} missing" if t.missing else ", no gaps"
        buf = (f"buffer {t.buffer_size} candles, {t.out_of_session_in_buffer} outside "
               f"{SESSION_OPEN:%H:%M}-{SESSION_CLOSE:%H:%M}")
        if self.levels is not None:
            lv = self.levels
            levels = (f"levels {lv['day']} H={lv['high']:.2f} L={lv['low']:.2f} "
                      f"C={lv['close']:.2f} ({lv['source']})")
        elif self.levels_problem:
            levels = "levels UNAVAILABLE"
        else:
            levels = "levels not needed"
        verdict = "OK" if self.ok else f"{len(self.warnings)} warning(s)"
        return (f"[{self.underlying}] session readiness {self.day} at "
                f"{self.checked_at:%H:%M}: {prev}; {today}; {buf}; {levels} -> {verdict}")

    def as_dict(self) -> dict:
        p, t = self.prev, self.today
        return {
            "ok": self.ok,
            "checked_at": self.checked_at.isoformat(),
            "prev_session_day": p.day.isoformat() if p.day else None,
            "prev_session_candles": p.candles,
            "prev_session_expected": FULL_SESSION_CANDLES,
            "prev_session_in_buffer": p.in_buffer,
            "today_candles": t.candles,
            "today_expected": t.expected,
            "today_missing": t.missing,
            "today_gaps": t.gaps,
            "buffer_size": t.buffer_size,
            "out_of_session_in_buffer": t.out_of_session_in_buffer,
            "pre_open_today": t.pre_open_today,
            "levels": self.levels,
            "levels_problem": self.levels_problem,
            "warnings": list(self.warnings),
        }


def check_prev_session(day: date, merged: Iterable[dict], cache: Iterable[dict],
                       prev_day: Optional[date] = None) -> PrevSessionCheck:
    """The previous session's 1-min coverage. *prev_day*: the session the levels
    came from (the broker knows real sessions); else the latest day before *day*
    with in-session candles."""
    sess: dict[date, set[datetime]] = {}
    for m in _minutes(merged):
        if m.date() < day and _in_session(m.time().replace(tzinfo=None)):
            sess.setdefault(m.date(), set()).add(m)
    if prev_day is None:
        prev_day = max(sess) if sess else None
    if prev_day is None:
        return PrevSessionCheck(None, 0, 0)
    stamps = sorted(sess.get(prev_day, ()))
    return PrevSessionCheck(
        prev_day, len(stamps), _in_session_count(cache, prev_day),
        f"{stamps[0]:%H:%M}" if stamps else None, f"{stamps[-1]:%H:%M}" if stamps else None,
    )


def check_today(now: datetime, cache: Iterable[dict]) -> TodayCheck:
    """Today's in-session minutes closed by *now* (IST, aware) against the cache,
    and the out-of-session candles in the cache. A minute's candle exists once
    the minute has ended, so at 09:16 only 09:15 is expected."""
    now = now.astimezone(IST)
    day = now.date()
    cache = list(cache or [])
    minutes = _minutes(cache)
    have = {m for m in minutes if m.date() == day and _in_session(m.time().replace(tzinfo=None))}
    out_of_session = sum(1 for m in minutes if not _in_session(m.time().replace(tzinfo=None)))
    pre_open = sum(1 for m in minutes
                   if m.date() == day and m.time().replace(tzinfo=None) < SESSION_OPEN)
    expected: list[datetime] = []
    if day.weekday() < 5:
        m = datetime.combine(day, SESSION_OPEN, now.tzinfo)
        last = min(now.replace(second=0, microsecond=0) - timedelta(minutes=1),
                   datetime.combine(day, LAST_MINUTE, now.tzinfo))
        while m <= last:
            expected.append(m)
            m += timedelta(minutes=1)
    missing = [m for m in expected if m not in have]
    return TodayCheck(
        expected=len(expected), candles=len(expected) - len(missing), gaps=_ranges(missing),
        missing=len(missing), buffer_size=len(cache), out_of_session_in_buffer=out_of_session,
        pre_open_today=pre_open,
    )


def build(underlying: str, now: datetime, prev: PrevSessionCheck, today: TodayCheck,
          levels: Optional[dict], levels_problem: Optional[str]) -> Readiness:
    """Combine the checks into a Readiness with its warnings."""
    now = now.astimezone(IST)
    w: list[dict] = []
    if prev.day is None:
        w.append({"code": PREV_SESSION_NO_CANDLES,
                  "message": f"{underlying}: no previous-session 1-minute candles in the cache "
                             f"or the ohlc table; multi-day indicators start cold"})
    elif prev.candles < FULL_SESSION_CANDLES:
        span = f", {prev.first}-{prev.last}" if prev.first else ""
        w.append({"code": PREV_SESSION_INCOMPLETE,
                  "message": f"{underlying}: previous session {prev.day} has {prev.candles}/"
                             f"{FULL_SESSION_CANDLES} one-minute candles{span} "
                             f"({FULL_SESSION_CANDLES - prev.candles} missing)"})
    if levels is None and levels_problem:
        w.append({"code": PREV_SESSION_LEVELS_UNAVAILABLE,
                  "message": f"{underlying}: previous-day levels unavailable, PIVOT_BOUNCE is "
                             f"off today: {levels_problem}"})
    if today.missing:
        shown = ", ".join(today.gaps[:MAX_GAP_RANGES_SHOWN])
        more = (f" and {len(today.gaps) - MAX_GAP_RANGES_SHOWN} more range(s)"
                if len(today.gaps) > MAX_GAP_RANGES_SHOWN else "")
        w.append({"code": TODAY_CANDLE_GAPS,
                  "message": f"{underlying}: {today.missing} of {today.expected} minutes since "
                             f"{SESSION_OPEN:%H:%M} have no candle ({shown}{more})"})
    if today.out_of_session_in_buffer:
        w.append({"code": OUT_OF_SESSION_CANDLES_IN_BUFFER,
                  "message": f"{underlying}: {today.out_of_session_in_buffer} of "
                             f"{today.buffer_size} cached candles are outside "
                             f"{SESSION_OPEN:%H:%M}-{SESSION_CLOSE:%H:%M} "
                             f"({today.pre_open_today} from before today's open)"})
    return Readiness(underlying, now.date(), now, prev, today, levels, levels_problem, w)


def refresh(rep: Readiness, now: datetime, cache: Iterable[dict], levels: Optional[dict],
            levels_problem: Optional[str]) -> Readiness:
    """*rep* brought up to *now*: the previous session's candle count is kept (the
    ohlc table is not re-read), its buffer share and today's checks are redone
    from *cache*."""
    cache = list(cache or [])
    prev = rep.prev
    if prev.day is not None:
        prev = replace(prev, in_buffer=_in_session_count(cache, prev.day))
    return build(rep.underlying, now, prev, check_today(now, cache), levels, levels_problem)
