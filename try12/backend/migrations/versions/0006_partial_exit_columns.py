"""Add partial exit columns to paper_positions

Tracks the 50% partial close that fires at +35% gain.
Remaining lots continue to full target with SL moved to breakeven.

Revision ID: 0006
Revises: 0005
Create Date: 2025-01-01 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0006"
down_revision: Union[str, None] = "0005"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_positions",
        sa.Column("partial_closed",     sa.Boolean(), nullable=False, server_default="false"))
    op.add_column("paper_positions",
        sa.Column("partial_exit_price", sa.Float(),   nullable=True))
    op.add_column("paper_positions",
        sa.Column("partial_exit_qty",   sa.Integer(), nullable=True))
    op.add_column("paper_positions",
        sa.Column("partial_pnl",        sa.Float(),   nullable=True))
    op.add_column("paper_positions",
        sa.Column("remaining_quantity", sa.Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_positions", "remaining_quantity")
    op.drop_column("paper_positions", "partial_pnl")
    op.drop_column("paper_positions", "partial_exit_qty")
    op.drop_column("paper_positions", "partial_exit_price")
    op.drop_column("paper_positions", "partial_closed")
