"""
Performance statistics for research trade lists (Part 10). Pure.

Every figure is in R (multiples of the plan's initial spot risk) unless the
name says otherwise.

Dependence: trades from the same session share that session's market, so
confidence intervals resample whole SESSIONS (cluster bootstrap), not
trades. Sharpe and Sortino are computed on DAILY R sums over sessions with at
least one trade, annualised by sqrt(252); Sortino's downside deviation is
the root mean square of min(daily R, 0) over all those days (target 0).
Both are descriptive only: daily R sums of a strategy that trades on a
minority of days are not a portfolio return series.
"""
from __future__ import annotations

import math
import random
from collections import defaultdict
from typing import Callable, Iterable, Optional

BOOTSTRAP_SEED = 20261030
BOOTSTRAP_N = 2000


def wilson(k: int, n: int, z: float = 1.96) -> tuple[Optional[float], Optional[float]]:
    if n == 0:
        return None, None
    p = k / n
    d = 1 + z * z / n
    c = (p + z * z / (2 * n)) / d
    h = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / d
    return round(c - h, 4), round(c + h, 4)


def cluster_bootstrap_mean(values: list[float], clusters: list, n: int = BOOTSTRAP_N,
                           seed: int = BOOTSTRAP_SEED) -> tuple[Optional[float], Optional[float]]:
    """95% CI of the per-trade mean, resampling clusters (sessions) with replacement."""
    if not values:
        return None, None
    groups: dict = defaultdict(list)
    for v, c in zip(values, clusters):
        groups[c].append(v)
    keys = sorted(groups)
    sums = [sum(groups[k]) for k in keys]
    cnts = [len(groups[k]) for k in keys]
    rng = random.Random(seed)
    m = len(keys)
    means = []
    for _ in range(n):
        s = c = 0
        for _ in range(m):
            j = rng.randrange(m)
            s += sums[j]
            c += cnts[j]
        means.append(s / c)
    means.sort()
    return round(means[int(0.025 * n)], 4), round(means[int(0.975 * n) - 1], 4)


def max_drawdown(rs: Iterable[float]) -> float:
    peak = cum = dd = 0.0
    for r in rs:
        cum += r
        peak = max(peak, cum)
        dd = min(dd, cum - peak)
    return round(dd, 4)


def max_consecutive_losses(rs: Iterable[float]) -> int:
    best = cur = 0
    for r in rs:
        cur = cur + 1 if r < 0 else 0
        best = max(best, cur)
    return best


def summarize(rows: list[dict], r_key: str = "r_net", day_key: str = "day",
              boot: bool = True) -> dict:
    """Headline statistics of *rows* (each with r_key, day_key and optional
    r_gross, mfe/mae, hold_min, cost_r), in chronological order."""
    rs = [float(r[r_key]) for r in rows]
    n = len(rs)
    if n == 0:
        return {"n": 0}
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x < 0]
    gp, gl = sum(wins), -sum(losses)
    daily: dict = defaultdict(float)
    for r in rows:
        daily[r[day_key]] += float(r[r_key])
    d = list(daily.values())
    mu = sum(d) / len(d)
    sd = math.sqrt(sum((x - mu) ** 2 for x in d) / (len(d) - 1)) if len(d) > 1 else 0.0
    dsd = math.sqrt(sum(min(x, 0.0) ** 2 for x in d) / len(d))
    out = {
        "n": n, "sessions": len(daily), "wins": len(wins), "losses": len(losses),
        "win_rate": round(len(wins) / n, 4), "win_rate_ci95": wilson(len(wins), n),
        "gross_profit_r": round(gp, 3), "gross_loss_r": round(gl, 3),
        "profit_factor": round(gp / gl, 3) if gl > 0 else None,
        "expectancy_r": round(sum(rs) / n, 4),
        "avg_win_r": round(gp / len(wins), 4) if wins else None,
        "avg_loss_r": round(-gl / len(losses), 4) if losses else None,
        "payoff": round((gp / len(wins)) / (gl / len(losses)), 3) if wins and losses else None,
        "total_r": round(sum(rs), 3),
        "max_drawdown_r": max_drawdown(rs),
        "max_consecutive_losses": max_consecutive_losses(rs),
        "sharpe_daily_ann": round(mu / sd * math.sqrt(252), 3) if sd > 0 else None,
        "sortino_daily_ann": round(mu / dsd * math.sqrt(252), 3) if dsd > 0 else None,
        "worst_session_r": round(min(d), 3), "best_session_r": round(max(d), 3),
        "expectancy_without_best_session": round((sum(rs) - max(d)) / max(1, n - 1), 4),
    }
    for k in ("r_gross", "mfe_r", "mae_r", "hold_min", "cost_r"):
        vals = [float(r[k]) for r in rows if r.get(k) is not None]
        if vals:
            out[f"mean_{k}"] = round(sum(vals) / len(vals), 4)
    if boot:
        out["expectancy_ci95"] = cluster_bootstrap_mean(rs, [r[day_key] for r in rows])
    return out


def breakdown(rows: list[dict], key: Callable[[dict], object], r_key: str = "r_net",
              min_n: int = 1) -> dict:
    groups: dict = defaultdict(list)
    for r in rows:
        groups[key(r)].append(r)
    return {str(k): summarize(v, r_key, boot=len(v) >= 20)
            for k, v in sorted(groups.items(), key=lambda kv: str(kv[0])) if len(v) >= min_n}


def time_bucket(hhmm: str) -> str:
    h, m = (int(x) for x in hhmm.split(":"))
    mins = h * 60 + m
    for end, lab in ((600, "09:15-10:00"), (660, "10:00-11:00"), (750, "11:00-12:30"),
                     (840, "12:30-14:00"), (1000, "14:00-close")):
        if mins < end:
            return lab
    return "14:00-close"
