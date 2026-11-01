"""
Live market-data health: what the strategies and the selector are about to
trust, checked every time it is asked for. Pure checks below; the API route
gathers the inputs (cached 1-min candles, cached ticks of subscribed tokens).

Missing minutes are session_readiness's job and are not repeated here. These
checks add:

  candles   duplicate minutes, impossible OHLC (high < low, open/close outside
            the range), price outliers (a 1-min move far beyond the recent
            typical move), and how old the newest closed candle is
  quotes    for every subscribed token with a cached tick: stale quotes (older
            than stale_tick_seconds), crossed quotes (bid > ask), one-sided or
            zero quotes, spreads wider than quote_max_spread_pct of mid, and the
            feed latency (receive time - exchange timestamp)

Nothing is repaired or substituted: a bad record is counted and reported,
never fixed. Outside market hours freshness is not judged (status CLOSED).
"""
from __future__ import annotations

import statistics
from datetime import datetime, time as dtime, timezone
from typing import Iterable, Optional

from app.services.market_data.timestamps import IST, try_normalize_candle_timestamp

SESSION = (dtime(9, 15), dtime(15, 30))
OK, WARN, FAIL, CLOSED = "OK", "WARN", "FAIL", "CLOSED"
OUTLIER_MULT = 8.0          # a 1-min move this many typical moves away is flagged
OUTLIER_FLOOR_PCT = 0.6     # ...and at least this % of price
CANDLE_STALE_S = 150        # newest closed candle older than this in session → FAIL
LATENCY_WARN_MS = 3000


def in_session(now: datetime) -> bool:
    t = now.astimezone(IST)
    return t.weekday() < 5 and SESSION[0] <= t.time().replace(tzinfo=None) < SESSION[1]


def candle_checks(candles: Iterable[dict], now: datetime) -> dict:
    """Checks on one token's cached 1-min candles (any order)."""
    rows, bad_ts = [], 0
    for c in candles or []:
        t = try_normalize_candle_timestamp(c.get("timestamp"))
        if t is None:
            bad_ts += 1
            continue
        try:
            o, h, l, cl = (float(c[k]) for k in ("open", "high", "low", "close"))
        except (KeyError, TypeError, ValueError):
            bad_ts += 1
            continue
        rows.append((t.replace(second=0, microsecond=0), o, h, l, cl))
    rows.sort(key=lambda r: r[0])
    minutes = [r[0] for r in rows]
    dupes = len(minutes) - len(set(minutes))
    invalid = [r[0].isoformat() for r in rows
               if not (r[3] <= min(r[1], r[4]) and r[2] >= max(r[1], r[4]) and r[3] > 0)]
    rets = [abs(b[4] - a[4]) for a, b in zip(rows, rows[1:]) if a[0].date() == b[0].date()]
    typical = statistics.median(rets) if rets else 0.0
    outliers = []
    for a, b in zip(rows, rows[1:]):
        if a[0].date() != b[0].date() or a[4] <= 0:
            continue
        move = abs(b[4] - a[4])
        if move > max(OUTLIER_MULT * typical, a[4] * OUTLIER_FLOOR_PCT / 100):
            outliers.append({"at": b[0].isoformat(), "move": round(move, 2)})
    newest = rows[-1][0] if rows else None
    age_s = round((now - newest).total_seconds() - 60, 1) if newest else None   # bar closes 1 min after start
    live = in_session(now)
    status = OK
    if invalid or bad_ts:
        status = FAIL
    elif dupes or outliers:
        status = WARN
    if live and (age_s is None or age_s > CANDLE_STALE_S):
        status = FAIL
    if not live and status == OK:
        status = CLOSED
    return {"status": status, "candles": len(rows), "duplicates": dupes, "invalid_ohlc": invalid[:10],
            "unreadable": bad_ts, "outliers": outliers[:10], "typical_move": round(typical, 2),
            "newest": newest.isoformat() if newest else None, "newest_age_s": age_s}


def quote_checks(ticks: dict[str, Optional[dict]], now: datetime, stale_s: float,
                 max_spread_pct: float) -> dict:
    """Checks on the cached ticks of the subscribed tokens ({token: tick or None})."""
    n = missing = stale = crossed = one_sided = wide = 0
    latencies: list[float] = []
    worst: list[dict] = []
    for token, tick in ticks.items():
        n += 1
        if not tick:
            missing += 1
            continue
        try:
            ts = datetime.fromisoformat(str(tick.get("ts")))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            age = (now - ts).total_seconds()
        except (TypeError, ValueError):
            age = None
        if age is None or age > stale_s:
            stale += 1
        exch = tick.get("exchange_ts") or 0
        if exch and age is not None:
            lat = (ts.timestamp() * 1000) - float(exch)
            if -60_000 < lat < 600_000:              # ignore clock-format garbage
                latencies.append(lat)
        bid, ask = float(tick.get("best_bid") or 0), float(tick.get("best_ask") or 0)
        if bid <= 0 or ask <= 0:
            one_sided += 1
        elif bid > ask:
            crossed += 1
            worst.append({"token": token, "issue": "crossed", "bid": bid, "ask": ask})
        else:
            mid = (bid + ask) / 2
            if mid > 0 and (ask - bid) / mid * 100 > max_spread_pct:
                wide += 1
    lat_sorted = sorted(latencies)
    p50 = lat_sorted[len(lat_sorted) // 2] if lat_sorted else None
    p95 = lat_sorted[int(len(lat_sorted) * 0.95) - 1] if len(lat_sorted) >= 20 else None
    live = in_session(now)
    usable = n - missing
    status = OK
    if live:
        if usable == 0 or (usable and stale / usable > 0.5):
            status = FAIL
        elif (usable and (stale / usable > 0.1 or crossed / usable > 0.05)) or \
                (p95 is not None and p95 > LATENCY_WARN_MS):
            status = WARN
    else:
        status = CLOSED
    return {"status": status, "tokens": n, "no_tick": missing, "stale": stale, "crossed": crossed,
            "one_sided": one_sided, "wide_spread": wide,
            "latency_ms_p50": round(p50) if p50 is not None else None,
            "latency_ms_p95": round(p95) if p95 is not None else None,
            "examples": worst[:5]}


def overall(parts: Iterable[str]) -> str:
    parts = list(parts)
    for s in (FAIL, WARN):
        if s in parts:
            return s
    return CLOSED if parts and all(p == CLOSED for p in parts) else OK
