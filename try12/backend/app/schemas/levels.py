from typing import Optional

from pydantic import BaseModel


class PriceLevelResponse(BaseModel):
    price:         float
    kind:          str            # SUPPORT | RESISTANCE | PIVOT
    source:        str            # SWING | CPR_* | PDH | PDL | PDC | ORB_* | DAY_*
    strength:      int            # 1..5
    touches:       int
    label:         str
    last_touch_at: Optional[str] = None


class LevelsMeta(BaseModel):
    candle_source:     str                    # redis | db | merged
    candles_used:      int                    # one-minute rows loaded
    resampled_bars:    int                    # bars the min_candles guard saw
    span_days:         int
    sources_available: dict[str, bool]
    warnings:          list[str]
    skipped_rows:      int
    pivot_count:       int
    cluster_tol:       float
    prev_day_date:     Optional[str] = None
    session_date:      str


class LevelsResponse(BaseModel):
    underlying: str
    token:      str
    interval:   str
    spot:       float
    as_of:      str
    levels:     list[PriceLevelResponse]
    meta:       LevelsMeta
