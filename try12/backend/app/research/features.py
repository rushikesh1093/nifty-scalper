"""
Point-in-time features recorded with every research signal (Part 4A). Each
is computed from candles that closed before the signal and from day context
built from earlier sessions only (``annotate``).
"""
from __future__ import annotations

from typing import Optional

from app.research import regime
from app.research.data import Session
from app.research.variants import bar_shape, fut_rel_volume, pivot_level
from app.strategies.indicators import resample


def annotate(sessions: list[Session]) -> None:
    """Day context from earlier sessions only: previous close, the 5-session
    move up to the previous close, the opening gap and the mean 5-min ranges
    of earlier sessions (regime volatility reference)."""
    ranges: list[float] = []
    for i, s in enumerate(sessions):
        prev = sessions[i - 1] if i else None
        ctx = {"prior_ranges": list(ranges[-regime.ATR_LOOKBACK_SESSIONS:])}
        if prev is not None:
            pc = prev.candles[-1]["close"]
            ctx["prev_close"] = pc
            ctx["gap_pct"] = round((s.candles[0]["open"] - pc) / pc * 100, 3)
            prev_day_move = (pc - prev.candles[0]["open"]) / prev.candles[0]["open"] * 100
            ctx["prev_day_trend_pct"] = round(prev_day_move, 3)
        if i >= 6:
            c5 = sessions[i - 6].candles[-1]["close"]
            ctx["move_5d_pct"] = round((sessions[i - 1].candles[-1]["close"] - c5) / c5 * 100, 3)
        s.ctx = ctx
        r = regime.session_mean_range(s.candles)
        if r is not None:
            ranges.append(r)


def make_feature_fn(bar_minutes: int):
    def fn(sess: Session, k: int, history: list[dict], today: list[dict], tr) -> dict:
        sgn = tr.sign
        b5 = resample(history, 5)
        today_from = next((i for i, b in enumerate(b5) if b["start"].date() == sess.day), len(b5))
        fut_vol = {}
        for b in resample([c for c in sess.fut_candles if c["timestamp"] < today[-1]["timestamp"]
                           or c["timestamp"] == today[-1]["timestamp"]], 5):
            fut_vol[b["start"]] = b["volume"]
        rg = regime.classify(b5, today_from, sess.ctx.get("prior_ranges", []), fut_vol)
        bars = resample(today, bar_minutes)
        shp = bar_shape(bars[-1], sgn) if bars else {}
        atr5 = rg.get("atr5")
        f = {
            "regime": rg["label"], "structure": rg["structure"], "vol": rg["vol"],
            "transition": rg["transition"], "adx5": rg.get("adx"), "atr5": atr5,
            "atr_pct": rg.get("atr_pct"), "ema_slope_atr": rg.get("ema_slope_atr"),
            "trend_align": (None if rg.get("ema_slope_atr") is None
                            else int((rg["ema_slope_atr"] > 0) == (sgn > 0))),
            "vwap_dist_atr_signed": (None if rg.get("vwap_dist_atr") is None
                                     else round(sgn * rg["vwap_dist_atr"], 3)),
            "vwap_crosses": rg.get("vwap_crosses"), "or_expansion": rg.get("or_expansion"),
            "rel_fut_vol": (None if not bars else fut_rel_volume(
                [c for c in sess.fut_candles if c["timestamp"] <= today[-1]["timestamp"]],
                bars[-1], bar_minutes)),
            "bar_body_ratio": round(shp.get("body_ratio", 0.0), 3),
            "bar_close_loc": round(shp.get("close_loc", 0.0), 3),
            "bar_range_atr": (round(shp["range"] / atr5, 3) if atr5 and shp else None),
            "risk_atr": round(tr.risk_pts / atr5, 3) if atr5 else None,
            "gap_pct": sess.ctx.get("gap_pct"), "move_5d_pct": sess.ctx.get("move_5d_pct"),
            "prev_day_trend_pct": sess.ctx.get("prev_day_trend_pct"),
            "vix_open": sess.vix_open, "expiry": sess.is_expiry,
            "pivot_level": pivot_level(tr.notes),
        }
        if f["pivot_level"] and tr.strategy == "PIVOT_BOUNCE":
            lvl = _level_value(tr.notes)
            far = bars[-1]["low"] if sgn > 0 else bars[-1]["high"]
            if lvl is not None:
                f["sweep_depth"] = round(sgn * (lvl - far), 2)
                f["reward_risk"] = (round(abs(tr.target - tr.plan_entry) / tr.risk_pts, 3)
                                    if tr.target and tr.risk_pts > 0 else None)
        return f
    return fn


def _level_value(notes: str) -> Optional[float]:
    import re
    m = re.search(r"(?:at|rejection at) (?:S1|PP|R1) ([0-9.]+)", notes)
    return float(m.group(1)) if m else None
