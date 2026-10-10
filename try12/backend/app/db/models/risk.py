"""
Risk and observability tables (migration 0012, design §11-§12).

  no_trade_decisions — one row per explicit no-trade decision or recorded degradation
  risk_halts         — engaged/released trading halts, one per (run, day, type, scope)
  position_events    — trailing advances, partial exits, stale deferrals per position
  cycle_stats        — positive evidence that the per-candle cycle evaluated (R12.8)

Every metric column is Float or Integer, never Numeric (§7).
"""
from sqlalchemy import (
    BigInteger, Column, Date, DateTime, Float, Index, Integer, JSON, String, Text,
    UniqueConstraint, text,
)

from app.db.base import Base

DEGRADATION_ONCE_WHERE = (
    "stage = 'DEGRADATION' AND code = 'MTM_DEGRADED' AND position_id IS NOT NULL"
)


class NoTradeDecision(Base):
    __tablename__ = "no_trade_decisions"

    id            = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id        = Column(String(32), nullable=False)
    trading_day   = Column(Date, nullable=False)
    decided_at    = Column(DateTime(timezone=True), nullable=False)
    stage         = Column(String(32), nullable=False)     # no_trade.Stage
    code          = Column(String(48), nullable=False)     # no_trade.ReasonCode
    underlying    = Column(String(20), nullable=True)
    strategy_name = Column(String(50), nullable=True)
    signal_id     = Column(BigInteger, nullable=True)
    position_id   = Column(BigInteger, nullable=True)      # DEGRADATION dedupe key
    reason        = Column(Text, nullable=False)           # untruncated (R12.4)
    context       = Column(JSON, nullable=False)

    __table_args__ = (
        Index("ix_ntd_run_day", "run_id", "trading_day"),
        Index("ix_ntd_code", "code"),
        Index("ix_ntd_stage", "stage"),
        Index("ix_ntd_strategy_day", "strategy_name", "trading_day"),
        # Backstop for record_degradation_once: one MTM_DEGRADED row per
        # (run, day, position). Narrower than §11's "stage = 'DEGRADATION'"
        # wording on purpose: FALLBACK_FILL is one row per fallback fill, and
        # one position can have two (entry and exit leg, §12).
        Index(
            "uq_ntd_degradation_once", "run_id", "trading_day", "code", "position_id",
            unique=True,
            postgresql_where=text(DEGRADATION_ONCE_WHERE),
            sqlite_where=text(DEGRADATION_ONCE_WHERE),
        ),
    )


class RiskHalt(Base):
    __tablename__ = "risk_halts"

    id           = Column(Integer, primary_key=True, autoincrement=True)
    run_id       = Column(String(32), nullable=False)
    trading_day  = Column(Date, nullable=False)
    halt_type    = Column(String(24), nullable=False)   # DAILY_LOSS | LOSING_STREAK | STRATEGY_STREAK | MANUAL
    scope        = Column(String(50), nullable=True)    # strategy name for STRATEGY_STREAK
    reason       = Column(Text, nullable=True)
    context      = Column(JSON, nullable=True)
    engaged_at   = Column(DateTime(timezone=True), nullable=True)
    released_at  = Column(DateTime(timezone=True), nullable=True)
    released_by  = Column(String(50), nullable=True)
    release_note = Column(Text, nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "trading_day", "halt_type", "scope", name="uq_risk_halts_key"),
    )


class PositionEvent(Base):
    __tablename__ = "position_events"

    id          = Column(BigInteger, primary_key=True, autoincrement=True)
    position_id = Column(BigInteger, nullable=False)
    run_id      = Column(String(32), nullable=False, default="PAPER", server_default="PAPER")
    event_type  = Column(String(32), nullable=False)   # TRAIL_ADVANCE | PARTIAL_EXIT | ...
    occurred_at = Column(DateTime(timezone=True), nullable=False)
    price       = Column(Float, nullable=True)
    from_value  = Column(Float, nullable=True)
    to_value    = Column(Float, nullable=True)
    tier_label  = Column(String(24), nullable=True)
    detail      = Column(JSON, nullable=True)

    __table_args__ = (
        Index("ix_pe_position_time", "position_id", "occurred_at"),
    )


class CycleStats(Base):
    __tablename__ = "cycle_stats"

    id                    = Column(BigInteger, primary_key=True, autoincrement=True)
    run_id                = Column(String(32), nullable=False)
    trading_day           = Column(Date, nullable=False)
    underlying            = Column(String(20), nullable=False)
    candles_evaluated     = Column(Integer, nullable=False, default=0, server_default="0")
    strategies_evaluated  = Column(Integer, nullable=False, default=0, server_default="0")
    signals_emitted       = Column(Integer, nullable=False, default=0, server_default="0")
    rejections_recorded   = Column(Integer, nullable=False, default=0, server_default="0")
    oi_iv_context_missing = Column(Integer, nullable=False, default=0, server_default="0")
    first_cycle_at        = Column(DateTime(timezone=True), nullable=True)
    last_cycle_at         = Column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        UniqueConstraint("run_id", "trading_day", "underlying", name="uq_cycle_stats_key"),
    )
