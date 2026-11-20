"""
Regime v2 (shadow): only the parts of the market state that held up on three
years of NIFTY (docs/REGIME_REDESIGN.md). Pure: candles in, a label out.

  vol        LOW / NORMAL / HIGH: the current 1-min ATR(14) as a percentile of
             the last REGIME_V2_LOOKBACK ATR values in the 500-candle history
             (< 30 / > 70). Predicted the next 30 minutes' range in every year
             (Spearman +0.35 / +0.37 / +0.24; HIGH ~68 pts vs LOW ~44 pts).
  or_side    ABOVE / INSIDE / BELOW today's 09:15-09:30 range, or FORMING
             before 09:30. The v1 breakout labels kept their sign every year
             (next 30 min about +3 / -3 pts) - smaller than option costs, so
             it is information, not a trade signal.
  data_ok    False (label INSUFFICIENT_DATA) with no candle today, or
             too little history for the percentile.

Dropped from v1: the trend labels (ADX / VWAP side), whose next-30-minute
direction changed sign between years, and the name-based strategy map.

v2 gates nothing. It is computed for the dashboard and for the research
evaluation (app.research.live_regime_eval) until a strategy rule built on it
passes the same evidence gates as any strategy.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import time as dtime
from typing import Optional

import numpy as np

from app.services.market_data.timestamps import try_normalize_candle_timestamp
from app.services.trading.regime_indicators import compute_atr_percentile
from app.strategies.indicators import atr

REGIME_V2_LOOKBACK = 486
VOL_LOW, VOL_HIGH = 30.0, 70.0
MIN_TODAY = 1
OR_END = dtime(9, 30)


@dataclass(frozen=True)
class RegimeV2:
    label: str                    # e.g. "VOL_HIGH|OR_ABOVE" or "INSUFFICIENT_DATA"
    vol: str
    atr_percentile: Optional[float]
    or_side: str
    or_high: Optional[float]
    or_low: Optional[float]
    data_ok: bool

    def to_dict(self) -> dict:
        return asdict(self)


def vol_bucket(pct: Optional[float]) -> str:
    if pct is None:
        return "UNKNOWN"
    return "LOW" if pct < VOL_LOW else "HIGH" if pct > VOL_HIGH else "NORMAL"


def classify_v2(today: list[dict], history: list[dict]) -> RegimeV2:
    """*today*: today's completed 1-min candles; *history*: up to the last 500
    session candles ending with today's (the runner's two lists)."""
    if len(today) < MIN_TODAY or len(history) < REGIME_V2_LOOKBACK // 4:
        return RegimeV2("INSUFFICIENT_DATA", "UNKNOWN", None, "UNKNOWN", None, None, False)
    # ATR on the history (it ends with today's candles), so the label exists
    # from the first candle of the day, as the runner's ATR fallback does.
    h = np.array([float(c["high"]) for c in history]); l = np.array([float(c["low"]) for c in history])
    c = np.array([float(x["close"]) for x in history])
    a = atr(h, l, c, 14)
    pct = None
    if np.isfinite(a[-1]):
        lookback = min(REGIME_V2_LOOKBACK, len(history) - 14)
        pct = round(compute_atr_percentile(float(a[-1]), history, period=lookback), 1)
    vol = vol_bucket(pct)

    rng = [x for x in today
           if (t := try_normalize_candle_timestamp(x.get("timestamp"))) is not None
           and t.time().replace(tzinfo=None) < OR_END]
    last_t = try_normalize_candle_timestamp(today[-1].get("timestamp"))
    if last_t is None or last_t.time().replace(tzinfo=None) < dtime(9, 29) or not rng:
        or_side, oh, ol = "FORMING", None, None
    else:
        oh, ol = max(float(x["high"]) for x in rng), min(float(x["low"]) for x in rng)
        px = float(today[-1]["close"])
        or_side = "ABOVE" if px > oh else "BELOW" if px < ol else "INSIDE"
    data_ok = pct is not None
    label = f"VOL_{vol}|OR_{or_side}" if data_ok else "INSUFFICIENT_DATA"
    return RegimeV2(label, vol, pct, or_side, oh, ol, data_ok)
