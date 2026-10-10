"""
Derived per-trade series for a backtest run (design §4.6 ``/series``):
equity / drawdown, exit-reason buckets, the R-multiple histogram and daily P&L.

Built so they cannot disagree with the headline metrics: trades are ordered by
``(exit_time, position_id)`` like ``trade_metrics``; the drawdown peak starts at
0.0 like ``stats.equity_drawdown``; zero trades gives null drawdowns, not 0.0.
"""
from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Optional

from app.api.payloads import EMPTY_REASON
from app.services.metrics import trade_metrics as tm

BIN_WIDTHS = (0.1, 0.25, 0.5)
R_CLIP = 3.0
MAX_EQUITY_POINTS = 2000
_FAR = datetime.max.replace(tzinfo=timezone.utc)


def ordered(trades: list[tm.TradeRow]) -> list[tm.TradeRow]:
    return sorted(trades, key=lambda t: (t.exit_time or _FAR, t.position_id))


def _pct(x: Optional[float], capital: float) -> Optional[float]:
    if x is None or not capital:
        return None
    return x / capital * 100.0


def _iso(v) -> Optional[str]:
    return v.isoformat() if v is not None else None


def equity(trades: list[tm.TradeRow], capital: float) -> tuple[list[dict], Optional[float]]:
    """(points, max_drawdown_inr). max_drawdown_inr is None for zero trades."""
    points, cum, peak = [], 0.0, 0.0
    for i, t in enumerate(ordered(trades), start=1):
        cum += t.net_pnl
        peak = max(peak, cum)
        dd = peak - cum
        points.append({
            "i": i, "exit_time": _iso(t.exit_time), "trading_day": _iso(t.trading_day),
            "net_pnl": t.net_pnl, "cum_net_inr": cum, "cum_net_pct": _pct(cum, capital),
            "drawdown_inr": dd, "drawdown_pct": _pct(dd, capital),
            "r_multiple": t.r_multiple, "exit_reason": t.exit_reason,
        })
    max_dd = max((p["drawdown_inr"] for p in points), default=None)
    return points, max_dd


def downsample(points: list[dict], limit: int = MAX_EQUITY_POINTS) -> tuple[list[dict], bool]:
    """Every ceil(n/limit)-th point plus the first, last, global peak and trough."""
    n = len(points)
    if n <= limit:
        return points, False
    step = math.ceil(n / limit)
    keep = set(range(0, n, step)) | {0, n - 1}
    keep.add(max(range(n), key=lambda k: points[k]["cum_net_inr"]))
    keep.add(min(range(n), key=lambda k: points[k]["cum_net_inr"]))
    return [points[k] for k in sorted(keep)], True


def exit_reasons(trades: list[tm.TradeRow], flat_eps: float) -> list[dict]:
    buckets: dict[str, list[tm.TradeRow]] = {}
    for t in trades:
        buckets.setdefault(t.exit_reason or "UNRECORDED", []).append(t)
    out = []
    for reason, ts in buckets.items():
        rs = [float(t.r_multiple) for t in ts if t.r_valid]
        outcomes = [tm.outcome_of(t, flat_eps) for t in ts]
        out.append({"exit_reason": reason, "count": len(ts),
                    "net_inr": sum(t.net_pnl for t in ts),
                    "avg_r": (sum(rs) / len(rs)) if rs else None,
                    "wins": outcomes.count("WIN"), "losses": outcomes.count("LOSS")})
    out.sort(key=lambda b: (-b["count"], b["exit_reason"]))
    return out


def _k(x: float) -> float:
    return round(x, 9)          # fold float noise (e.g. -1.0 / 0.1) before floor/ceil


def r_histogram(trades: list[tm.TradeRow], bin_width: float) -> dict:
    """Bins over [-3, +3]: half-open [lo, hi) with the last bin closed; r < -3 and
    r > +3 are counted as clipped, so exactly ±3.0 are binned."""
    w = float(bin_width)
    rs = [float(t.r_multiple) for t in trades if t.r_valid]
    out = {"bin_width": w, "bins": [], "r_valid": len(rs),
           "r_unavailable": len(trades) - len(rs),
           "clipped_low": sum(1 for r in rs if r < -R_CLIP),
           "clipped_high": sum(1 for r in rs if r > R_CLIP)}
    inside = [r for r in rs if -R_CLIP <= r <= R_CLIP]
    if not inside:
        return out
    k_clip = round(R_CLIP / w)
    k_lo = max(-k_clip, math.floor(_k(min(inside) / w)))
    k_hi = min(k_clip, math.ceil(_k(max(inside) / w)))
    if k_hi <= k_lo:                                    # every value on one boundary
        if k_lo < k_clip:
            k_hi = k_lo + 1
        else:
            k_lo = k_hi - 1
    n = k_hi - k_lo
    counts = [0] * n
    for r in inside:
        idx = min(max(math.floor(_k(r / w)) - k_lo, 0), n - 1)
        counts[idx] += 1
    out["bins"] = [{"lo": round((k_lo + j) * w, 10), "hi": round((k_lo + j + 1) * w, 10),
                    "count": c} for j, c in enumerate(counts)]
    return out


def daily(trades: list[tm.TradeRow]) -> list[dict]:
    by_day: dict = {}
    for t in ordered(trades):
        d = by_day.setdefault(t.trading_day, {"net_pnl": 0.0, "trades": 0})
        d["net_pnl"] += t.net_pnl
        d["trades"] += 1
    out, cum = [], 0.0
    for day in sorted(by_day):
        cum += by_day[day]["net_pnl"]
        out.append({"trading_day": day.isoformat(), "net_pnl": by_day[day]["net_pnl"],
                    "trades": by_day[day]["trades"], "cum_net_inr": cum})
    return out


def build(trades: list[tm.TradeRow], capital: float, flat_eps: float,
          bin_width: float = 0.25) -> dict:
    """The /series payload body (without run_id / dataset)."""
    points, max_dd = equity(trades, capital)
    points, sampled = downsample(points)
    return {
        "capital": capital, "trades": len(trades), "downsampled": sampled,
        "equity": points,
        "max_drawdown_inr": max_dd, "max_drawdown_pct": _pct(max_dd, capital),
        "exit_reasons": exit_reasons(trades, flat_eps),
        "r_histogram": r_histogram(trades, bin_width),
        "daily": daily(trades),
        "empty_reason": None if trades else EMPTY_REASON,
    }
