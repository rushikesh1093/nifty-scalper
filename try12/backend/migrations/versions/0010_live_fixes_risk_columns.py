"""Add live-fix risk columns (initial_stop_loss, statutory_charges, kill switch, daily_trades_date)

paper_positions: initial_stop_loss, statutory_charges.
trading_config:  trading_halted, halted_reason, halted_at, daily_trades_date.

Revision ID: 0010
Revises: 0009
Create Date: 2026-10-06 00:00:00

All columns are nullable or carry a server default, so existing rows and older
code paths keep working (backwards compatible).
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0010"
down_revision: Union[str, None] = "0009"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── paper_positions ───────────────────────────────────────────────────────
    # initial_stop_loss: SL at entry, never overwritten by trailing updates.
    # Used for achieved R:R (Kelly sizing, regime performance).
    op.add_column(
        "paper_positions",
        sa.Column("initial_stop_loss", sa.Float(), nullable=True),
    )
    # statutory_charges: STT + exchange txn + SEBI + stamp + GST on closed qty.
    op.add_column(
        "paper_positions",
        sa.Column("statutory_charges", sa.Float(), nullable=True, server_default="0"),
    )

    # Backfill initial_stop_loss: prefer the SL recorded on the originating
    # signal (never trailed), else the current stop_loss.
    op.execute("""
        UPDATE paper_positions p
        SET initial_stop_loss = s.stop_loss_ref
        FROM strategy_signals s
        WHERE p.signal_id = s.id
          AND p.initial_stop_loss IS NULL
          AND s.stop_loss_ref IS NOT NULL
    """)
    op.execute("""
        UPDATE paper_positions
        SET initial_stop_loss = stop_loss
        WHERE initial_stop_loss IS NULL
    """)

    # ── trading_config ────────────────────────────────────────────────────────
    # Kill switch: persisted halt flag survives restarts.
    op.add_column(
        "trading_config",
        sa.Column(
            "trading_halted",
            sa.Boolean(),
            nullable=False,
            server_default=sa.text("false"),
        ),
    )
    op.add_column(
        "trading_config",
        sa.Column("halted_reason", sa.String(200), nullable=True),
    )
    op.add_column(
        "trading_config",
        sa.Column("halted_at", sa.DateTime(timezone=True), nullable=True),
    )
    # IST trading date that daily_trades_count belongs to.
    op.add_column(
        "trading_config",
        sa.Column("daily_trades_date", sa.Date(), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("trading_config", "daily_trades_date")
    op.drop_column("trading_config", "halted_at")
    op.drop_column("trading_config", "halted_reason")
    op.drop_column("trading_config", "trading_halted")

    op.drop_column("paper_positions", "statutory_charges")
    op.drop_column("paper_positions", "initial_stop_loss")
