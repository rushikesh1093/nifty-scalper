"""Add study_phase flag to trading_config

When True, signals are recorded but no positions are opened.
study_phase defaults to False — paper trading is active immediately.

Revision ID: 0005
Revises: 0004
Create Date: 2025-01-01 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0005"
down_revision: Union[str, None] = "0004"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trading_config",
        sa.Column("study_phase", sa.Boolean(), nullable=False, server_default="false"),
    )
    # Force any existing row to False so old deployments aren't stuck in study mode.
    op.execute("UPDATE trading_config SET study_phase = false")


def downgrade() -> None:
    op.drop_column("trading_config", "study_phase")
