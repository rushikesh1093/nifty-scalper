"""
Market study tables — 20-22 day data collection period.
Rich daily + intraday features for strategy research and backtesting.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Date, DateTime,
    Boolean, Index, JSON,
)
from sqlalchemy.sql import func
from app.db.base import Base


class MarketDailySnapshot(Base):
    """One row per underlying per day — EOD summary."""
    __tablename__ = "market_daily_snapshots"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    date             = Column(Date,        nullable=False)
    underlying       = Column(String(20),  nullable=False)   # NIFTY / BANKNIFTY

    # Spot OHLC
    open             = Column(Float, nullable=True)
    high             = Column(Float, nullable=True)
    low              = Column(Float, nullable=True)
    close            = Column(Float, nullable=True)
    prev_close       = Column(Float, nullable=True)
    change_pct       = Column(Float, nullable=True)

    # Volatility
    india_vix        = Column(Float, nullable=True)
    atm_iv           = Column(Float, nullable=True)
    iv_percentile    = Column(Float, nullable=True)   # IV rank 0-100 vs 52w
    hv_20            = Column(Float, nullable=True)   # 20-day historical vol

    # OI / PCR
    total_ce_oi      = Column(BigInteger, nullable=True)
    total_pe_oi      = Column(BigInteger, nullable=True)
    pcr_oi           = Column(Float,      nullable=True)
    pcr_volume       = Column(Float,      nullable=True)
    max_pain         = Column(Float,      nullable=True)
    max_oi_ce_strike = Column(Float,      nullable=True)
    max_oi_pe_strike = Column(Float,      nullable=True)

    # Trend context
    trend            = Column(String(10), nullable=True)   # BULLISH/BEARISH/SIDEWAYS
    atm_strike       = Column(Float,      nullable=True)
    nearest_expiry   = Column(Date,       nullable=True)
    days_to_expiry   = Column(Integer,    nullable=True)

    # Key intraday levels
    orb_high         = Column(Float, nullable=True)   # Opening range (9:15-9:30)
    orb_low          = Column(Float, nullable=True)
    vwap_close       = Column(Float, nullable=True)   # VWAP at market close
    pdh              = Column(Float, nullable=True)   # Previous day high
    pdl              = Column(Float, nullable=True)   # Previous day low

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_mds_underlying_date", "underlying", "date", unique=True),
    )


class IntradayMarketFeature(Base):
    """
    Intraday snapshots every 5 minutes during market hours.
    Captures microstructure data for strategy pattern study.
    """
    __tablename__ = "intraday_market_features"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    snapshot_time    = Column(DateTime(timezone=True), nullable=False)
    underlying       = Column(String(20), nullable=False)

    # Spot
    spot_ltp         = Column(Float, nullable=True)
    spot_change_pct  = Column(Float, nullable=True)
    vwap             = Column(Float, nullable=True)
    spot_vs_vwap_pct = Column(Float, nullable=True)   # % above/below VWAP

    # ATM options
    atm_strike       = Column(Float, nullable=True)
    atm_ce_ltp       = Column(Float, nullable=True)
    atm_pe_ltp       = Column(Float, nullable=True)
    atm_ce_iv        = Column(Float, nullable=True)
    atm_pe_iv        = Column(Float, nullable=True)
    atm_straddle_val = Column(Float, nullable=True)
    atm_ce_spread    = Column(Float, nullable=True)
    atm_pe_spread    = Column(Float, nullable=True)
    atm_ce_volume    = Column(BigInteger, nullable=True)
    atm_pe_volume    = Column(BigInteger, nullable=True)

    # OI flow
    ce_oi_change     = Column(BigInteger, nullable=True)
    pe_oi_change     = Column(BigInteger, nullable=True)
    pcr_oi           = Column(Float,      nullable=True)

    # Momentum
    spot_5m_return   = Column(Float, nullable=True)
    spot_15m_return  = Column(Float, nullable=True)
    vix_snapshot     = Column(Float, nullable=True)

    # Technical levels at this moment
    ema9             = Column(Float, nullable=True)
    ema21            = Column(Float, nullable=True)
    rsi14            = Column(Float, nullable=True)
    supertrend       = Column(Float, nullable=True)
    supertrend_dir   = Column(String(5), nullable=True)   # UP/DOWN
    bb_upper         = Column(Float, nullable=True)
    bb_lower         = Column(Float, nullable=True)
    bb_mid           = Column(Float, nullable=True)

    # ORB context
    orb_high         = Column(Float, nullable=True)
    orb_low          = Column(Float, nullable=True)
    orb_broken       = Column(String(5), nullable=True)   # UP/DOWN/NONE

    # Full chain JSON for offline analysis
    chain_snapshot   = Column(JSON, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_imf_underlying_ts", "underlying", "snapshot_time"),
    )


class VIXData(Base):
    """India VIX daily OHLC."""
    __tablename__ = "vix_data"

    id         = Column(Integer, primary_key=True, autoincrement=True)
    date       = Column(Date,  nullable=False, unique=True)
    open       = Column(Float, nullable=True)
    high       = Column(Float, nullable=True)
    low        = Column(Float, nullable=True)
    close      = Column(Float, nullable=True)
    change_pct = Column(Float, nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now())
