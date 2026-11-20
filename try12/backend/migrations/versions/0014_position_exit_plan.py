"""paper_positions.exit_plan — per-position spot exit plan

The three strategies (VWAP_EMA_PULLBACK, ORB_BREAKOUT, SUPERTREND_EMA) carry
spot-based stops/targets, a T1 partial, break-even triggers and time stops.
paper_engine stores the plan (exit_plan.ExitPlan.to_dict()) on the position and
writes back its one-shot flags and trailed stop. NULL keeps the generic
premium ladder, so rows written before this revision are unchanged.

Revision ID: 0014
Revises: 0013
Create Date: 2026-10-22 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0014"
down_revision: Union[str, None] = "0013"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("paper_positions", sa.Column("exit_plan", sa.JSON(), nullable=True))


def downgrade() -> None:
    op.drop_column("paper_positions", "exit_plan")
