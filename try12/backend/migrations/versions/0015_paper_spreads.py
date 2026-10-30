"""paper_spreads — multi-leg paper option spreads (expiry-day iron fly)

A separate paper book for spreads whose legs open and close together
(app.services.trading.iron_fly). paper_positions and everything that reads it
are unchanged.

Revision ID: 0015
Revises: 0014
Create Date: 2026-10-30 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0015"
down_revision: Union[str, None] = "0014"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "paper_spreads",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("strategy_name", sa.String(50), nullable=False),
        sa.Column("trading_day", sa.Date(), nullable=False),
        sa.Column("underlying", sa.String(20), nullable=False),
        sa.Column("expiry", sa.Date(), nullable=False),
        sa.Column("status", sa.String(12), nullable=False),
        sa.Column("lots", sa.Integer(), nullable=False),
        sa.Column("lot_size", sa.Integer(), nullable=False),
        sa.Column("legs", sa.JSON(), nullable=False),
        sa.Column("spot_entry", sa.Float(), nullable=False),
        sa.Column("spot_exit", sa.Float(), nullable=True),
        sa.Column("credit_pts", sa.Float(), nullable=False),
        sa.Column("max_loss", sa.Float(), nullable=False),
        sa.Column("entry_time", sa.DateTime(timezone=True), nullable=False),
        sa.Column("exit_time", sa.DateTime(timezone=True), nullable=True),
        sa.Column("exit_reason", sa.String(20), nullable=True),
        sa.Column("gross_pnl", sa.Float(), nullable=True),
        sa.Column("brokerage", sa.Float(), nullable=True),
        sa.Column("statutory_charges", sa.Float(), nullable=True),
        sa.Column("net_pnl", sa.Float(), nullable=True),
        sa.Column("r_multiple", sa.Float(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("strategy_name", "trading_day", name="uq_paper_spread_strategy_day"),
    )


def downgrade() -> None:
    op.drop_table("paper_spreads")
