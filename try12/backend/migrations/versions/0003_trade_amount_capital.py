"""Add trade_amount and capital tracking to trading_config

Revision ID: 0003
Revises: 0002
Create Date: 2024-01-03 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0003"
down_revision: Union[str, None] = "0002"
branch_labels: Union[str, Sequence[str], None] = None
depends_on:    Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # trade_amount_used on paper_positions
    op.add_column("paper_positions",
        sa.Column("trade_amount_used", sa.Float(), nullable=True))

    # trade_amount: ₹ budget per single trade (e.g. ₹10000)
    op.add_column("trading_config",
        sa.Column("trade_amount", sa.Float(), nullable=False, server_default="10000.0"))

    # used_capital: ₹ currently deployed in open positions (updated on open/close)
    op.add_column("trading_config",
        sa.Column("used_capital", sa.Float(), nullable=False, server_default="0.0"))

    # max_trades_per_day: hard cap on number of trades regardless of capital
    op.add_column("trading_config",
        sa.Column("max_trades_per_day", sa.Integer(), nullable=False, server_default="10"))

    # daily_trades_count: resets at midnight, incremented on each trade entry
    op.add_column("trading_config",
        sa.Column("daily_trades_count", sa.Integer(), nullable=False, server_default="0"))

    # Set sensible defaults on existing row
    op.execute(
        "UPDATE trading_config SET "
        "trade_amount=10000.0, used_capital=0.0, "
        "max_trades_per_day=10, daily_trades_count=0 "
        "WHERE id=1"
    )


def downgrade() -> None:
    op.drop_column("paper_positions", "trade_amount_used")
    for col in ["trade_amount", "used_capital", "max_trades_per_day", "daily_trades_count"]:
        op.drop_column("trading_config", col)
