"""
Tick data table — every price update received from AngelOne WebSocket.
This becomes a TimescaleDB hypertable partitioned on `timestamp`.
High-frequency writes; use BRIN index on timestamp for fast range scans.
"""
from sqlalchemy import (
    Column, Integer, String, Float, BigInteger, DateTime, Index,
)
from sqlalchemy.sql import func
from app.db.base import Base


class Tick(Base):
    __tablename__ = "ticks"

    id             = Column(BigInteger, primary_key=True, autoincrement=True)
    token          = Column(String(20),  nullable=False, index=True)
    symbol         = Column(String(60),  nullable=False)
    exchange       = Column(String(10),  nullable=False)

    # Price fields from AngelOne WebSocket feed
    ltp            = Column(Float,       nullable=False)            # Last traded price
    open           = Column(Float,       nullable=True)
    high           = Column(Float,       nullable=True)
    low            = Column(Float,       nullable=True)
    close          = Column(Float,       nullable=True)             # Prev day close

    # Volume & OI
    volume         = Column(BigInteger,  nullable=True)
    oi             = Column(BigInteger,  nullable=True)             # Open interest
    oi_change      = Column(BigInteger,  nullable=True)

    # Bid/Ask
    best_bid       = Column(Float,       nullable=True)
    best_ask       = Column(Float,       nullable=True)
    best_bid_qty   = Column(Integer,     nullable=True)
    best_ask_qty   = Column(Integer,     nullable=True)

    # Exchange timestamp (ms epoch from AngelOne)
    exchange_ts    = Column(BigInteger,  nullable=True)
    timestamp      = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    __table_args__ = (
        # TimescaleDB will convert this to hypertable on migration
        Index("ix_tick_token_ts",    "token", "timestamp"),
        Index("ix_tick_exchange_ts", "exchange", "timestamp"),
    )

    def __repr__(self):
        return f"<Tick {self.symbol} ltp={self.ltp} @ {self.timestamp}>"
