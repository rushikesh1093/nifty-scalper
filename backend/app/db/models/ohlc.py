"""
OHLC candle table — aggregated candles for all timeframes.
Populated two ways:
  1. Real-time: rolling aggregation from tick stream (1m candles)
  2. Historical: bulk-fetched via AngelOne getCandleData REST API on startup
"""
from sqlalchemy import (
    Column, Integer, String, Float, BigInteger, DateTime,
    Index, UniqueConstraint,
)
from sqlalchemy.sql import func
from app.db.base import Base


class OHLC(Base):
    __tablename__ = "ohlc"

    id         = Column(BigInteger, primary_key=True, autoincrement=True)
    token      = Column(String(20), nullable=False)
    symbol     = Column(String(60), nullable=False)
    exchange   = Column(String(10), nullable=False)
    interval   = Column(String(20), nullable=False)   # ONE_MINUTE, FIVE_MINUTE, etc.

    open       = Column(Float,      nullable=False)
    high       = Column(Float,      nullable=False)
    low        = Column(Float,      nullable=False)
    close      = Column(Float,      nullable=False)
    volume     = Column(BigInteger, nullable=True)
    oi         = Column(BigInteger, nullable=True)    # Open interest at candle close

    # Candle open time (bucket start)
    timestamp  = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        UniqueConstraint("token", "interval", "timestamp", name="uq_ohlc_token_interval_ts"),
        Index("ix_ohlc_token_interval_ts", "token", "interval", "timestamp"),
        Index("ix_ohlc_symbol_interval",   "symbol", "interval"),
    )

    def __repr__(self):
        return f"<OHLC {self.symbol} [{self.interval}] O={self.open} H={self.high} L={self.low} C={self.close}>"
