"""
Paper multi-leg option spreads (app.services.trading.iron_fly).

A separate book from paper_positions: a spread's legs open and close together,
and it never touches the single-leg engine's capital, risk gate, Kelly sizing or
metrics. One row per spread; the legs live in ``legs`` (JSON list, one dict per
leg: token, symbol, strike, option_type, side "SELL"/"BUY", entry_price,
exit_price, and the bid/ask seen at entry and exit).
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Date, DateTime, JSON, UniqueConstraint,
)
from sqlalchemy.sql import func

from app.db.base import Base


class PaperSpread(Base):
    __tablename__ = "paper_spreads"
    __table_args__ = (
        # one spread per strategy per day: a restart can never open a second one
        UniqueConstraint("strategy_name", "trading_day", name="uq_paper_spread_strategy_day"),
    )

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name   = Column(String(50), nullable=False)
    trading_day     = Column(Date,       nullable=False)
    underlying      = Column(String(20), nullable=False)
    expiry          = Column(Date,       nullable=False)
    status          = Column(String(12), nullable=False)        # OPEN / CLOSED / UNSETTLED

    lots            = Column(Integer, nullable=False)
    lot_size        = Column(Integer, nullable=False)
    legs            = Column(JSON,    nullable=False)

    spot_entry      = Column(Float, nullable=False)
    spot_exit       = Column(Float, nullable=True)
    credit_pts      = Column(Float, nullable=False)             # net premium received per unit
    max_loss        = Column(Float, nullable=False)             # Rs: (wing - credit) x quantity

    entry_time      = Column(DateTime(timezone=True), nullable=False)
    exit_time       = Column(DateTime(timezone=True), nullable=True)
    exit_reason     = Column(String(20), nullable=True)         # TIME_EXIT / EXPIRY_SETTLE / UNSETTLED

    gross_pnl       = Column(Float, nullable=True)
    brokerage       = Column(Float, nullable=True)
    statutory_charges = Column(Float, nullable=True)
    net_pnl         = Column(Float, nullable=True)
    r_multiple      = Column(Float, nullable=True)              # net_pnl / max_loss

    created_at      = Column(DateTime(timezone=True), server_default=func.now())
