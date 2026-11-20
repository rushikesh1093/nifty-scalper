"""paper_positions: spot, delta and IV at entry and exit (trade log, Phase 0)

spot_entry / spot_exit: the underlying at entry and exit. delta_at_entry,
iv_entry, iv_exit: implied from the option's fill against spot and the time to
15:30 on expiry (chain_snapshots.option_greeks). All nullable; rows written
before this revision keep NULL.

Revision ID: 0016
Revises: 0015
Create Date: 2026-10-30 00:00:00
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0016"
down_revision: Union[str, None] = "0015"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

COLUMNS = ("spot_entry", "spot_exit", "delta_at_entry", "iv_entry", "iv_exit")


def upgrade() -> None:
    for name in COLUMNS:
        op.add_column("paper_positions", sa.Column(name, sa.Float(), nullable=True))


def downgrade() -> None:
    for name in reversed(COLUMNS):
        op.drop_column("paper_positions", name)
