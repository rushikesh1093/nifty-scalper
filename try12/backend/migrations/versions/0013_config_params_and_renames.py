"""trading_config risk parameters, guarded default updates, renames

trading_config:    the operator-facing risk numbers as typed columns (they
                   overlay TradingParams); trade_amount becomes a nullable
                   display cache and known-bad defaults are rewritten.
daily_pnl_summary: max_drawdown → worst_trade_loss (R15.4: it was the largest
                   single-trade loss, never an equity drawdown).

Revision ID: 0013
Revises: 0012
Create Date: 2026-10-20 00:00:02

Guarded default updates (design §11). A stored value is rewritten only when
it equals the column default OR the value mode_manager.ensure_config would
have derived from capital; anything else is operator-set, left alone, and
logged as a divergence (R18.5). capital itself is never touched.

The guard logic is pure (plan_default_updates / is_known_bad) so it is unit
tested without PostgreSQL. downgrade() refills trade_amount from
fixed_fraction_pct × capital before restoring NOT NULL; the rewritten
defaults are not reverted (they were known-bad values).
"""
import logging
from typing import Any, Mapping, Optional, Sequence, Union

from alembic import op
import sqlalchemy as sa

revision: str = "0013"
down_revision: Union[str, None] = "0012"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

logger = logging.getLogger("alembic.runtime.migration")

CONFIG_COLUMNS = (
    ("open_exposure_cap_pct", sa.Float(), "40.0"),
    ("fixed_fraction_pct", sa.Float(), "10.0"),
    ("kelly_multiplier", sa.Float(), "0.25"),
    ("kelly_fraction_cap_pct", sa.Float(), "15.0"),
    ("kelly_min_trades", sa.Integer(), "30"),
    ("losing_streak_halt_n", sa.Integer(), "3"),
    ("strategy_streak_halt_n", sa.Integer(), "2"),
    ("post_loss_cooldown_minutes", sa.Integer(), "15"),
    ("min_rr", sa.Float(), "2.0"),
    ("stale_tick_seconds", sa.Integer(), "60"),
    ("entry_cutoff_buffer_min", sa.Integer(), "10"),
)

GUARDED_KEYS = ("max_trades_per_day", "max_lots_per_trade", "max_open_positions",
                "max_daily_loss_pct", "max_daily_loss", "trade_amount")


# ── pure guard logic ─────────────────────────────────────────────────────────

def _f(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _same(a: Any, b: Any) -> bool:
    a, b = _f(a), _f(b)
    return a is not None and b is not None and abs(a - b) < 1e-9


def known_bad_values(key: str, row: Mapping[str, Any]) -> list[float]:
    """Column default, plus what ensure_config would have derived, for *key*."""
    capital = _f(row.get("capital")) or 0.0
    trade_amount = _f(row.get("trade_amount"))
    if key == "max_trades_per_day":
        return [10]
    if key == "max_lots_per_trade":
        return [5, max(1, min(5, int(capital / 10_000)))]
    if key == "max_open_positions":
        out = [6]
        if trade_amount:
            out.append(max(2, int(capital / trade_amount * 0.6)))
        return out
    if key == "max_daily_loss_pct":
        return [2.0, 5.0]
    if key == "max_daily_loss":
        return [5000.0, float(round(capital * 0.05))]
    if key == "trade_amount":
        return [10000.0, float(round(capital * 0.20 / 500) * 500)]
    raise KeyError(key)


def target_value(key: str, row: Mapping[str, Any]) -> Any:
    capital = _f(row.get("capital")) or 0.0
    return {
        "max_trades_per_day": 6,
        "max_lots_per_trade": 3,
        "max_open_positions": 3,
        "max_daily_loss_pct": 3.0,
        "max_daily_loss": round(0.03 * capital, 2),
        "trade_amount": None,                   # demoted to a display cache (§9.5)
    }[key]


def is_known_bad(key: str, row: Mapping[str, Any]) -> bool:
    return any(_same(row.get(key), v) for v in known_bad_values(key, row))


def plan_default_updates(row: Mapping[str, Any]) -> tuple[dict, dict]:
    """(updates to apply, operator-set values left alone) for one trading_config row.
    Every predicate is evaluated against the row as stored, before any update."""
    updates: dict = {}
    divergent: dict = {}
    for key in GUARDED_KEYS:
        stored = row.get(key)
        target = target_value(key, row)
        if stored is None and target is None:
            continue
        if target is not None and _same(stored, target):
            continue
        if is_known_bad(key, row):
            updates[key] = target
        else:
            divergent[key] = stored
    return updates, divergent


# ── upgrade / downgrade ──────────────────────────────────────────────────────

def upgrade() -> None:
    for name, type_, default in CONFIG_COLUMNS:
        op.add_column("trading_config",
                      sa.Column(name, type_, nullable=False, server_default=default))
    with op.batch_alter_table("trading_config") as b:
        b.alter_column("trade_amount", existing_type=sa.Float(), nullable=True)

    bind = op.get_bind()
    cfg = sa.table("trading_config", sa.column("id"), sa.column("capital"),
                   *(sa.column(k) for k in GUARDED_KEYS))
    for row in bind.execute(sa.select(cfg)).mappings().all():
        updates, divergent = plan_default_updates(dict(row))
        if updates:
            bind.execute(sa.update(cfg).where(cfg.c.id == row["id"]).values(**updates))
            logger.info("0013: trading_config id=%s known-bad defaults rewritten: %s",
                        row["id"], updates)
        if divergent:
            logger.warning("0013: trading_config id=%s operator-set values left alone "
                           "(review against the §19.1 defaults): %s", row["id"], divergent)

    with op.batch_alter_table("daily_pnl_summary") as b:
        b.alter_column("max_drawdown", new_column_name="worst_trade_loss",
                       existing_type=sa.Float(), existing_nullable=True)


def downgrade() -> None:
    with op.batch_alter_table("daily_pnl_summary") as b:
        b.alter_column("worst_trade_loss", new_column_name="max_drawdown",
                       existing_type=sa.Float(), existing_nullable=True)

    op.execute("""
        UPDATE trading_config
        SET trade_amount = fixed_fraction_pct * capital / 100.0
        WHERE trade_amount IS NULL
    """)
    with op.batch_alter_table("trading_config") as b:
        b.alter_column("trade_amount", existing_type=sa.Float(), nullable=False)
        for name, _, _ in reversed(CONFIG_COLUMNS):
            b.drop_column(name)
