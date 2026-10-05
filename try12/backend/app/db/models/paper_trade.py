"""
Paper trading tables — simulated orders, positions, P&L.
Identical schema to live trading so switching modes is seamless.
"""
from sqlalchemy import (
    Column, BigInteger, Integer, String, Float, Boolean,
    DateTime, Date, Index, Enum as SAEnum, JSON,
)
from sqlalchemy.sql import func
import enum
from app.db.base import Base


class TradingMode(str, enum.Enum):
    PAPER = "PAPER"
    LIVE  = "LIVE"


class SignalDirection(str, enum.Enum):
    BUY  = "BUY"
    SELL = "SELL"


class SignalStatus(str, enum.Enum):
    GENERATED = "GENERATED"   # Signal fired, not yet acted on
    SELECTED  = "SELECTED"    # Option selected for this signal
    ENTERED   = "ENTERED"     # Position taken
    SL_HIT    = "SL_HIT"
    TARGET_HIT = "TARGET_HIT"
    EXPIRED   = "EXPIRED"     # Signal timed out before entry
    SKIPPED   = "SKIPPED"     # Skipped by risk/mode check


class StrategySignal(Base):
    """
    Every signal fired by any strategy.
    Recorded regardless of whether a trade is taken — crucial for study phase.
    """
    __tablename__ = "strategy_signals"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name    = Column(String(50),  nullable=False)
    underlying       = Column(String(20),  nullable=False)
    direction        = Column(SAEnum(SignalDirection), nullable=False)
    status           = Column(SAEnum(SignalStatus), nullable=False, default=SignalStatus.GENERATED)

    # Signal context
    spot_price       = Column(Float,      nullable=False)
    vwap             = Column(Float,      nullable=True)
    signal_time      = Column(DateTime(timezone=True), nullable=False)
    expiry           = Column(Date,       nullable=True)

    # Entry parameters
    suggested_strike = Column(Float,      nullable=True)
    suggested_token  = Column(String(20), nullable=True)
    suggested_symbol = Column(String(60), nullable=True)
    entry_price_ref  = Column(Float,      nullable=True)   # option LTP at signal time
    stop_loss_ref    = Column(Float,      nullable=True)   # option SL
    target_ref       = Column(Float,      nullable=True)   # option target

    # Risk/reward
    risk_reward      = Column(Float,      nullable=True)
    max_loss_pts     = Column(Float,      nullable=True)

    # Human-readable reason when status=SKIPPED (study_phase, risk check, no option, etc.)
    skip_reason      = Column(String(300), nullable=True)

    # Indicator values at signal time (for study)
    indicators       = Column(JSON,       nullable=True)

    mode             = Column(SAEnum(TradingMode), nullable=False, default=TradingMode.PAPER)
    trade_id         = Column(BigInteger, nullable=True)   # FK to paper_positions.id

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_sig_strategy_time",   "strategy_name", "signal_time"),
        Index("ix_sig_underlying_time", "underlying",    "signal_time"),
    )


class PaperPosition(Base):
    """
    Open and closed paper trading positions.
    Simulates real order fills with configurable slippage.
    """
    __tablename__ = "paper_positions"

    id               = Column(BigInteger, primary_key=True, autoincrement=True)
    strategy_name    = Column(String(50),  nullable=True)
    signal_id        = Column(BigInteger,  nullable=True)

    # Instrument
    token            = Column(String(20),  nullable=False)
    symbol           = Column(String(60),  nullable=False)
    underlying       = Column(String(20),  nullable=False)
    strike           = Column(Float,       nullable=True)
    option_type      = Column(String(5),   nullable=True)   # CE / PE
    expiry           = Column(Date,        nullable=True)
    exchange         = Column(String(10),  nullable=False, default="NFO")

    # Position
    direction        = Column(SAEnum(SignalDirection), nullable=False)
    lots             = Column(Integer,  nullable=False, default=1)
    lot_size         = Column(Integer,  nullable=False, default=50)
    quantity         = Column(Integer,  nullable=False)

    # Fill prices (simulated)
    entry_price      = Column(Float,  nullable=False)
    entry_slippage   = Column(Float,  nullable=False, default=0.0)
    actual_entry     = Column(Float,  nullable=False)   # entry + slippage

    # Budget used for this trade (trade_amount from config at entry time)
    trade_amount_used = Column(Float, nullable=True)    # actual ₹ deployed

    # Exit
    exit_price       = Column(Float,  nullable=True)
    exit_slippage    = Column(Float,  nullable=True, default=0.0)
    actual_exit      = Column(Float,  nullable=True)
    exit_reason      = Column(String(20), nullable=True)   # SL/TARGET/MANUAL/EXPIRY/EOD

    # Risk management
    stop_loss        = Column(Float,  nullable=True)
    target           = Column(Float,  nullable=True)
    trailing_sl      = Column(Float,  nullable=True)
    max_loss_per_lot = Column(Float,  nullable=True)

    # Partial exit tracking
    # When position is up 35%, 50% of lots are closed at market.
    # The remaining lots run to the full target with SL moved to breakeven.
    partial_closed        = Column(Boolean, nullable=False, default=False)  # has partial exit fired?
    partial_exit_price    = Column(Float,   nullable=True)   # price at partial close
    partial_exit_qty      = Column(Integer, nullable=True)   # qty closed in partial exit
    partial_pnl           = Column(Float,   nullable=True)   # P&L from partial close
    remaining_quantity    = Column(Integer, nullable=True)   # qty still open after partial exit

    # P&L
    gross_pnl        = Column(Float,  nullable=True)
    brokerage        = Column(Float,  nullable=True, default=0.0)
    net_pnl          = Column(Float,  nullable=True)

    # Regime at entry — set from RegimeState when position is opened
    entry_regime     = Column(String(50), nullable=True)

    # State
    is_open          = Column(Boolean, nullable=False, default=True)
    mode             = Column(SAEnum(TradingMode), nullable=False, default=TradingMode.PAPER)

    entry_time       = Column(DateTime(timezone=True), nullable=False)
    exit_time        = Column(DateTime(timezone=True), nullable=True)
    created_at       = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_pp_strategy_open",   "strategy_name", "is_open"),
        Index("ix_pp_token_open",      "token", "is_open"),
        Index("ix_pp_underlying_date", "underlying", "entry_time"),
    )


