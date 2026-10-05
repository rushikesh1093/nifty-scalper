"""Fix daily_pnl_summary unique index to include mode column

Previously the index was unique on (date, strategy_name), which would reject
a second row when both PAPER and LIVE trades exist for the same strategy on the
same day.  The correct uniqueness key is (date, strategy_name, mode).

Revision ID: 0004
Revises: 0003
Create Date: 2025-01-01 00:00:00
"""
from typing import Sequence, Union
from alembic import op

revision: str = "0004"
down_revision: Union[str, None] = "0003"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Drop the old 2-column unique index
    op.drop_index("ix_dpnl_date_strategy", table_name="daily_pnl_summary")

    # Re-create with mode included so PAPER and LIVE rows can coexist
    op.create_index(
        "ix_dpnl_date_strategy",
        "daily_pnl_summary",
        ["date", "strategy_name", "mode"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index("ix_dpnl_date_strategy", table_name="daily_pnl_summary")
    op.create_index(
        "ix_dpnl_date_strategy",
        "daily_pnl_summary",
        ["date", "strategy_name"],
        unique=True,
    )
