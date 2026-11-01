"""
Causal market-regime classifier for research (Part 6).

Input: completed 5-min bars up to the decision time (history + today) and the
mean 5-min range of each of the previous sessions. Nothing after the decision
time is read; the tests check that appending future bars never changes a
label already given.

Two axes, reported separately and combined into one label:
  volatility  LOW / NORMAL / HIGH: the last hour's mean 5-min range as a
              percentile of the previous 20 sessions' mean 5-min range
              (< 20th, > 80th percentile)
  structure   TREND_UP_STRONG / TREND_DOWN_STRONG: ADX(14) >= 25, |EMA 20
              slope over 30 min| >= 0.5 ATR, and >= 75% of the last 12 closes
              on the slope's side of EMA 20
              WEAK_TREND_UP / WEAK_TREND_DOWN: ADX >= 20 and |slope| >= 0.25 ATR
              otherwise RANGE
Combined label, first match: INSUFFICIENT_DATA (< 30 bars or < 5 prior
sessions), OPENING (before 09:45: the opening range is still forming),
TREND_*_STRONG, WEAK_TREND_*, HIGH_VOL_CHOP (HIGH vol, no trend, >= 3 VWAP
crosses today), COMPRESSION (LOW vol, no trend), RANGE. ``transition`` is
True when the structure label differs from the one 3 bars (15 min) earlier.

The thresholds are conventional round numbers fixed before any result was
looked at. They are not fitted; the validation report checks whether the
labels separate outcomes on unseen months at all.
"""
from __future__ import annotations

from datetime import time as dtime
from typing import Optional

import numpy as np

from app.services.trading.regime_indicators import compute_adx
from app.strategies.indicators import atr, ema, vwap

MIN_BARS = 30
MIN_PRIOR_SESSIONS = 5
ATR_LOOKBACK_SESSIONS = 20
OPENING_END = dtime(9, 45)
LABELS = ("INSUFFICIENT_DATA", "OPENING", "TREND_UP_STRONG", "TREND_DOWN_STRONG",
          "WEAK_TREND_UP", "WEAK_TREND_DOWN", "HIGH_VOL_CHOP", "COMPRESSION", "RANGE")


def _structure(h, l, c, end: int) -> tuple[str, dict]:
    h, l, c = h[:end], l[:end], c[:end]
    adx = compute_adx(h, l, c, 14)
    a = atr(h, l, c, 14)
    e = ema(c, 20)
    if adx is None or not np.isfinite(a[-1]) or a[-1] <= 0 or len(e) < 13 or np.isnan(e[-7]):
        return "UNKNOWN", {}
    slope = (e[-1] - e[-7]) / a[-1]
    side = np.sign(c[-12:] - e[-12:])
    persist = float(np.mean(side == np.sign(slope))) if slope else 0.0
    if adx >= 25 and abs(slope) >= 0.5 and persist >= 0.75:
        lab = "TREND_UP_STRONG" if slope > 0 else "TREND_DOWN_STRONG"
    elif adx >= 20 and abs(slope) >= 0.25:
        lab = "WEAK_TREND_UP" if slope > 0 else "WEAK_TREND_DOWN"
    else:
        lab = "RANGE"
    return lab, {"adx": round(float(adx), 2), "atr5": round(float(a[-1]), 2),
                 "ema_slope_atr": round(float(slope), 3), "persistence": round(persist, 3)}


def vol_bucket(pct: Optional[float]) -> str:
    if pct is None:
        return "UNKNOWN"
    return "LOW" if pct < 0.2 else "HIGH" if pct > 0.8 else "NORMAL"


def classify(bars5: list[dict], today_from: int, prior_ranges: list[float],
             fut_vol: Optional[dict] = None) -> dict:
    """Label at the close of bars5[-1]. *today_from* is the index of today's
    first bar in *bars5*; *prior_ranges* the mean 5-min range of each earlier
    session, oldest first; *fut_vol* optional {bar start: futures volume}."""
    out = {"label": "INSUFFICIENT_DATA", "structure": "UNKNOWN", "vol": "UNKNOWN",
           "transition": False, "vwap_crosses": 0, "atr_pct": None}
    if len(bars5) < MIN_BARS or len(prior_ranges) < MIN_PRIOR_SESSIONS:
        return out
    h = np.array([b["high"] for b in bars5]); l = np.array([b["low"] for b in bars5])
    c = np.array([b["close"] for b in bars5])
    lab, feats = _structure(h, l, c, len(c))
    prev_lab, _ = _structure(h, l, c, len(c) - 3)
    out.update(feats)
    out["structure"] = lab
    out["transition"] = prev_lab not in ("UNKNOWN", lab)
    recent = (h - l)[-12:]
    ref = np.array(prior_ranges[-ATR_LOOKBACK_SESSIONS:])
    pct = float(np.mean(ref < recent.mean()))
    out["atr_pct"] = round(pct, 3)
    out["vol"] = vol_bucket(pct)
    today = bars5[today_from:]
    if today:
        v = np.array([(fut_vol or {}).get(b["start"], 0.0) for b in today])
        vw = vwap(h[today_from:], l[today_from:], c[today_from:], v)
        sides = np.sign(c[today_from:] - vw)
        out["vwap_crosses"] = int(np.sum(sides[1:] * sides[:-1] < 0))
        out["vwap_dist_atr"] = round(float((c[-1] - vw[-1]) / feats["atr5"]), 3) if feats else None
        out["or_expansion"] = round(float((h[today_from:].max() - l[today_from:].min())
                                          / max(1e-9, h[today_from:today_from + 3].max()
                                                - l[today_from:today_from + 3].min())), 3)
    t_end = bars5[-1]["end"].time().replace(tzinfo=None)
    if lab == "UNKNOWN":
        out["label"] = "INSUFFICIENT_DATA"
    elif t_end <= OPENING_END:
        out["label"] = "OPENING"
    elif lab != "RANGE":
        out["label"] = lab
    elif out["vol"] == "HIGH" and out["vwap_crosses"] >= 3:
        out["label"] = "HIGH_VOL_CHOP"
    elif out["vol"] == "LOW":
        out["label"] = "COMPRESSION"
    else:
        out["label"] = "RANGE"
    return out


def session_mean_range(candles: list[dict]) -> Optional[float]:
    """Mean 5-min high-low range of one session (the volatility reference)."""
    from app.strategies.indicators import resample
    b = resample(candles, 5)
    return float(np.mean([x["high"] - x["low"] for x in b])) if b else None
