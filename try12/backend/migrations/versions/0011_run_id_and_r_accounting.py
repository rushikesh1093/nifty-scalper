"""run_id on every trading table, R-accounting columns, run-scoped unique keys

paper_positions:    run_id + the R-accounting / fill-provenance columns (design §8.4, §11).
strategy_signals:   run_id, expiry_band, expiry_reason, selection_rejections,
                    score_breakdown, no_trade_decision_id.
regime_states:      run_id; unique key (underlying, timestamp) → (run_id, underlying, timestamp).
daily_pnl_summary:  run_id, statutory_charges, flat/R sums, expectancies, gross
                    profit/loss, max_equity_drawdown; unique index gains run_id.
strategy_perf:      run_id, losing/flat counts, R sums and the R-sign Kelly counters
                    (winning_trades_r / losing_trades_r); unique key → (strategy_name, run_id).
regime_performance: run_id, flat count, R sums, expectancies, gross profit/loss;
                    unique key → (regime, underlying, strategy_name, run_id).

Revision ID: 0011
Revises: 0010
Create Date: 2026-10-20 00:00:00

Every new metric column is Float (double precision), never Numeric (§7).
Pre-existing positions are marked r_source = 'BACKFILL_UNVERIFIED' so R
statistics exclude them; the strategy_perf _r counters are backfilled by
reconcile_performance, not here (only a per-position scan can tell).

Constraint changes run through batch_alter_table so the same revision runs on
PostgreSQL (plain ALTER) and on SQLite (table copy) for the migration test.
Old unique keys are located by their column set, not their name, so a
database whose constraint names drifted still migrates.

downgrade() deletes every non-'PAPER' row from the run-scoped aggregate
tables before restoring the narrower unique keys (they would collide).
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0011"
down_revision: Union[str, None] = "0010"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

RUN_ID = dict(nullable=False, server_default="PAPER")

POSITION_COLUMNS = (
    ("original_quantity", sa.Integer(), {}),
    ("original_lots", sa.Integer(), {}),
    ("initial_risk", sa.Float(), {}),
    ("r_source", sa.String(20), {}),
    ("exit_value_weighted", sa.Float(), {}),
    ("r_multiple", sa.Float(), {}),
    ("r_multiple_net", sa.Float(), {}),
    ("target_stretched", sa.Boolean(), dict(nullable=False, server_default=sa.false())),
    ("entry_fill_source", sa.String(16), {}),
    ("exit_fill_source", sa.String(16), {}),
    ("dte_at_entry", sa.Integer(), {}),
    ("expiry_band", sa.String(16), {}),
    ("entry_regime_multiplier", sa.Float(), {}),
    ("partial_skipped", sa.Boolean(), dict(nullable=False, server_default=sa.false())),
    ("hold_minutes", sa.Float(), {}),
    ("sizing_detail", sa.JSON(), {}),
    ("entry_indicators", sa.JSON(), {}),
)

SIGNAL_COLUMNS = (
    ("expiry_band", sa.String(16), {}),
    ("expiry_reason", sa.String(200), {}),
    ("selection_rejections", sa.JSON(), {}),
    ("score_breakdown", sa.JSON(), {}),
    ("no_trade_decision_id", sa.BigInteger(), {}),
)

_I0 = dict(nullable=False, server_default="0")

DAILY_COLUMNS = (
    ("statutory_charges", sa.Float(), _I0),
    ("flat_trades", sa.Integer(), _I0),
    ("sum_r", sa.Float(), _I0),
    ("sum_r_win", sa.Float(), _I0),
    ("sum_r_loss", sa.Float(), _I0),
    ("r_sample_size", sa.Integer(), _I0),
    ("expectancy_r", sa.Float(), {}),
    ("expectancy_inr", sa.Float(), {}),
    ("gross_profit", sa.Float(), _I0),
    ("gross_loss", sa.Float(), _I0),
    ("max_equity_drawdown", sa.Float(), {}),
)

PERF_COLUMNS = (
    ("losing_trades", sa.Integer(), _I0),
    ("flat_trades", sa.Integer(), _I0),
    ("sum_r", sa.Float(), _I0),
    ("sum_r_win", sa.Float(), _I0),
    ("sum_r_loss", sa.Float(), _I0),
    ("r_sample_size", sa.Integer(), _I0),
    ("winning_trades_r", sa.Integer(), _I0),
    ("losing_trades_r", sa.Integer(), _I0),
    ("avg_win_r", sa.Float(), {}),
    ("avg_loss_r", sa.Float(), {}),
    ("expectancy_r", sa.Float(), {}),
    ("expectancy_inr", sa.Float(), {}),
    ("gross_profit", sa.Float(), _I0),
    ("gross_loss", sa.Float(), _I0),
)

REGIME_PERF_COLUMNS = (
    ("flat_trades", sa.Integer(), _I0),
    ("sum_r", sa.Float(), _I0),
    ("sum_r_win", sa.Float(), _I0),
    ("sum_r_loss", sa.Float(), _I0),
    ("r_sample_size", sa.Integer(), _I0),
    ("avg_r_multiple", sa.Float(), {}),
    ("expectancy_r", sa.Float(), {}),
    ("expectancy_inr", sa.Float(), {}),
    ("gross_profit", sa.Float(), _I0),
    ("gross_loss", sa.Float(), _I0),
)


# ── helpers ──────────────────────────────────────────────────────────────────

def _unique_names(table: str, cols: tuple[str, ...]) -> tuple[list[str], list[str]]:
    """(unique constraint names, standalone unique index names) on exactly *cols*."""
    insp = sa.inspect(op.get_bind())
    cons = [u["name"] for u in insp.get_unique_constraints(table)
            if tuple(u["column_names"]) == cols and u.get("name")]
    idx = [i["name"] for i in insp.get_indexes(table)
           if i.get("unique") and tuple(i["column_names"]) == cols
           and not i.get("duplicates_constraint") and i["name"] not in cons]
    return cons, idx


def _swap_unique(table: str, old_cols: tuple[str, ...], new_name: str,
                 new_cols: tuple[str, ...]) -> None:
    cons, idx = _unique_names(table, old_cols)
    for name in idx:
        op.drop_index(name, table_name=table)
    with op.batch_alter_table(table) as b:
        for name in cons:
            b.drop_constraint(name, type_="unique")
        b.create_unique_constraint(new_name, list(new_cols))


def _add(table: str, columns) -> None:
    for name, type_, kw in columns:
        op.add_column(table, sa.Column(name, type_, nullable=kw.get("nullable", True),
                                       server_default=kw.get("server_default")))


def _drop(table: str, names) -> None:
    with op.batch_alter_table(table) as b:
        for name in names:
            b.drop_column(name)


# ── upgrade ──────────────────────────────────────────────────────────────────

def upgrade() -> None:
    # paper_positions
    op.add_column("paper_positions", sa.Column("run_id", sa.String(32), **RUN_ID))
    _add("paper_positions", POSITION_COLUMNS)
    op.create_index("ix_pp_run_entry", "paper_positions", ["run_id", "entry_time"])
    # quantity / lots are reduced by a partial close, so add the closed leg back.
    op.execute("""
        UPDATE paper_positions
        SET original_quantity = quantity + CASE WHEN partial_closed
                                               THEN COALESCE(partial_exit_qty, 0) ELSE 0 END,
            original_lots = lots + CASE WHEN partial_closed AND lot_size > 0
                                        THEN COALESCE(partial_exit_qty, 0) / lot_size ELSE 0 END
    """)
    op.execute("""
        UPDATE paper_positions
        SET initial_risk = actual_entry - initial_stop_loss
        WHERE initial_stop_loss IS NOT NULL AND actual_entry - initial_stop_loss > 0
    """)
    # 0010 backfilled initial_stop_loss from stop_loss_ref, which may be a trailed
    # value: no pre-existing row is trusted for R statistics.
    op.execute("UPDATE paper_positions SET r_source = 'BACKFILL_UNVERIFIED'")

    # strategy_signals
    op.add_column("strategy_signals", sa.Column("run_id", sa.String(32), **RUN_ID))
    _add("strategy_signals", SIGNAL_COLUMNS)

    # regime_states
    op.add_column("regime_states", sa.Column("run_id", sa.String(32), **RUN_ID))
    _swap_unique("regime_states", ("underlying", "timestamp"),
                 "uq_regime_states_run_underlying_ts", ("run_id", "underlying", "timestamp"))

    # daily_pnl_summary
    op.add_column("daily_pnl_summary", sa.Column("run_id", sa.String(32), **RUN_ID))
    _add("daily_pnl_summary", DAILY_COLUMNS)
    op.drop_index("ix_dpnl_date_strategy", table_name="daily_pnl_summary")
    op.create_index("ix_dpnl_date_strategy", "daily_pnl_summary",
                    ["date", "strategy_name", "mode", "run_id"], unique=True)

    # strategy_perf
    op.add_column("strategy_perf", sa.Column("run_id", sa.String(32), **RUN_ID))
    _add("strategy_perf", PERF_COLUMNS)
    _swap_unique("strategy_perf", ("strategy_name",),
                 "uq_strategy_perf_name_run", ("strategy_name", "run_id"))
    if not any(i["name"] == "ix_strategy_perf_name"
               for i in sa.inspect(op.get_bind()).get_indexes("strategy_perf")):
        op.create_index("ix_strategy_perf_name", "strategy_perf", ["strategy_name"])

    # regime_performance
    op.add_column("regime_performance", sa.Column("run_id", sa.String(32), **RUN_ID))
    _add("regime_performance", REGIME_PERF_COLUMNS)
    _swap_unique("regime_performance", ("regime", "underlying", "strategy_name"),
                 "uq_regime_perf_key_run", ("regime", "underlying", "strategy_name", "run_id"))


# ── downgrade ────────────────────────────────────────────────────────────────

def downgrade() -> None:
    # regime_performance
    op.execute("DELETE FROM regime_performance WHERE run_id <> 'PAPER'")
    with op.batch_alter_table("regime_performance") as b:
        b.drop_constraint("uq_regime_perf_key_run", type_="unique")
        b.create_unique_constraint("uq_regime_perf_regime_underlying_strategy",
                                   ["regime", "underlying", "strategy_name"])
    _drop("regime_performance", ["run_id"] + [c[0] for c in REGIME_PERF_COLUMNS])

    # strategy_perf
    op.execute("DELETE FROM strategy_perf WHERE run_id <> 'PAPER'")
    op.drop_index("ix_strategy_perf_name", table_name="strategy_perf")
    with op.batch_alter_table("strategy_perf") as b:
        b.drop_constraint("uq_strategy_perf_name_run", type_="unique")
        b.create_unique_constraint("uq_strategy_perf_name", ["strategy_name"])
    _drop("strategy_perf", ["run_id"] + [c[0] for c in PERF_COLUMNS])
    op.create_index("ix_strategy_perf_name", "strategy_perf", ["strategy_name"], unique=True)

    # daily_pnl_summary
    op.execute("DELETE FROM daily_pnl_summary WHERE run_id <> 'PAPER'")
    op.drop_index("ix_dpnl_date_strategy", table_name="daily_pnl_summary")
    _drop("daily_pnl_summary", ["run_id"] + [c[0] for c in DAILY_COLUMNS])
    op.create_index("ix_dpnl_date_strategy", "daily_pnl_summary",
                    ["date", "strategy_name", "mode"], unique=True)

    # regime_states
    op.execute("DELETE FROM regime_states WHERE run_id <> 'PAPER'")
    with op.batch_alter_table("regime_states") as b:
        b.drop_constraint("uq_regime_states_run_underlying_ts", type_="unique")
        b.create_unique_constraint("uq_regime_states_underlying_ts", ["underlying", "timestamp"])
    _drop("regime_states", ["run_id"])

    # strategy_signals
    _drop("strategy_signals", ["run_id"] + [c[0] for c in SIGNAL_COLUMNS])

    # paper_positions
    op.drop_index("ix_pp_run_entry", table_name="paper_positions")
    _drop("paper_positions", ["run_id"] + [c[0] for c in POSITION_COLUMNS])
