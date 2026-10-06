"""
Pure candle math for the chart surfaces — no I/O, no DB, no Redis, no clock reads.

  parse_candle_ts           — ISO string / datetime → aware datetime (naive treated as IST)
  merge_dedup_by_timestamp  — union of ohlc rows + Redis rows keyed on the UTC instant
  resample_candles          — 1m → 3m/5m/15m, IST-aligned buckets
  compute_levels            — support / resistance levels (swing clusters + CPR + PDH/PDL + ORB + day range)

Redis candles carry IST-offset timestamps while ohlc rows are stored in UTC, so every
comparison here is done on the parsed instant, never on the string.
"""
from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone, tzinfo
from typing import Any, Iterable, Optional

import numpy as np

from app.strategies import indicators

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))


# ── Timestamps / merge ────────────────────────────────────────────────────────

def parse_candle_ts(raw: Any) -> datetime:
    """Parse a candle timestamp; naive values are treated as IST. Raises on garbage."""
    dt = raw if isinstance(raw, datetime) else datetime.fromisoformat(str(raw))
    return dt.replace(tzinfo=IST) if dt.tzinfo is None else dt


def merge_dedup_by_timestamp(db_rows: Iterable[dict], cached: Iterable[dict]) -> list[dict]:
    """
    Union of ohlc-table rows and Redis rows, de-duplicated on the UTC instant.
    Cached (Redis) rows are iterated last so they win ties. Every output row carries
    an IST-offset timestamp; output is strictly ascending by instant.
    """
    by_instant: dict[datetime, dict] = {}
    skipped = 0
    for row in (*db_rows, *cached):
        try:
            inst = parse_candle_ts(row["timestamp"]).astimezone(timezone.utc)
        except Exception:
            skipped += 1
            continue
        by_instant[inst] = {**row, "timestamp": inst.astimezone(IST).isoformat()}
    if skipped:
        logger.warning("merge_dedup_by_timestamp: skipped %d candle(s) with unparseable timestamps", skipped)
    return [by_instant[k] for k in sorted(by_instant)]


# ── Resampling ────────────────────────────────────────────────────────────────

def resample_candles(
    candles: list[dict],
    minutes: int,
    *,
    tz: tzinfo = IST,
    token: str,
    interval: str,
    symbol: Optional[str] = None,
) -> list[dict]:
    """
    Aggregate 1-minute candles into `minutes`-wide buckets aligned in `tz`
    (09:15 IST starts a bucket for 3/5/15). Every bucket — including the
    in-progress trailing one — is emitted and carries token/symbol/interval so
    it validates as a CandleResponse. volume/oi/symbol are read with .get()
    because seeded Redis candles have no 'oi' key.
    """
    if minutes <= 0:
        raise ValueError("minutes must be positive")
    size = minutes * 60

    # Parse once, de-duplicate on the instant (last occurrence wins), sort ascending.
    parsed: dict[datetime, dict] = {}
    skipped = 0
    for c in candles:
        try:
            inst = parse_candle_ts(c["timestamp"]).astimezone(timezone.utc)
        except Exception:
            skipped += 1
            continue
        parsed[inst] = c
    if skipped:
        logger.warning("resample_candles: skipped %d candle(s) with unparseable timestamps", skipped)

    buckets: dict[int, dict] = {}
    order: list[int] = []
    for inst in sorted(parsed):
        c = parsed[inst]
        local = inst.astimezone(tz)
        off = int(local.utcoffset().total_seconds()) if local.utcoffset() else 0
        epoch = int(inst.timestamp())
        start_epoch = ((epoch + off) // size) * size - off
        b = buckets.get(start_epoch)
        o, h, l, cl = float(c["open"]), float(c["high"]), float(c["low"]), float(c["close"])
        vol = c.get("volume") or 0
        if b is None:
            buckets[start_epoch] = {
                "token":     token,
                "symbol":    symbol if symbol is not None else c.get("symbol"),
                "interval":  interval,
                "timestamp": datetime.fromtimestamp(start_epoch, tz=timezone.utc).astimezone(tz).isoformat(),
                "open":      o,
                "high":      h,
                "low":       l,
                "close":     cl,
                "volume":    vol,
                "oi":        c.get("oi"),
            }
            order.append(start_epoch)
        else:
            b["high"] = max(b["high"], h)
            b["low"] = min(b["low"], l)
            b["close"] = cl
            b["volume"] = (b["volume"] or 0) + vol
            b["oi"] = c.get("oi")
            if symbol is None and c.get("symbol") is not None:
                b["symbol"] = c.get("symbol")

    out = []
    for k in order:
        b = buckets[k]
        b["volume"] = int(round(b["volume"])) if b["volume"] is not None else None
        out.append(b)
    return out


# ── Levels ────────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class PriceLevel:
    price: float
    kind: str        # "SUPPORT" | "RESISTANCE" | "PIVOT"
    source: str      # SWING | CPR_PIVOT | CPR_BC | CPR_TC | CPR_R1 | CPR_R2 | CPR_S1 | CPR_S2
                     # | PDH | PDL | PDC | ORB_HIGH | ORB_LOW | DAY_HIGH | DAY_LOW
    strength: int    # 1..5
    touches: int
    label: str       # "S1", "PDH", "Swing 22,610" ...
    last_touch_at: Optional[str]


_BASE_STRENGTH = {
    "PDH": 4, "PDL": 4, "CPR_PIVOT": 4,
    "PDC": 3, "CPR_BC": 3, "CPR_TC": 3, "CPR_R1": 3, "CPR_S1": 3,
    "ORB_HIGH": 3, "ORB_LOW": 3, "DAY_HIGH": 3, "DAY_LOW": 3,
    "CPR_R2": 2, "CPR_S2": 2,
}
_LABELS = {
    "PDH": "PDH", "PDL": "PDL", "PDC": "PDC",
    "CPR_PIVOT": "Pivot", "CPR_BC": "BC", "CPR_TC": "TC",
    "CPR_R1": "R1", "CPR_R2": "R2", "CPR_S1": "S1", "CPR_S2": "S2",
    "ORB_HIGH": "ORB High", "ORB_LOW": "ORB Low",
    "DAY_HIGH": "Day High", "DAY_LOW": "Day Low",
}


def _touches_to_strength(touches: int) -> int:
    if touches <= 1:
        return 1
    if touches == 2:
        return 2
    if touches <= 4:
        return 3
    if touches <= 6:
        return 4
    return 5


def _finite_pos(v: Any) -> Optional[float]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) and f > 0 else None