class DailyPnLSummary(Base):
    """Aggregated daily P&L per strategy — for performance dashboard."""
    __tablename__ = "daily_pnl_summary"

    id             = Column(Integer, primary_key=True, autoincrement=True)
    date           = Column(Date,       nullable=False)
    strategy_name  = Column(String(50), nullable=False)
    mode           = Column(SAEnum(TradingMode), nullable=False)

    total_trades   = Column(Integer, nullable=False, default=0)
    winning_trades = Column(Integer, nullable=False, default=0)
    losing_trades  = Column(Integer, nullable=False, default=0)
    win_rate       = Column(Float,   nullable=True)

    gross_pnl      = Column(Float,   nullable=False, default=0.0)
    brokerage      = Column(Float,   nullable=False, default=0.0)
    net_pnl        = Column(Float,   nullable=False, default=0.0)

    max_drawdown   = Column(Float,   nullable=True)
    avg_rr         = Column(Float,   nullable=True)   # Average risk:reward achieved
    total_lots     = Column(Integer, nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now())

    __table_args__ = (
        Index("ix_dpnl_date_strategy", "date", "strategy_name", "mode", unique=True),
    )


class TradingConfig(Base):
    """
    Runtime configuration: mode, risk limits, strategy enable/disable.
    Single row — updated via API.
    """
    __tablename__ = "trading_config"

    id                    = Column(Integer, primary_key=True, default=1)
    mode                  = Column(SAEnum(TradingMode), nullable=False, default=TradingMode.PAPER)

    # Risk limits (per day)
    max_daily_loss        = Column(Float,   nullable=False, default=5000.0)
    max_daily_loss_pct    = Column(Float,   nullable=False, default=2.0)    # % of capital
    max_open_positions    = Column(Integer, nullable=False, default=6)
    max_lots_per_trade    = Column(Integer, nullable=False, default=5)
    capital               = Column(Float,   nullable=False, default=100000.0)

    # Slippage simulation (paper mode)
    slippage_pct          = Column(Float,   nullable=False, default=0.2)    # 0.2% of LTP

    # ── Capital / trade sizing ──────────────────────────────────────────────
    # Amount in ₹ to deploy per trade (lots calculated from this)
    trade_amount          = Column(Float,   nullable=False, default=10000.0)
    # Running total of capital currently locked in open positions
    used_capital          = Column(Float,   nullable=False, default=0.0)
    # Hard cap on trades per day (resets midnight)
    max_trades_per_day    = Column(Integer, nullable=False, default=10)
    # Counter incremented on every new trade entry, reset daily
    daily_trades_count    = Column(Integer, nullable=False, default=0)

    # Strategy enable flags (JSON: {strategy_name: bool})
    strategy_flags        = Column(JSON,    nullable=True)

    # Safety: auto square-off time
    square_off_time       = Column(String(5), nullable=False, default="15:15")

    # Live mode guard: must be explicitly set to True to go live
    live_mode_confirmed   = Column(Boolean,  nullable=False, default=False)

    # study_phase: always False — paper trading is active immediately.
    # Kept in schema for API compatibility; cannot be set to True via normal config updates.
    study_phase           = Column(Boolean,  nullable=False, default=False)

    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())


class StrategyPerf(Base):
    """
    Rolling performance stats per strategy — used by half-Kelly sizing.

    Updated after every closed position (paper or live).
    Stores a trailing window of the last N trades so the Kelly fraction
    adapts as the strategy's recent win rate changes.

    Half-Kelly fraction:
        f = 0.5 × max(0, (p × b − q) / b)
    where:
        p = win_rate / 100
        q = 1 − p
        b = avg_rr   (average achieved risk:reward)

    trade_amount = clamp(f × capital, KELLY_MIN_AMOUNT, KELLY_MAX_AMOUNT)

    Only applied when total_trades >= KELLY_MIN_TRADES (20) — below that
    threshold the system falls back to cfg.trade_amount (fixed sizing).
    """
    __tablename__ = "strategy_perf"

    id            = Column(Integer,     primary_key=True, autoincrement=True)
    strategy_name = Column(String(50),  nullable=False, unique=True, index=True)

    # Rolling counters (all-time, not daily)
    total_trades   = Column(Integer, nullable=False, default=0)
    winning_trades = Column(Integer, nullable=False, default=0)

    # Running averages
    win_rate       = Column(Float, nullable=True)   # 0–100
    avg_rr         = Column(Float, nullable=True)   # average achieved R:R

    # Computed half-Kelly fraction (cached — avoids recomputing on every signal)
    kelly_fraction = Column(Float, nullable=True)   # 0.0–0.25 (half-Kelly, capped)

    updated_at = Column(DateTime(timezone=True), onupdate=func.now(), server_default=func.now())
