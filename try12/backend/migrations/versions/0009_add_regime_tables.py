"""Add regime_states, regime_performance, regime_config tables and entry_regime on paper_positions

Revision ID: 0009
Revises: 0008
Create Date: 2024-01-09 00:00:00
"""
from typing import Sequence, Union
from alembic import op
import sqlalchemy as sa

revision: str = "0009"
down_revision: Union[str, None] = "0008"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:

    # ── regime_states ─────────────────────────────────────────────────────────
    # Stores each regime classification event with the supporting indicator values.
    # Requirements 13.1, 13.2 — regime state is persisted per underlying.
    op.create_table(
        "regime_states",
        sa.Column("id",                 sa.Integer(),               nullable=False, autoincrement=True),
        sa.Column("underlying",         sa.String(20),              nullable=False),
        sa.Column("regime",             sa.String(50),              nullable=False),
        sa.Column(
            "timestamp",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column("adx",                sa.Numeric(6, 2),           nullable=True),
        sa.Column("atr",                sa.Numeric(8, 2),           nullable=True),
        sa.Column("atr_percentile",     sa.Numeric(5, 2),           nullable=True),
        sa.Column("vwap",               sa.Numeric(10, 2),          nullable=True),
        sa.Column("price",              sa.Numeric(10, 2),          nullable=True),
        sa.Column("vwap_cross_count",   sa.Integer(),               nullable=True),
        sa.Column("volume",             sa.BigInteger(),            nullable=True),
        sa.Column("avg_volume",         sa.Numeric(12, 2),          nullable=True),
        sa.Column("previous_regime",    sa.String(50),              nullable=True),
        sa.Column(
            "transition_detected",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("false"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("underlying", "timestamp", name="uq_regime_states_underlying_ts"),
    )
    op.create_index(
        "ix_regime_states_underlying_ts",
        "regime_states",
        ["underlying", sa.text("timestamp DESC")],
    )
    op.create_index(
        "ix_regime_states_regime",
        "regime_states",
        ["regime"],
    )

    # ── regime_performance ────────────────────────────────────────────────────
    # Per-regime, per-underlying, per-strategy PnL tracking.
    # Requirement 19.3 — persisted in DB with required fields.
    op.create_table(
        "regime_performance",
        sa.Column("id",             sa.Integer(),               nullable=False, autoincrement=True),
        sa.Column("regime",         sa.String(50),              nullable=False),
        sa.Column("underlying",     sa.String(20),              nullable=False),
        sa.Column("strategy_name",  sa.String(50),              nullable=False),
        sa.Column("total_trades",   sa.Integer(),               nullable=False, server_default="0"),
        sa.Column("winning_trades", sa.Integer(),               nullable=False, server_default="0"),
        sa.Column("losing_trades",  sa.Integer(),               nullable=False, server_default="0"),
        sa.Column("total_pnl",      sa.Numeric(12, 2),          nullable=False, server_default="0"),
        sa.Column("avg_pnl",        sa.Numeric(10, 2),          nullable=True),
        sa.Column("win_rate",       sa.Numeric(5, 2),           nullable=True),
        sa.Column("avg_rr",         sa.Numeric(6, 3),           nullable=True),
        sa.Column("first_trade_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_trade_at",  sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "regime", "underlying", "strategy_name",
            name="uq_regime_perf_regime_underlying_strategy",
        ),
    )
    op.create_index(
        "ix_regime_performance_regime",
        "regime_performance",
        ["regime"],
    )
    op.create_index(
        "ix_regime_performance_underlying",
        "regime_performance",
        ["underlying"],
    )

    # ── regime_config ─────────────────────────────────────────────────────────
    # Configurable thresholds for regime detection.  underlying=NULL is the
    # global default row; per-underlying rows override it (Requirement 18).
    op.create_table(
        "regime_config",
        sa.Column("id",                         sa.Integer(),      nullable=False, autoincrement=True),
        sa.Column("underlying",                 sa.String(20),     nullable=True),   # NULL = global
        sa.Column("adx_trend_threshold",        sa.Numeric(5, 2),  nullable=True, server_default="22.0"),
        sa.Column("adx_range_threshold",        sa.Numeric(5, 2),  nullable=True, server_default="18.0"),
        sa.Column("atr_low_percentile",         sa.Numeric(5, 2),  nullable=True, server_default="30.0"),
        sa.Column("atr_high_percentile",        sa.Numeric(5, 2),  nullable=True, server_default="70.0"),
        sa.Column("vwap_proximity_percent",     sa.Numeric(5, 3),  nullable=True, server_default="0.2"),
        sa.Column("vwap_cross_chop_threshold",  sa.Integer(),      nullable=True, server_default="8"),
        sa.Column("volume_surge_multiplier",    sa.Numeric(4, 2),  nullable=True, server_default="1.5"),
        sa.Column("regime_transition_window",   sa.Integer(),      nullable=True, server_default="2"),
        sa.Column("transition_cooldown_candles",sa.Integer(),      nullable=True, server_default="3"),
        sa.Column(
            "is_active",
            sa.Boolean(),
            nullable=True,
            server_default=sa.text("true"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("underlying", name="uq_regime_config_underlying"),
    )

    # Seed the global-default row (underlying IS NULL).
    op.execute("""
        INSERT INTO regime_config (
            underlying,
            adx_trend_threshold,
            adx_range_threshold,
            atr_low_percentile,
            atr_high_percentile,
            vwap_proximity_percent,
            vwap_cross_chop_threshold,
            volume_surge_multiplier,
            regime_transition_window,
            transition_cooldown_candles,
            is_active
        ) VALUES (
            NULL,
            22.0,
            18.0,
            30.0,
            70.0,
            0.200,
            8,
            1.50,
            2,
            3,
            TRUE
        )
    """)

    # ── paper_positions: add entry_regime column ──────────────────────────────
    # Records the regime that was active when the position was opened.
    # Requirement 12.1 / 19.1 — links closed positions to their entry regime.
    op.add_column(
        "paper_positions",
        sa.Column("entry_regime", sa.String(50), nullable=True),
    )
    op.create_index(
        "ix_paper_positions_entry_regime",
        "paper_positions",
        ["entry_regime"],
    )


def downgrade() -> None:
    # Reverse in opposite order to avoid FK / dependency issues.
    op.drop_index("ix_paper_positions_entry_regime", table_name="paper_positions")
    op.drop_column("paper_positions", "entry_regime")

    op.drop_table("regime_config")

    op.drop_index("ix_regime_performance_underlying",  table_name="regime_performance")
    op.drop_index("ix_regime_performance_regime",      table_name="regime_performance")
    op.drop_table("regime_performance")

    op.drop_index("ix_regime_states_regime",           table_name="regime_states")
    op.drop_index("ix_regime_states_underlying_ts",    table_name="regime_states")
    op.drop_table("regime_states")
