"""
Orders table — tracks every order placed/modified/cancelled via AngelOne.
Supports paper trading mode (is_paper=True) for strategy testing.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Boolean,
    DateTime, Index, Enum as SAEnum,
)
from sqlalchemy.sql import func
import enum

from app.db.base import Base


class OrderSide(str, enum.Enum):
    BUY  = "BUY"
    SELL = "SELL"


class OrderStatus(str, enum.Enum):
    PENDING    = "PENDING"
    OPEN       = "OPEN"
    COMPLETE   = "COMPLETE"
    CANCELLED  = "CANCELLED"
    REJECTED   = "REJECTED"


class OrderType(str, enum.Enum):
    MARKET = "MARKET"
    LIMIT  = "LIMIT"
    SL     = "SL"        # Stop Loss Market
    SL_M   = "SL-M"      # Stop Loss Market


class ProductType(str, enum.Enum):
    INTRADAY = "INTRADAY"   # MIS
    DELIVERY = "DELIVERY"   # CNC/NRML


class Order(Base):
    __tablename__ = "orders"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name   = Column(String(50),  nullable=True)          # Which strategy placed this
    broker_order_id = Column(String(50),  nullable=True, index=True)  # AngelOne order ID
    token           = Column(String(20),  nullable=False)
    symbol          = Column(String(60),  nullable=False)
    exchange        = Column(String(10),  nullable=False, default="NFO")

    side            = Column(SAEnum(OrderSide),    nullable=False)
    order_type      = Column(SAEnum(OrderType),    nullable=False)
    product_type    = Column(SAEnum(ProductType),  nullable=False, default=ProductType.INTRADAY)
    status          = Column(SAEnum(OrderStatus),  nullable=False, default=OrderStatus.PENDING)

    quantity        = Column(Integer, nullable=False)
    price           = Column(Float,   nullable=True)    # Limit price (None for MARKET)
    trigger_price   = Column(Float,   nullable=True)    # For SL orders
    avg_price       = Column(Float,   nullable=True)    # Fill price
    filled_qty      = Column(Integer, nullable=True, default=0)

    # P&L tracking
    pnl             = Column(Float,   nullable=True)

    is_paper        = Column(Boolean, nullable=False, default=False)  # Paper trade flag

    placed_at       = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())
    filled_at       = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index("ix_order_strategy_status", "strategy_name", "status"),
        Index("ix_order_token_placed",    "token", "placed_at"),
    )

    def __repr__(self):
        return f"<Order {self.side} {self.symbol} qty={self.quantity} status={self.status}>"
