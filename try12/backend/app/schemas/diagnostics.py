from typing import Optional

from pydantic import BaseModel


class Blocker(BaseModel):
    code:       str
    severity:   str                     # blocking | warning
    message:    str
    underlying: Optional[str] = None
    detail:     Optional[str] = None


class FeedProbe(BaseModel):
    connected:     Optional[bool] = None
    subscriptions: Optional[int]  = None
    session_valid: Optional[bool] = None


class SignalsProbe(BaseModel):
    today_count:          Optional[int]   = None
    by_status:            dict[str, int]  = {}
    last_signal_at:       Optional[str]   = None
    last_signal_age_secs: Optional[int]   = None


class PositionsProbe(BaseModel):
    open:        Optional[int] = None
    today_total: Optional[int] = None


class ReadinessWarning(BaseModel):
    code:    str
    message: str


class ReadinessProbe(BaseModel):
    """Session readiness (app.services.market_data.session_readiness)."""
    ok:                       bool
    checked_at:               str
    prev_session_day:         Optional[str]  = None
    prev_session_candles:     int
    prev_session_expected:    int
    prev_session_in_buffer:   int
    today_candles:            int
    today_expected:           int
    today_missing:            int
    today_gaps:               list[str]      = []
    buffer_size:              int
    out_of_session_in_buffer: int
    pre_open_today:           int
    levels:                   Optional[dict] = None
    levels_problem:           Optional[str]  = None
    warnings:                 list[ReadinessWarning] = []


class UnderlyingProbe(BaseModel):
    underlying:            str
    spot:                  Optional[float] = None
    candles_today:         Optional[int]   = None
    candles_required:      int
    atr:                   Optional[float] = None
    atr_min:               float
    last_candle_at:        Optional[str]   = None
    regime:                Optional[str]   = None
    regime_allows_trading: Optional[bool]  = None
    prev_day_snapshot:     Optional[str]   = None
    readiness:             Optional[ReadinessProbe] = None


class VixProbe(BaseModel):
    ltp:  Optional[float] = None
    min:  float
    max:  float
    gate: str                           # PASS | BLOCKED | BYPASSED


class CapitalProbe(BaseModel):
    capital:            Optional[float] = None
    available:          Optional[float] = None
    trade_amount:       Optional[float] = None
    daily_trades:       Optional[int]   = None
    max_trades_per_day: Optional[int]   = None
    open_positions:     Optional[int]   = None
    max_open_positions: Optional[int]   = None
    circuit_breaker:    Optional[bool]  = None
    circuit_reason:     Optional[str]   = None


class StrategiesProbe(BaseModel):
    total:    Optional[int] = None
    enabled:  Optional[int] = None
    disabled: list[str]     = []


class SignalDiagnosticsResponse(BaseModel):
    as_of:               str
    session_window_open: bool           # clock-based: IST weekday within SESSION_START_IST..SESSION_END_IST
    session_window:      str            # e.g. "09:15-15:25 IST"
    session_window_note: str
    runner_running:      Optional[bool] = None
    mode:                Optional[str]  = None
    study_phase:         Optional[bool] = None
    feed:                FeedProbe
    signals:             SignalsProbe
    positions:           PositionsProbe
    underlyings:         list[UnderlyingProbe]
    vix:                 VixProbe
    capital:             CapitalProbe
    strategies:          StrategiesProbe
    blockers:            list[Blocker]
