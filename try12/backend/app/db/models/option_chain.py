"""
Option chain snapshot table.
Stores periodic snapshots of the full Nifty option chain (all strikes, CE+PE).
Used for IV surface, PCR calculation, max pain, and strategy signal generation.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Date, DateTime,
    Index, Enum as SAEnum,
)
from sqlalchemy.sql import func
import enum

from app.db.base import Base


class OptionSide(str, enum.Enum):
    CE = "CE"
    PE = "PE"


class OptionChainSnapshot(Base):
    __tablename__ = "option_chain_snapshots"

    id              = Column(BigInteger, primary_key=True, autoincrement=True)
    underlying      = Column(String(20), nullable=False)     # NIFTY / BANKNIFTY
    underlying_ltp  = Column(Float,      nullable=False)     # Spot price at snapshot time
    expiry          = Column(Date,       nullable=False)
    strike          = Column(Float,      nullable=False)
    option_type     = Column(SAEnum(OptionSide), nullable=False)
    token           = Column(String(20), nullable=False)

    # Price
    ltp             = Column(Float,      nullable=True)
    bid             = Column(Float,      nullable=True)
    ask             = Column(Float,      nullable=True)
    iv              = Column(Float,      nullable=True)      # Implied volatility (%)

    # Greeks (calculated or from feed)
    delta           = Column(Float,      nullable=True)
    gamma           = Column(Float,      nullable=True)
    theta           = Column(Float,      nullable=True)
    vega            = Column(Float,      nullable=True)

    # OI & volume
    oi              = Column(BigInteger, nullable=True)
    oi_change       = Column(BigInteger, nullable=True)
    volume          = Column(BigInteger, nullable=True)

    # Snapshot time
    snapshot_time   = Column(DateTime(timezone=True), nullable=False)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_oc_underlying_expiry_ts",  "underlying", "expiry", "snapshot_time"),
        Index("ix_oc_strike_type_ts",        "strike", "option_type", "snapshot_time"),
        Index("ix_oc_token_ts",              "token", "snapshot_time"),
    )

    def __repr__(self):
        return (
            f"<OptionChain {self.underlying} {self.strike}{self.option_type} "
            f"ltp={self.ltp} iv={self.iv}>"
        )
