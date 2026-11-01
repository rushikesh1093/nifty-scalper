"""strategy_signals: the signal's exit plan and its shadow (counterfactual) outcome

exit_plan: the spot exit plan the strategy attached (exit_plan.ExitPlan.to_dict).
shadow_r_net / shadow_exit / shadow_hold_min: for a SKIPPED signal, what the plan
would have made on that day's real 1-min candles, net of the research cost model
(app.services.trading.shadow_outcomes), written after the close. All nullable;
rows written before this revision keep NULL.

Revision ID: 0017
Revises: 0016
Create Date: 2026-11-01 00:00:00
"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "0017"
down_revision: Union[str, None] = "0016"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("strategy_signals", sa.Column("exit_plan", sa.JSON(), nullable=True))
    op.add_column("strategy_signals", sa.Column("shadow_r_net", sa.Float(), nullable=True))
    op.add_column("strategy_signals", sa.Column("shadow_exit", sa.String(20), nullable=True))
    op.add_column("strategy_signals", sa.Column("shadow_hold_min", sa.Float(), nullable=True))


def downgrade() -> None:
    for name in ("shadow_hold_min", "shadow_exit", "shadow_r_net", "exit_plan"):
        op.drop_column("strategy_signals", name)
