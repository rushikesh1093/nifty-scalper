"""Risk, observability and replay tables

no_trade_decisions — explicit no-trade decisions and recorded degradations (§12)
risk_halts         — engaged/released halts per (run, day, type, scope)
position_events    — trail advances, partial exits, stale deferrals
cycle_stats        — positive evidence of per-candle evaluation (R12.8)
replay_runs        — one row per replay run (params, hash, seed, code version)

Revision ID: 0012
Revises: 0011
Create Date: 2026-10-20 00:00:01

Purely additive; downgrade() drops the five tables.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0012"
down_revision: Union[str, None] = "0011"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# Bounds MTM_DEGRADED to one row per position per day. Deliberately narrower
# than "every DEGRADATION": FALLBACK_FILL is one row per fill, and a position
# can have two (entry and exit leg, design §12). Kept in sync with
# app/db/models/risk.py DEGRADATION_ONCE_WHERE (migrations never import app code).
_DEGRADATION_ONCE = "stage = 'DEGRADATION' AND code = 'MTM_DEGRADED' AND position_id IS NOT NULL"


def upgrade() -> None:
    op.create_table(
        "no_trade_decisions",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("stage", sa.String(32), nullable=False),
        sa.Column("code", sa.String(48), nullable=False),
        sa.Column("underlying", sa.String(20), nullable=True),
        sa.Column("strategy_name", sa.String(50), nullable=True),
        sa.Column("signal_id", sa.BigInteger(), nullable=True),
        sa.Column("position_id", sa.BigInteger(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("context", sa.JSON(), nullable=False),
    )
    op.create_index("ix_ntd_run_day", "no_trade_decisions", ["run_id", "trading_day"])
    op.create_index("ix_ntd_code", "no_trade_decisions", ["code"])
    op.create_index("ix_ntd_stage", "no_trade_decisions", ["stage"])
    op.create_index("ix_ntd_strategy_day", "no_trade_decisions", ["strategy_name", "trading_day"])
    # Partial: ordinary rejections legitimately repeat and are not constrained.
    op.create_index(
        "uq_ntd_degradation_once", "no_trade_decisions",
        ["run_id", "trading_day", "code", "position_id"], unique=True,
        postgresql_where=sa.text(_DEGRADATION_ONCE),
        sqlite_where=sa.text(_DEGRADATION_ONCE),
    )

    op.create_table(
        "risk_halts",
        sa.Column("id", sa.Integer(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("halt_type", sa.String(24), nullable=False),
        sa.Column("scope", sa.String(50), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("context", sa.JSON(), nullable=True),
        sa.Column("engaged_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("released_by", sa.String(50), nullable=True),
        sa.Column("release_note", sa.Text(), nullable=True),
        sa.UniqueConstraint("run_id", "trading_day", "halt_type", "scope",
                            name="uq_risk_halts_key"),
    )

    op.create_table(
        "position_events",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("position_id", sa.BigInteger(), nullable=False),
        sa.Column("run_id", sa.String(32), nullable=False, server_default="PAPER"),
        sa.Column("event_type", sa.String(32), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("price", sa.Float(), nullable=True),
        sa.Column("from_value", sa.Float(), nullable=True),
        sa.Column("to_value", sa.Float(), nullable=True),
        sa.Column("tier_label", sa.String(24), nullable=True),
        sa.Column("detail", sa.JSON(), nullable=True),
    )
    op.create_index("ix_pe_position_time", "position_events", ["position_id", "occurred_at"])

    op.create_table(
        "cycle_stats",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("underlying", sa.String(20), nullable=False),
        sa.Column("candles_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("strategies_evaluated", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("signals_emitted", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("rejections_recorded", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("oi_iv_context_missing", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("first_cycle_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_cycle_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "trading_day", "underlying", name="uq_cycle_stats_key"),
    )

    op.create_table(
        "replay_runs",
        sa.Column("run_id", sa.String(32), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("date_from", sa.Date(), nullable=True),
        sa.Column("date_to", sa.Date(), nullable=True),
        sa.Column("underlyings", sa.JSON(), nullable=True),
        sa.Column("params", sa.JSON(), nullable=True),
        sa.Column("params_hash", sa.String(64), nullable=True),
        sa.Column("seed", sa.BigInteger(), nullable=True),
        sa.Column("code_version", sa.String(64), nullable=True),
        sa.Column("status", sa.String(16), nullable=True),
        sa.Column("coverage", sa.JSON(), nullable=True),
        sa.Column("metrics", sa.JSON(), nullable=True),
        sa.Column("validation", sa.JSON(), nullable=True),
        sa.Column("notes", sa.Text(), nullable=True),
    )
    op.create_index("ix_replay_runs_status_window", "replay_runs",
                    ["status", "date_from", "date_to"])


def downgrade() -> None:
    op.drop_index("ix_replay_runs_status_window", table_name="replay_runs")
    op.drop_table("replay_runs")
    op.drop_table("cycle_stats")
    op.drop_index("ix_pe_position_time", table_name="position_events")
    op.drop_table("position_events")
    op.drop_table("risk_halts")
    for name in ("uq_ntd_degradation_once", "ix_ntd_strategy_day", "ix_ntd_stage",
                 "ix_ntd_code", "ix_ntd_run_day"):
        op.drop_index(name, table_name="no_trade_decisions")
    op.drop_table("no_trade_decisions")
