"""
Research telemetry (migration 0014).

  trend_decisions — one row per (run, underlying, completed 1-min candle) with the
                    unified engine's decision on that candle (BUY CE / BUY PE /
                    NO TRADE + reason code), its trend-evidence score, regime,
                    the runner gate that stopped the live path (if any), and the
                    underlying index price +15 / +30 / +60 minutes later.

The rows are written by app.services.trading.decision_telemetry, independently of
order execution: a decision is recorded whether or not a gate, the risk gate or
the option selector later stopped it, and recording never affects trading.

Every metric column is Float or Integer, never Numeric (§7).
"""
from sqlalchemy import (
    BigInteger, Boolean, Column, Date, DateTime, Float, Index, Integer, JSON, String,
    UniqueConstraint,
)

from app.db.base import Base


class TrendDecisionRecord(Base):
    __tablename__ = "trend_decisions"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id         = Column(String(32), nullable=False)
    trading_day    = Column(Date, nullable=False)
    underlying     = Column(String(20), nullable=False)
    candle_ts      = Column(DateTime(timezone=True), nullable=False)  # start of the decided candle
    evaluated_at   = Column(DateTime(timezone=True), nullable=False)

    # the engine's decision on this candle (computed whether or not the live path ran it)
    decision       = Column(String(10), nullable=False)    # "BUY CE" | "BUY PE" | "NO TRADE"
    reason_code    = Column(String(40), nullable=True)     # NO TRADE reason code
    reason         = Column(String(400), nullable=True)
    trend          = Column(String(12), nullable=True)     # UPTREND | DOWNTREND | SIDEWAYS
    direction      = Column(Integer, nullable=False, default=0)   # +1 up, -1 down, 0 sideways
    strength       = Column(String(10), nullable=True)
    score          = Column(Integer, nullable=True)        # 0-6 trend evidence (not a probability)
    entry_trigger  = Column(String(80), nullable=True)

    # what the live path did with the candle
    gate           = Column(String(48), nullable=True)     # runner gate that stopped it; NULL = evaluated
    engine_evaluated = Column(Boolean, nullable=False, default=False)
    signal_emitted = Column(Boolean, nullable=False, default=False)
    regime         = Column(String(40), nullable=True)
    atr_percentile = Column(Float, nullable=True)

    # index levels at the decision (INDEX POINTS, never premium)
    spot           = Column(Float, nullable=True)          # close of the decided candle
    vwap           = Column(Float, nullable=True)
    atr            = Column(Float, nullable=True)
    stop_points    = Column(Float, nullable=True)
    target_points  = Column(Float, nullable=True)
    features       = Column(JSON, nullable=True)

    # forward path of the underlying, filled as later candles complete
    next_open      = Column(Float, nullable=True)          # open of the next candle
    next2_high     = Column(Float, nullable=True)          # high / low of the next two candles
    next2_low      = Column(Float, nullable=True)
    px_15          = Column(Float, nullable=True)          # close of the candle 15 / 30 / 60 min later
    px_30          = Column(Float, nullable=True)
    px_60          = Column(Float, nullable=True)
    high_60        = Column(Float, nullable=True)          # extremes of the following 60 candles
    low_60         = Column(Float, nullable=True)
    fwd_complete   = Column(Boolean, nullable=False, default=False)
    fwd_updated_at = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "underlying", "candle_ts", name="uq_trend_decisions_key"),
        Index("ix_td_day_underlying", "trading_day", "underlying"),
        Index("ix_td_open_fwd", "fwd_complete", "trading_day"),
    )
