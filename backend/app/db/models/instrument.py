"""
Instruments master table.
Synced daily from AngelOne OpenAPI scrip master JSON dump.
Covers Nifty/BankNifty CE/PE options, futures, and spot index.
"""
from sqlalchemy import (
    Column, String, Integer, Float, Date, DateTime, Boolean,
    Index, UniqueConstraint, Enum as SAEnum,
)
from sqlalchemy.sql import func
import enum

from app.db.base import Base


class InstrumentType(str, enum.Enum):
    OPTIDX = "OPTIDX"   # Index option  (Nifty/BankNifty CE/PE)
    FUTIDX = "FUTIDX"   # Index future
    INDEX  = "INDEX"    # Spot index
    OPTSTK = "OPTSTK"   # Stock option
    FUTSTK = "FUTSTK"   # Stock future
    EQ     = "EQ"       # Equity cash


class OptionType(str, enum.Enum):
    CE = "CE"
    PE = "PE"


class Instrument(Base):
    __tablename__ = "instruments"

    id              = Column(Integer, primary_key=True, autoincrement=True)
    token           = Column(String(20),  nullable=False)           # AngelOne symbol token
    symbol          = Column(String(60),  nullable=False)           # e.g. NIFTY29AUG24C24000
    name            = Column(String(30),  nullable=False)           # Underlying: NIFTY, BANKNIFTY
    expiry          = Column(Date,        nullable=True)
    strike          = Column(Float,       nullable=True)
    option_type     = Column(SAEnum(OptionType),      nullable=True)
    instrument_type = Column(SAEnum(InstrumentType),  nullable=False)
    lot_size        = Column(Integer,     nullable=False, default=50)
    tick_size       = Column(Float,       nullable=False, default=0.05)
    exchange        = Column(String(10),  nullable=False, default="NFO")
    is_active       = Column(Boolean,     nullable=False, default=True)
    created_at      = Column(DateTime(timezone=True), server_default=func.now())
    updated_at      = Column(DateTime(timezone=True), onupdate=func.now())

    __table_args__ = (
        UniqueConstraint("token", "exchange", name="uq_token_exchange"),
        Index("ix_inst_name_expiry",   "name", "expiry"),
        Index("ix_inst_type_active",   "instrument_type", "is_active"),
        Index("ix_inst_strike_optype", "strike", "option_type"),
    )

    def __repr__(self):
        return f"<Instrument {self.symbol} [{self.token}]>"
