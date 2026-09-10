from pydantic import BaseModel
from typing import Optional
from datetime import datetime, date


class TickResponse(BaseModel):
    token:        str
    exchange:     str
    ltp:          float
    open:         Optional[float] = None
    high:         Optional[float] = None
    low:          Optional[float] = None
    close:        Optional[float] = None
    volume:       Optional[int]   = None
    oi:           Optional[int]   = None
    best_bid:     Optional[float] = None
    best_ask:     Optional[float] = None
    best_bid_qty: Optional[int]   = None
    best_ask_qty: Optional[int]   = None
    ts:           Optional[str]   = None


class CandleResponse(BaseModel):
    token:     str
    symbol:    Optional[str] = None
    interval:  str
    timestamp: str
    open:      float
    high:      float
    low:       float
    close:     float
    volume:    Optional[int] = None
    oi:        Optional[int] = None


class OptionStrikeResponse(BaseModel):
    token:       str
    symbol:      str
    strike:      float
    option_type: str
    expiry:      date
    ltp:         Optional[float] = None
    iv:          Optional[float] = None
    oi:          Optional[int]   = None
    volume:      Optional[int]   = None
    delta:       Optional[float] = None


class OptionChainResponse(BaseModel):
    underlying:     str
    underlying_ltp: float
    expiry:         date
    strikes:        list[OptionStrikeResponse]
    pcr:            Optional[float] = None   # Put-Call Ratio by OI
    max_pain:       Optional[float] = None


class LTPBulkRequest(BaseModel):
    tokens: list[str]


class LTPBulkResponse(BaseModel):
    data: dict[str, Optional[float]]
