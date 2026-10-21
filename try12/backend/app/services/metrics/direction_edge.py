"""
Direction-edge evaluation of the unified trend engine (research only).

Question: when the engine calls a direction, does the underlying INDEX move that
way over the next 15 / 30 / 60 minutes? Input is trend_decisions rows (see
app.services.trading.decision_telemetry); nothing here trades or changes any
threshold.

Method
  * signed move  = direction × (index price at +h − index close of the decided
    candle), in index points; also in ATR units (÷ the decision's ATR) and, for
    BUY CE / BUY PE decisions, in R units (÷ the engine's index-point stop).
  * Rows are NOT independent: one trend produces dozens of correlated 1-min
    decisions. So every statistic is computed per WHOLE TRADING DAY first (the
    day's mean signed move) and the mean across days carries a day-block
    bootstrap 95% interval (days resampled with replacement, fixed seed).
  * Populations: ``signals`` = BUY CE / BUY PE decisions, whatever the runner
    gates later did with them; ``trend_calls`` = every candle the engine
    classified UPTREND / DOWNTREND (the classifier alone, before entry rules).
  * Grouped by index, regime, trend-evidence score, strength and entry trigger.
    The score is a 0-6 count of agreeing evidence, not a calibrated probability:
    its table shows what each score level actually did, nothing more.
  * A group is labelled "insufficient" below MIN_DAYS distinct days. An edge is
    reported as "positive" / "negative" only when the day-bootstrap interval
    excludes zero; otherwise "not distinguishable from zero".

Hit rate (share of positive moves) is reported for context only.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from statistics import mean
from typing import Callable, Iterable, Optional

HORIZONS = (15, 30, 60)
MIN_DAYS = 10
BOOT_N = 2000
BOOT_SEED = 7
SIGNAL_DECISIONS = ("BUY CE", "BUY PE")


def _get(r, k):
    return r.get(k) if isinstance(r, dict) else getattr(r, k, None)


def signed_move(r, h: int) -> Optional[float]:
    """direction × (px_h − spot) in index points, or None when not observable."""
    d = _get(r, "direction") or 0
    px, spot = _get(r, f"px_{h}"), _get(r, "spot")
    if not d or px is None or spot is None:
        return None
    return float(d) * (float(px) - float(spot))


def _units(r, h: int, unit: str) -> Optional[float]:
    m = signed_move(r, h)
    if m is None:
        return None
    if unit == "points":
        return m
    if unit == "atr":
        a = _get(r, "atr")
        return m / float(a) if a else None
    if unit == "R":
        s = _get(r, "stop_points")
        return m / float(s) if s else None
    raise ValueError(unit)


def day_bootstrap(day_means: list[float], n: int = BOOT_N,
                  seed: int = BOOT_SEED) -> tuple[Optional[float], Optional[float]]:
    """95% percentile interval of the mean of *day_means*, days resampled."""
    if len(day_means) < 2:
        return None, None
    rng = random.Random(seed)
    k = len(day_means)
    boots = sorted(mean(rng.choice(day_means) for _ in range(k)) for _ in range(n))
    return boots[int(0.025 * n)], boots[int(0.975 * n) - 1]


def summarize(rows: Iterable, h: int, unit: str = "atr") -> dict:
    """Day-level summary of the signed move at horizon *h* for *rows*."""
    by_day: dict[str, list[float]] = defaultdict(list)
    for r in rows:
        v = _units(r, h, unit)
        if v is None or not math.isfinite(v):
            continue
        by_day[str(_get(r, "trading_day"))].append(v)
    days = sorted(by_day)
    day_means = [mean(by_day[d]) for d in days]
    n = sum(len(v) for v in by_day.values())
    out = {"horizon_min": h, "unit": unit, "n_decisions": n, "n_days": len(days)}
    if not days:
        return dict(out, mean=None, ci95=[None, None], positive_days_frac=None,
                    hit_rate=None, verdict="no data")
    lo, hi = day_bootstrap(day_means)
    m = mean(day_means)
    allv = [x for d in days for x in by_day[d]]
    if len(days) < MIN_DAYS:
        verdict = f"insufficient (< {MIN_DAYS} days)"
    elif lo is not None and lo > 0:
        verdict = "positive"
    elif hi is not None and hi < 0:
        verdict = "negative"
    else:
        verdict = "not distinguishable from zero"
    return dict(
        out, mean=round(m, 4),
        ci95=[None if lo is None else round(lo, 4), None if hi is None else round(hi, 4)],
        positive_days_frac=round(sum(1 for x in day_means if x > 0) / len(days), 3),
        hit_rate=round(sum(1 for x in allv if x > 0) / len(allv), 3),
        verdict=verdict,
    )


def _score_bucket(r) -> str:
    s = _get(r, "score")
    return "n/a" if s is None else str(int(s))


GROUPINGS: dict[str, Callable] = {
    "underlying": lambda r: _get(r, "underlying") or "?",
    "regime": lambda r: _get(r, "regime") or "unknown",
    "score": _score_bucket,
    "strength": lambda r: _get(r, "strength") or "?",
    "trigger": lambda r: (_get(r, "entry_trigger") or "none").split(" ")[0],
}


def _grouped(rows: list, key: Callable, h: int, unit: str) -> dict:
    groups: dict[str, list] = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {g: summarize(v, h, unit) for g, v in sorted(groups.items())}


def per_day(rows: list, h: int, unit: str = "atr") -> list[dict]:
    """One line per trading day and index: decisions and the day's mean move."""
    by: dict[tuple, list[float]] = defaultdict(list)
    for r in rows:
        v = _units(r, h, unit)
        if v is not None and math.isfinite(v):
            by[(str(_get(r, "trading_day")), _get(r, "underlying"))].append(v)
    return [{"trading_day": d, "underlying": u, "n": len(v), "mean": round(mean(v), 4)}
            for (d, u), v in sorted(by.items())]


def report(rows: list, *, unit: str = "atr") -> dict:
    """The full direction-edge report for *rows* (trend_decisions rows or dicts)."""
    rows = list(rows)
    signals = [r for r in rows if _get(r, "decision") in SIGNAL_DECISIONS]
    trend_calls = [r for r in rows if (_get(r, "direction") or 0) != 0]
    days = sorted({str(_get(r, "trading_day")) for r in rows})
    out = {
        "scope": {"rows": len(rows), "trading_days": len(days),
                  "first_day": days[0] if days else None, "last_day": days[-1] if days else None,
                  "underlyings": sorted({_get(r, "underlying") for r in rows if _get(r, "underlying")})},
        "method": ("signed index move = direction × (index at +h − decision close); statistics "
                   "are per whole trading day, mean across days with a day-block bootstrap 95% "
                   f"interval; groups with < {MIN_DAYS} days are 'insufficient'. The evidence "
                   "score is a classification count, not a probability."),
        "unit": unit,
        "populations": {},
    }
    for name, pop in (("signals", signals), ("trend_calls", trend_calls)):
        block = {"overall": {f"{h}m": summarize(pop, h, unit) for h in HORIZONS},
                 "by": {g: {f"{h}m": _grouped(pop, key, h, unit) for h in HORIZONS}
                        for g, key in GROUPINGS.items()}}
        if name == "signals":
            block["overall_R"] = {f"{h}m": summarize(pop, h, "R") for h in HORIZONS}
        out["populations"][name] = block
    out["per_day_signals_30m"] = per_day(signals, 30, unit)
    return out
