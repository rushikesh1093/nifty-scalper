"""
replay_runs (migration 0012, design §11, §14.6): one row per replay run,
carrying the frozen params, their hash, the seed and the code version so a
run can be reproduced and its validation trail audited.
"""
from sqlalchemy import BigInteger, Column, Date, DateTime, Index, JSON, String, Text

from app.db.base import Base


class ReplayRun(Base):
    __tablename__ = "replay_runs"

    run_id       = Column(String(32), primary_key=True)
    created_at   = Column(DateTime(timezone=True), nullable=True)
    date_from    = Column(Date, nullable=True)
    date_to      = Column(Date, nullable=True)
    underlyings  = Column(JSON, nullable=True)
    params       = Column(JSON, nullable=True)
    params_hash  = Column(String(64), nullable=True)     # sha256 of canonical_json(params)
    seed         = Column(BigInteger, nullable=True)
    code_version = Column(String(64), nullable=True)     # git describe --always --dirty
    status       = Column(String(16), nullable=True)     # RUNNING | COMPLETE | FAILED
    coverage     = Column(JSON, nullable=True)
    metrics      = Column(JSON, nullable=True)
    validation   = Column(JSON, nullable=True)
    notes        = Column(Text, nullable=True)

    __table_args__ = (
        # Supports the §18 item 9 overlap query: date_from <= :to AND date_to >= :from
        Index("ix_replay_runs_status_window", "status", "date_from", "date_to"),
    )
