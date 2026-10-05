from pydantic import BaseModel
from typing import Optional
from datetime import date


class InstrumentResponse(BaseModel):
    id:              int
    token:           str
    symbol:          str
    name:            str
    expiry:          Optional[date]   = None
    strike:          Optional[float]  = None
    option_type:     Optional[str]    = None
    instrument_type: str
    lot_size:        int
    tick_size:       float
    exchange:        str
    is_active:       bool

    class Config:
        from_attributes = True


class InstrumentSearchRequest(BaseModel):
    exchange:    str = "NFO"
    search_text: str
