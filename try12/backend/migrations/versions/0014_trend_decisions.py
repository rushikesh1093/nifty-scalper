"""trend_decisions research telemetry

trend_decisions — one row per (run, underlying, completed candle): the unified
                  engine's decision, NO TRADE reason code, evidence score,
                  regime, runner gate, and the underlying price +15/+30/+60 min.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-21 00:00:00

Purely additive and idempotent: the telemetry writer also creates this table on
first use (checkfirst), so upgrade() skips it when it already exists.
downgrade() drops it.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

TABLE = "trend_decisions"


def upgrade() -> None:
    if sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("run_id", sa.String(32), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("underlying", sa.String(20), nullable=False),
        sa.Column("candle_ts", sa.DateTime(timezone=True), nullable=False),
        sa.Column("evaluated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("decision", sa.String(10), nullable=False),
        sa.Column("reason_code", sa.String(40), nullable=True),
        sa.Column("reason", sa.String(400), nullable=True),
        sa.Column("trend", sa.String(12), nullable=True),
        sa.Column("direction", sa.Integer(), nullable=False),
        sa.Column("strength", sa.String(10), nullable=True),
        sa.Column("score", sa.Integer(), nullable=True),
        sa.Column("entry_trigger", sa.String(80), nullable=True),
        sa.Column("gate", sa.String(48), nullable=True),
        sa.Column("engine_evaluated", sa.Boolean(), nullable=False),
        sa.Column("signal_emitted", sa.Boolean(), nullable=False),
        sa.Column("regime", sa.String(40), nullable=True),
        sa.Column("atr_percentile", sa.Float(), nullable=True),
        sa.Column("spot", sa.Float(), nullable=True),
        sa.Column("vwap", sa.Float(), nullable=True),
        sa.Column("atr", sa.Float(), nullable=True),
        sa.Column("stop_points", sa.Float(), nullable=True),
        sa.Column("target_points", sa.Float(), nullable=True),
        sa.Column("features", sa.JSON(), nullable=True),
        sa.Column("next_open", sa.Float(), nullable=True),
        sa.Column("next2_high", sa.Float(), nullable=True),
        sa.Column("next2_low", sa.Float(), nullable=True),
        sa.Column("px_15", sa.Float(), nullable=True),
        sa.Column("px_30", sa.Float(), nullable=True),
        sa.Column("px_60", sa.Float(), nullable=True),
        sa.Column("high_60", sa.Float(), nullable=True),
        sa.Column("low_60", sa.Float(), nullable=True),
        sa.Column("fwd_complete", sa.Boolean(), nullable=False),
        sa.Column("fwd_updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("run_id", "underlying", "candle_ts", name="uq_trend_decisions_key"),
    )
    op.create_index("ix_td_day_underlying", TABLE, ["trading_day", "underlying"])
    op.create_index("ix_td_open_fwd", TABLE, ["fwd_complete", "trading_day"])


def downgrade() -> None:
    if not sa.inspect(op.get_bind()).has_table(TABLE):
        return
    op.drop_index("ix_td_open_fwd", table_name=TABLE)
    op.drop_index("ix_td_day_underlying", table_name=TABLE)
    op.drop_table(TABLE)
