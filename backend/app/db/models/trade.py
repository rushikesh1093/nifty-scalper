"""
Trades table — completed round-trip trades (entry + exit pair).
Derived from orders; used for P&L reporting and strategy performance analytics.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Boolean,
    DateTime, Index, Enum as SAEnum,
)
from sqlalchemy.sql import func
import enum

from app.db.base import Base


class TradeSide(str, enum.Enum):
    LONG  = "LONG"
    SHORT = "SHORT"


class Trade(Base):
    __tablename__ = "trades"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name    = Column(String(50),  nullable=True)
    token            = Column(String(20),  nullable=False)
    symbol           = Column(String(60),  nullable=False)
    exchange         = Column(String(10),  nullable=False, default="NFO")
    side             = Column(SAEnum(TradeSide), nullable=False)

    quantity         = Column(Integer, nullable=False)
    lot_size         = Column(Integer, nullable=False, default=50)
    lots             = Column(Integer, nullable=False, default=1)

    entry_price      = Column(Float,   nullable=False)
    exit_price       = Column(Float,   nullable=True)
    stop_loss        = Column(Float,   nullable=True)
    target           = Column(Float,   nullable=True)

    # P&L fields
    gross_pnl        = Column(Float,   nullable=True)   # (exit - entry) * qty
    brokerage        = Column(Float,   nullable=True, default=0.0)
    net_pnl          = Column(Float,   nullable=True)   # gross - brokerage

    entry_order_id   = Column(BigInteger, nullable=True)   # FK to orders.id
    exit_order_id    = Column(BigInteger, nullable=True)

    is_paper         = Column(Boolean, nullable=False, default=False)
    is_open          = Column(Boolean, nullable=False, default=True)   # Position still open

    entry_time       = Column(DateTime(timezone=True), nullable=False)
    exit_time        = Column(DateTime(timezone=True), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_trade_strategy_entry",  "strategy_name", "entry_time"),
        Index("ix_trade_token_open",      "token", "is_open"),
    )

    def __repr__(self):
        return (
            f"<Trade {self.side} {self.symbol} entry={self.entry_price} "
            f"exit={self.exit_price} pnl={self.net_pnl}>"
        )
