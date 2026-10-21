"""
Entry-timing comparison for the unified trend engine (research only; nothing
here changes how the engine enters or exits).

Arms, for every BUY CE / BUY PE decision in trend_decisions:
  * NEXT_OPEN — what production does: enter at the open of the next candle.
  * LIMIT_2   — a limit at the decided candle's close, valid for the next two
    candles. Filled conservatively: only when a candle trades THROUGH the limit
    by at least ``through_pts`` index points (a touch is not a fill), and at the
    limit price even when the market opens beyond it in our favour. Unfilled
    after two candles = no trade.

Both arms are scored with the same exit: the index level at a fixed horizon
(+15 / +30 / +60 min) minus the entry, signed by direction, in index points and
ATR units. This is a fixed-horizon proxy computed from the telemetry columns;
the production stop / trail / timeout exits are compared offline by the
research simulator. Statistics are per whole trading day (as in direction_edge).

The report also shows what the LIMIT_2 arm gives up: the NEXT_OPEN outcome of
the decisions it failed to fill (missed trades are often the ones that ran away).
"""
from __future__ import annotations

import math
from collections import defaultdict
from statistics import mean
from typing import Iterable, Optional

from app.services.metrics.direction_edge import HORIZONS, MIN_DAYS, SIGNAL_DECISIONS, day_bootstrap

THROUGH_PTS = {"NIFTY": 1.0, "BANKNIFTY": 2.0}   # index points beyond the limit for a fill


def _get(r, k):
    return r.get(k) if isinstance(r, dict) else getattr(r, k, None)


def limit_fill(r, through_pts: float) -> Optional[float]:
    """Limit entry price if the next two candles trade through it, else None."""
    d, limit = _get(r, "direction") or 0, _get(r, "spot")
    hi, lo = _get(r, "next2_high"), _get(r, "next2_low")
    if not d or limit is None or hi is None or lo is None:
        return None
    if d > 0:
        return float(limit) if float(lo) <= float(limit) - through_pts else None
    return float(limit) if float(hi) >= float(limit) + through_pts else None


def _outcome(r, entry: Optional[float], h: int, unit: str) -> Optional[float]:
    d, px = _get(r, "direction") or 0, _get(r, f"px_{h}")
    if entry is None or px is None or not d:
        return None
    m = float(d) * (float(px) - entry)
    if unit == "points":
        return m
    a = _get(r, "atr")
    return m / float(a) if a else None


def _day_stats(pairs: list[tuple[str, float]]) -> dict:
    by: dict[str, list[float]] = defaultdict(list)
    for day, v in pairs:
        if v is not None and math.isfinite(v):
            by[day].append(v)
    days = sorted(by)
    if not days:
        return {"n": 0, "n_days": 0, "mean": None, "ci95": [None, None]}
    dm = [mean(by[d]) for d in days]
    lo, hi = day_bootstrap(dm)
    return {"n": sum(len(v) for v in by.values()), "n_days": len(days),
            "mean": round(mean(dm), 4),
            "ci95": [None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)]}


def report(rows: Iterable, *, unit: str = "atr",
           through: Optional[dict] = None) -> dict:
    through = dict(THROUGH_PTS, **(through or {}))
    sig = [r for r in rows if _get(r, "decision") in SIGNAL_DECISIONS
           and _get(r, "next_open") is not None and _get(r, "next2_high") is not None]
    filled, missed = [], []
    for r in sig:
        f = limit_fill(r, through.get(_get(r, "underlying"), 1.0))
        (filled if f is not None else missed).append((r, f))
    out = {
        "method": ("NEXT_OPEN = enter at the next candle's open; LIMIT_2 = limit at the "
                   "decision close for two candles, filled only when traded through by "
                   f"{through} index pts, at the limit; same fixed-horizon exit for both. "
                   f"Per-day statistics, day-block bootstrap; < {MIN_DAYS} days is insufficient."),
        "unit": unit,
        "signals": len(sig),
        "limit_fill_rate": round(len(filled) / len(sig), 3) if sig else None,
        "entry_improvement_pts": _day_stats(
            [(str(_get(r, "trading_day")),
              float(_get(r, "direction")) * (float(_get(r, "next_open")) - f))
             for r, f in filled]),
        "close_to_next_open_pts": _day_stats(
            [(str(_get(r, "trading_day")),
              float(_get(r, "direction")) * (float(_get(r, "next_open")) - float(_get(r, "spot"))))
             for r in sig]),
        "horizons": {},
    }
    for h in HORIZONS:
        day = lambda r: str(_get(r, "trading_day"))          # noqa: E731
        nxt_all = [(day(r), _outcome(r, float(_get(r, "next_open")), h, unit)) for r in sig]
        lim = [(day(r), _outcome(r, f, h, unit)) for r, f in filled]
        nxt_filled = [(day(r), _outcome(r, float(_get(r, "next_open")), h, unit))
                      for r, _ in filled]
        nxt_missed = [(day(r), _outcome(r, float(_get(r, "next_open")), h, unit))
                      for r, _ in missed]
        # per-signal arm totals: a missed limit contributes 0 (no trade)
        lim_as_book = lim + [(d, 0.0) for d, v in nxt_missed if v is not None]
        out["horizons"][f"{h}m"] = {
            "next_open_all": _day_stats(nxt_all),
            "limit2_filled": _day_stats(lim),
            "next_open_same_signals_as_filled": _day_stats(nxt_filled),
            "next_open_of_missed_limits": _day_stats(nxt_missed),
            "limit2_per_signal_missed_as_zero": _day_stats(lim_as_book),
        }
    return out