def _normalise(candles: list[dict], tz: tzinfo) -> tuple[list[dict], int]:
    """Coerce to the candle contract; drop + count malformed rows; sort; dedup on instant (last wins)."""
    by_instant: dict[datetime, dict] = {}
    skipped = 0
    for c in candles:
        try:
            ts = parse_candle_ts(c["timestamp"]).astimezone(tz)
        except Exception:
            skipped += 1
            continue
        o, h, l, cl = (_finite_pos(c.get(k)) for k in ("open", "high", "low", "close"))
        if o is None or h is None or l is None or cl is None or h < l:
            skipped += 1
            continue
        vol = c.get("volume")
        by_instant[ts.astimezone(timezone.utc)] = {
            "ts": ts, "open": o, "high": h, "low": l, "close": cl,
            "volume": int(vol) if isinstance(vol, (int, float)) and math.isfinite(vol) else 0,
        }
    return [by_instant[k] for k in sorted(by_instant)], skipped


def compute_levels(
    candles: list[dict],
    spot: float,
    prev_day: Optional[dict] = None,   # {"high","low","close"[, "date"]}
    *,
    session_date: date,                # caller passes datetime.now(IST).date()
    tz: tzinfo = IST,
    pivot_window: int = 3,
    cluster_tol_pct: float = 0.0006,
    min_candles: int = 40,
    max_levels: int = 12,
    orb_candles: int = 15,             # bars forming the opening range at the series' interval
) -> tuple[list[PriceLevel], dict]:
    """Support/resistance levels for `candles` (already at the requested interval). Pure."""
    rows, skipped = _normalise(candles, tz)
    warnings: list[str] = []
    if skipped:
        warnings.append("malformed_candles")

    prev_ok = bool(
        prev_day
        and _finite_pos(prev_day.get("high")) is not None
        and _finite_pos(prev_day.get("low")) is not None
        and _finite_pos(prev_day.get("close")) is not None
        and float(prev_day["high"]) > float(prev_day["low"])
    )
    prev_date = prev_day.get("date") if prev_day else None
    if isinstance(prev_date, date):
        prev_date = prev_date.isoformat()

    tol = max(float(spot) * cluster_tol_pct, 0.05)
    meta: dict = {
        "candles_used":      len(rows),
        "skipped_rows":      skipped,
        "pivot_count":       0,
        "cluster_tol":       round(tol, 4),
        "sources_available": {"swing": False, "cpr": prev_ok, "orb": False, "prev_day": prev_ok},
        "prev_day_date":     prev_date if prev_ok else None,
        "session_date":      session_date.isoformat(),
        "span_days":         len({r["ts"].date() for r in rows}),
        "warnings":          warnings,
    }

    # 1. Guard
    if len(rows) < min_candles:
        warnings.append("insufficient_candles")
        return [], meta

    # 3. Fractal pivots
    w = pivot_window
    highs = [r["high"] for r in rows]
    lows = [r["low"] for r in rows]
    pivots: list[tuple[float, datetime]] = []
    for i in range(w, len(rows) - w):
        hi = highs[i]
        if all(hi > highs[j] for j in range(i - w, i + w + 1) if j != i):
            pivots.append((hi, rows[i]["ts"]))
        lo = lows[i]
        if all(lo < lows[j] for j in range(i - w, i + w + 1) if j != i):
            pivots.append((lo, rows[i]["ts"]))
    meta["pivot_count"] = len(pivots)

    # 4. Cluster (sweep sorted prices, merge within tol of the running mean)
    clusters: list[list[tuple[float, datetime]]] = []
    for p in sorted(pivots, key=lambda x: x[0]):
        if clusters:
            cur = clusters[-1]
            mean = sum(x[0] for x in cur) / len(cur)
            if abs(p[0] - mean) <= tol:
                cur.append(p)
                continue
        clusters.append([p])

    # 5. Swing strength
    swings: list[dict] = []
    for cl in clusters:
        price = sum(x[0] for x in cl) / len(cl)
        touches = len(cl)
        strength = _touches_to_strength(touches)
        if len({x[1].date() for x in cl}) > 1:
            strength = min(5, strength + 1)
        swings.append({
            "price": price, "source": "SWING", "strength": strength, "touches": touches,
            "label": f"Swing {price:,.0f}", "last_touch_at": max(x[1] for x in cl).isoformat(),
        })
    meta["sources_available"]["swing"] = bool(swings)

    # 6. Structural levels
    structural: list[dict] = []

    def _add(source: str, price: float) -> None:
        structural.append({
            "price": float(price), "source": source, "strength": _BASE_STRENGTH[source],
            "touches": 0, "label": _LABELS[source], "last_touch_at": None,
        })

    if prev_ok:
        ph, pl, pc = float(prev_day["high"]), float(prev_day["low"]), float(prev_day["close"])
        _add("PDH", ph)
        _add("PDL", pl)
        _add("PDC", pc)
        c = indicators.cpr(ph, pl, pc)
        _add("CPR_PIVOT", c.pivot)
        _add("CPR_BC", c.bc)
        _add("CPR_TC", c.tc)
        _add("CPR_R1", c.r1)
        _add("CPR_R2", c.r2)
        _add("CPR_S1", c.s1)
        _add("CPR_S2", c.s2)
    else:
        warnings.append("no_prev_day_snapshot")

    today = [r for r in rows if r["ts"].date() == session_date]
    if today:
        o = indicators.orb(
            np.array([r["high"] for r in today], dtype=float),
            np.array([r["low"] for r in today], dtype=float),
            np.array([r["close"] for r in today], dtype=float),
            orb_candles,
        )
        _add("ORB_HIGH", o.high)
        _add("ORB_LOW", o.low)
        _add("DAY_HIGH", max(r["high"] for r in today))
        _add("DAY_LOW", min(r["low"] for r in today))
        meta["sources_available"]["orb"] = True
    else:
        warnings.append("no_today_candles")

    # 7. Merge swing clusters into structural levels (confluence)
    merged_levels = list(structural)
    for s in swings:
        best = None
        for lvl in merged_levels:
            if lvl["source"] == "SWING":
                continue
            d = abs(lvl["price"] - s["price"])
            if d <= tol and (best is None or d < abs(best["price"] - s["price"])):
                best = lvl
        if best is not None:
            best["touches"] += s["touches"]
            best["strength"] = min(5, max(best["strength"], s["strength"]) + 1)
            if s["last_touch_at"] and (best["last_touch_at"] is None or s["last_touch_at"] > best["last_touch_at"]):
                best["last_touch_at"] = s["last_touch_at"]
        else:
            merged_levels.append(s)

    # 8. Classify, 9. rank + cap, then top-down by price
    def _kind(price: float) -> str:
        if price > spot + tol:
            return "RESISTANCE"
        if price < spot - tol:
            return "SUPPORT"
        return "PIVOT"

    ranked = sorted(merged_levels, key=lambda l: (-l["strength"], abs(l["price"] - spot)))[:max_levels]
    levels = [
        PriceLevel(
            price=round(l["price"], 2), kind=_kind(l["price"]), source=l["source"],
            strength=int(l["strength"]), touches=int(l["touches"]), label=l["label"],
            last_touch_at=l["last_touch_at"],
        )
        for l in ranked
    ]
    levels.sort(key=lambda l: l.price, reverse=True)
    return levels, meta
