"""
Regime detection tables — state history, performance tracking, and configuration.

Three tables:
  regime_states       — persisted regime classification snapshots (one per candle)
  regime_performance  — rolling P&L and win-rate statistics per regime/strategy pair
  regime_config       — tunable thresholds for the regime detector (per-underlying or global)
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Boolean,
    DateTime, Index, UniqueConstraint, Numeric,
)
from sqlalchemy.sql import func

from app.db.base import Base


class RegimeStateModel(Base):
    """
    One row per regime classification event.
    Written by Regime_Persistence.persist_to_db() after every candle.
    """
    __tablename__ = "regime_states"

    id                  = Column(BigInteger, primary_key=True, autoincrement=True)
    underlying          = Column(String(20),  nullable=False)
    regime              = Column(String(50),  nullable=False)
    timestamp           = Column(DateTime(timezone=True), nullable=False, server_default=func.now())

    # Indicator snapshot at classification time
    adx                 = Column(Numeric(6,  2), nullable=True)
    atr                 = Column(Numeric(8,  2), nullable=True)
    atr_percentile      = Column(Numeric(5,  2), nullable=True)
    vwap                = Column(Numeric(10, 2), nullable=True)
    price               = Column(Numeric(10, 2), nullable=True)
    vwap_cross_count    = Column(Integer,         nullable=True)
    volume              = Column(BigInteger,       nullable=True)
    avg_volume          = Column(Numeric(12, 2),  nullable=True)

    # Transition metadata
    previous_regime     = Column(String(50),  nullable=True)
    transition_detected = Column(Boolean,      nullable=False, default=False)

    __table_args__ = (
        UniqueConstraint("underlying", "timestamp", name="regime_states_underlying_timestamp_key"),
        Index("idx_regime_states_underlying_timestamp", "underlying", "timestamp"),
        Index("idx_regime_states_regime", "regime"),
    )

    def __repr__(self) -> str:
        return (
            f"<RegimeStateModel {self.underlying} {self.regime} "
            f"adx={self.adx} ts={self.timestamp}>"
        )


class RegimePerformanceModel(Base):
    """
    Rolling P&L and win-rate per (regime, underlying, strategy_name) triplet.
    Updated by Strategy_Orchestrator after every closed position.
    """
    __tablename__ = "regime_performance"

    id              = Column(Integer,     primary_key=True, autoincrement=True)
    regime          = Column(String(50),  nullable=False)
    underlying      = Column(String(20),  nullable=False)
    strategy_name   = Column(String(50),  nullable=False)

    # Performance counters
    total_trades    = Column(Integer,      nullable=False, default=0)
    winning_trades  = Column(Integer,      nullable=False, default=0)
    losing_trades   = Column(Integer,      nullable=False, default=0)

    # P&L aggregates
    total_pnl       = Column(Numeric(12, 2), nullable=False, default=0)
    avg_pnl         = Column(Numeric(10, 2), nullable=True)
    win_rate        = Column(Numeric(5,  2), nullable=True)
    avg_rr          = Column(Numeric(6,  3), nullable=True)

    # Timestamps
    first_trade_at  = Column(DateTime(timezone=True), nullable=True)
    last_trade_at   = Column(DateTime(timezone=True), nullable=True)
    updated_at      = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint(
            "regime", "underlying", "strategy_name",
            name="regime_performance_unique",
        ),
        Index("idx_regime_performance_regime",     "regime"),
        Index("idx_regime_performance_underlying", "underlying"),
    )

    def __repr__(self) -> str:
        return (
            f"<RegimePerformanceModel {self.regime}/{self.underlying}/"
            f"{self.strategy_name} trades={self.total_trades} pnl={self.total_pnl}>"
        )


class RegimeConfigModel(Base):
    """
    Tunable thresholds for the Regime_Detector.
    One row per underlying (NULL underlying = global defaults).
    Seeded with a single global default row by the Alembic migration.
    """
    __tablename__ = "regime_config"

    id                          = Column(Integer,  primary_key=True, autoincrement=True)
    # NULL means this row is the global default; a value scopes it to one underlying
    underlying                  = Column(String(20), nullable=True)

    # ADX thresholds
    adx_trend_threshold         = Column(Numeric(5, 2), nullable=False, default=22.0)
    adx_range_threshold         = Column(Numeric(5, 2), nullable=False, default=18.0)

    # ATR percentile thresholds
    atr_low_percentile          = Column(Numeric(5, 2), nullable=False, default=30.0)
    atr_high_percentile         = Column(Numeric(5, 2), nullable=False, default=70.0)

    # VWAP thresholds
    vwap_proximity_percent      = Column(Numeric(5, 3), nullable=False, default=0.2)
    vwap_cross_chop_threshold   = Column(Integer,        nullable=False, default=8)

    # Volume / transition settings
    volume_surge_multiplier     = Column(Numeric(4, 2), nullable=False, default=1.5)
    regime_transition_window    = Column(Integer,        nullable=False, default=2)
    transition_cooldown_candles = Column(Integer,        nullable=False, default=3)

    # Metadata
    is_active   = Column(Boolean,  nullable=False, default=True)
    created_at  = Column(DateTime(timezone=True), nullable=False, server_default=func.now())
    updated_at  = Column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        UniqueConstraint("underlying", name="regime_config_underlying_key"),
    )

    def __repr__(self) -> str:
        scope = self.underlying or "GLOBAL"
        return f"<RegimeConfigModel {scope} active={self.is_active}>"
