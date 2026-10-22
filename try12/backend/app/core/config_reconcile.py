"""
trading_config reconciliation proposals (blockers-plan item 5). Pure: no I/O.

Given one ``trading_config`` row (as a raw mapping, so a pre-0013 column set
works) this computes what startup check 6 says about it, what migration 0013's
guarded default updates would rewrite, and the smallest correction that leaves
``validate()`` empty. ``scripts/reconcile_live_config.py`` prints the result; it
writes only under ``--apply`` with every safety condition met.

Never touched here: ``capital`` and ``fixed_fraction_pct`` (at most
``MAX_FIXED_FRACTION_PCT``, 100 %, of capital per trade). Whether the capital at
that fraction can buy a useful contract is the operator's decision (item 6);
the startup capital-sufficiency warning shows the numbers.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping, Optional

from app.core.trading_params import TRADE_AMOUNT_TOLERANCE, TradingParams, validate_overrides

PARTIAL_EXIT_MIN_LOTS = 2           # TradingParams.partial_exit_enabled is always True


@dataclass(frozen=True)
class Proposal:
    before: list[str]                         # validate() of the row as stored
    migration_updates: dict                   # what 0013 would rewrite ({} once applied)
    after_migration: list[str]                # validate() of row + migration_updates
    updates: dict                             # the proposed correction of the stored row
    after: list[str]                          # validate() of row + updates
    notes: list[str] = field(default_factory=list)


def overlay_from_row(row: Mapping[str, Any]) -> dict:
    """The row's values that overlay TradingParams: only TradingParams fields, never None."""
    names = TradingParams.field_names()
    return {k: v for k, v in row.items() if k in names and v is not None}


def _validate(row: Mapping[str, Any], updates: Optional[Mapping[str, Any]] = None) -> list[str]:
    merged = dict(row)
    merged.update(updates or {})
    return validate_overrides(overlay_from_row(merged))


def propose_corrections(row: Mapping[str, Any], *, schema_at_0013: bool,
                        migration_updates: Mapping[str, Any]) -> Proposal:
    """Corrections for the stored *row*.

    *migration_updates* is 0013's ``plan_default_updates(row)[0]``; it is reported
    (and validated on top of the row) only while the schema is not yet at 0013.
    The correction itself is computed against the row as stored, so it is the fix
    to apply on the schema the database has now.
    """
    overlay = overlay_from_row(row)
    casts = {"capital": float, "fixed_fraction_pct": float,
             "max_lots_per_trade": int, "trade_amount": float}
    tp = TradingParams(**{k: casts[k](v) for k, v in overlay.items() if k in casts})
    notes = [
        "capital and fixed_fraction_pct are never changed here: capital sizing (item 6) "
        "is the operator's decision.",
    ]

    mig = {} if schema_at_0013 else dict(migration_updates)
    if schema_at_0013:
        notes.append("schema is at 0013: its guarded default updates have already run.")
    elif mig:
        notes.append("migration 0013 has not run; its guarded default updates are shown "
                      "first (applying 0013 alone may already fix the row).")

    updates: dict = {}
    if tp.max_lots_per_trade < PARTIAL_EXIT_MIN_LOTS:
        updates["max_lots_per_trade"] = PARTIAL_EXIT_MIN_LOTS
    if tp.trade_amount is not None:
        derived = tp.fixed_fraction_pct / 100.0 * tp.capital
        if abs(tp.trade_amount - derived) > TRADE_AMOUNT_TOLERANCE:
            if schema_at_0013:
                updates["trade_amount"] = None
            else:
                updates["trade_amount"] = round(derived, 2)
                notes.append("trade_amount is NOT NULL before migration 0013, so the fix is "
                             "fixed_fraction_pct × capital rather than NULL.")

    return Proposal(
        before=_validate(row),
        migration_updates=mig,
        after_migration=_validate(row, mig),
        updates=updates,
        after=_validate(row, updates),
        notes=notes,
    )


def build_update_sql(updates: Mapping[str, Any]) -> tuple[str, dict]:
    """``UPDATE trading_config SET ... WHERE id = 1 AND mode = 'PAPER'`` with bound
    parameters. Column names must be TradingParams fields (they are interpolated)."""
    if not updates:
        raise ValueError("no updates to build")
    names = TradingParams.field_names()
    bad = sorted(k for k in updates if k not in names)
    if bad:
        raise ValueError(f"not TradingParams fields: {bad}")
    assignments = ", ".join(f"{k} = :{k}" for k in updates)
    return (f"UPDATE trading_config SET {assignments} WHERE id = 1 AND mode = 'PAPER'",
            dict(updates))
