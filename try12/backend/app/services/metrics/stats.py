"""
Statistics for the metrics store and the validation report (design §16.1).

Pure and stdlib-only. The only randomness in the system is the bootstrap
resampler, seeded from the run record (``random.Random(seed)``), so a re-run
over the same data reproduces every interval (R13.8).

Trades are not independent: at most 6 per day, up to 3 concurrent, all gated
by one per-candle regime classification. The resampling unit that gates D10 is
therefore the trading day (``bootstrap_ci_by_day``); the i.i.d. percentile
interval (``bootstrap_ci``) is printed beside it for reference only.

Both resamplers draw ``rng.randrange(n)`` once per unit, so with one trade per
day (and the trades in day order) they draw identical samples and the two
intervals coincide — the degenerate case the tests pin.
"""
from __future__ import annotations

import math
import random
import statistics
from itertools import groupby
from typing import Callable, Optional, Sequence, TypeVar

T = TypeVar("T")

SQRT_252 = math.sqrt(252)


def mean(values: Sequence[float]) -> Optional[float]:
    return statistics.fmean(values) if values else None


def stdev(values: Sequence[float]) -> Optional[float]:
    """Sample standard deviation; None below two values."""
    return statistics.stdev(values) if len(values) >= 2 else None


def median(values: Sequence[float]) -> Optional[float]:
    return statistics.median(values) if values else None


def wilson_interval(wins: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    """Wilson score interval for a binomial proportion. (None, None) when n == 0.
    Assumes independent trials, which trades are not; it gates nothing (§18 item 4)."""
    if n <= 0:
        return None, None
    p = wins / n
    z2 = z * z
    denom = 1 + z2 / n
    centre = (p + z2 / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z2 / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def _percentile_bounds(out: list[float], iterations: int, alpha: float) -> tuple[float, float]:
    out.sort()
    return out[int(alpha / 2 * iterations)], out[int((1 - alpha / 2) * iterations) - 1]


def bootstrap_ci(values: Sequence[T], statistic: Callable[[list[T]], float], iterations: int,
                 seed: int, alpha: float = 0.05) -> tuple[Optional[float], Optional[float]]:
    """i.i.d. percentile bootstrap — reference only; never gates (§16.1)."""
    vals = list(values)
    n = len(vals)
    if n == 0 or iterations <= 0:
        return None, None
    rng, out = random.Random(seed), []
    for _ in range(iterations):
        out.append(statistic([vals[rng.randrange(n)] for _ in range(n)]))
    return _percentile_bounds(out, iterations, alpha)


def bootstrap_ci_by_day(trades: Sequence[T], statistic: Callable[[list[T]], float],
                        iterations: int, seed: int, alpha: float = 0.05,
                        day_of: Callable[[T], object] = lambda t: t.trading_day,
                        ) -> tuple[Optional[float], Optional[float]]:
    """Block bootstrap: resample trading days with replacement, keeping each day
    intact. ``statistic`` takes a list of trades. This is the interval that
    gates D10 criteria 2, 3 and 5 and the per-strategy OOS test."""
    ordered = sorted(trades, key=day_of)
    days = [list(g) for _, g in groupby(ordered, key=day_of)]
    if not days or iterations <= 0:
        return None, None
    rng, out = random.Random(seed), []
    for _ in range(iterations):
        sample = [t for _ in range(len(days)) for t in days[rng.randrange(len(days))]]
        out.append(statistic(sample))
    return _percentile_bounds(out, iterations, alpha)


def equity_drawdown(ordered_net_pnls: Sequence[float]) -> float:
    """Peak-to-trough fall of the cumulative net-P&L curve (starting at 0),
    reported as a non-negative number (R15.4)."""
    cum = peak = dd = 0.0
    for x in ordered_net_pnls:
        cum += x
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def longest_losing_streak(ordered_net_pnls: Sequence[float], flat_epsilon: float = 0.0) -> int:
    """Longest run of losses (net < -flat_epsilon). A win resets the run; a flat
    trade neither extends nor resets it (D6's streak rule)."""
    best = cur = 0
    for x in ordered_net_pnls:
        if x < -flat_epsilon:
            cur += 1
            best = max(best, cur)
        elif x > flat_epsilon:
            cur = 0
    return best


def sharpe_like(daily: Sequence[float]) -> Optional[float]:
    """mean / stdev × √252 of daily net P&L, risk-free 0. None below two days
    or with zero dispersion."""
    sd = stdev(daily)
    if sd is None or sd == 0:
        return None
    return mean(daily) / sd * SQRT_252


def sortino_like(daily: Sequence[float], target: float = 0.0) -> Optional[float]:
    """mean / downside deviation × √252, where downside deviation is the RMS of
    min(0, x − target) over ALL included days. None when no day is below target."""
    if not daily:
        return None
    downside = [min(0.0, x - target) for x in daily]
    if not any(d < 0 for d in downside):
        return None
    dd = math.sqrt(sum(d * d for d in downside) / len(downside))
    return mean(daily) / dd * SQRT_252
