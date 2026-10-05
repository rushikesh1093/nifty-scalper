"""Add strategy_perf table for half-Kelly dynamic sizing

Revision ID: 0008
Revises: 0007
Create Date: 2024-01-08 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0008"
down_revision: Union[str, None] = "0007"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        "strategy_perf",
        sa.Column("id",             sa.Integer(),    nullable=False, autoincrement=True),
        sa.Column("strategy_name",  sa.String(50),   nullable=False),
        sa.Column("total_trades",   sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(),    nullable=False, server_default="0"),
        sa.Column("win_rate",       sa.Float(),      nullable=True),
        sa.Column("avg_rr",         sa.Float(),      nullable=True),
        sa.Column("kelly_fraction", sa.Float(),      nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            onupdate=sa.text("now()"),
            nullable=True,
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("strategy_name", name="uq_strategy_perf_name"),
    )
    op.create_index(
        "ix_strategy_perf_name",
        "strategy_perf",
        ["strategy_name"],
        unique=True,
    )

    # Bump max_open_positions default from 3 → 6 on the config row if it exists
    # so existing deployments benefit without a manual API call.
    op.execute("""
        UPDATE trading_config
        SET max_open_positions = 6
        WHERE id = 1 AND max_open_positions <= 3
    """)


def downgrade() -> None:
    op.drop_index("ix_strategy_perf_name", table_name="strategy_perf")
    op.drop_table("strategy_perf")
