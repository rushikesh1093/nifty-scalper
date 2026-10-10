"""
Reconcile the trading_config row with startup check 6 (blockers-plan item 5).

    python scripts/reconcile_live_config.py --database-url URL [--print-sql]
    python scripts/reconcile_live_config.py --database-url URL --apply --confirm-db NAME

DRY RUN BY DEFAULT. The dry run opens a read-only transaction (``SET
TRANSACTION READ ONLY`` on PostgreSQL), runs only SELECTs (``trading_config``
id = 1 and ``alembic_version``) and prints: the current values, the check-6
violations, what migration 0013's guarded default updates would rewrite (loaded
from the migration file itself, so the guard has one definition), the residual
violations after 0013, the proposed correction, validation after it, and the SQL.

For the live row (capital 15000, max_lots_per_trade 1, trade_amount 3000) on the
pre-0013 schema the proposed SQL is:

    UPDATE trading_config SET max_lots_per_trade = 2, trade_amount = 1500.0
    WHERE id = 1 AND mode = 'PAPER';

(after 0013: ``trade_amount = NULL``). Migration 0013 alone already rewrites
both fields (max_lots_per_trade -> 3, trade_amount -> NULL). This SQL has NOT
been applied to any database.

``--apply`` refuses (exit 2) unless ``--confirm-db`` equals the URL's database
name, the row is mode PAPER with live_mode_confirmed false, and the correction
validates. It then updates, re-reads and re-validates in one transaction, rolling
back on any violation. It never changes capital or fixed_fraction_pct (item 6 is
the operator's decision). There is no default URL.
"""
from __future__ import annotations

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import inspect as sa_inspect, text  # noqa: E402
from sqlalchemy.engine import make_url  # noqa: E402

from app.core.config_reconcile import Proposal, build_update_sql, propose_corrections  # noqa: E402

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION_0013 = BACKEND / "migrations" / "versions" / "0013_config_params_and_renames.py"
SHOWN_KEYS = ("mode", "live_mode_confirmed", "capital", "fixed_fraction_pct", "trade_amount",
              "max_lots_per_trade", "max_trades_per_day", "max_open_positions",
              "max_daily_loss", "max_daily_loss_pct")


class Refused(Exception):
    """--apply preconditions not met; nothing was written."""


def load_plan_default_updates():
    spec = importlib.util.spec_from_file_location("_migration_0013", MIGRATION_0013)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.plan_default_updates


def _sql_literal(v) -> str:
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    return repr(v)


def render_sql(updates: dict) -> str:
    """The UPDATE with values inlined, for an operator to read (never executed)."""
    sql, params_ = build_update_sql(updates)
    for k, v in params_.items():
        sql = sql.replace(f":{k}", _sql_literal(v))
    return sql + ";"


def parse_args(argv=None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(prog="python scripts/reconcile_live_config.py",
                                 description="Dry-run-by-default trading_config reconciliation.")
    ap.add_argument("--database-url", required=True,
                    help="SQLAlchemy async URL (no default)")
    ap.add_argument("--apply", action="store_true",
                    help="write the correction (requires --confirm-db)")
    ap.add_argument("--confirm-db", default=None, metavar="NAME",
                    help="must equal the database name in --database-url for --apply")
    ap.add_argument("--print-sql", action="store_true", help="print only the SQL snippet")
    return ap.parse_args(argv)


async def _read_state(db) -> tuple[dict, str | None]:
    """(trading_config row, alembic version or None). SELECTs only."""
    row = (await db.execute(text("SELECT * FROM trading_config WHERE id = 1"))
           ).mappings().one_or_none()
    if row is None:
        raise RuntimeError("trading_config row id = 1 not found")
    conn = await db.connection()
    has_version = await conn.run_sync(lambda c: sa_inspect(c).has_table("alembic_version"))
    version = None
    if has_version:
        version = (await db.execute(text("SELECT version_num FROM alembic_version"))).scalar()
    return dict(row), version


def _at_0013(version) -> bool:
    try:
        return version is not None and int(str(version)) >= 13
    except ValueError:
        return False


def _propose(row: dict, version) -> Proposal:
    at_0013 = _at_0013(version)
    migration_updates, _divergent = load_plan_default_updates()(row)
    return propose_corrections(row, schema_at_0013=at_0013, migration_updates=migration_updates)


def _report(row: dict, version, p: Proposal) -> list[str]:
    out = ["trading_config id = 1:"]
    out += [f"  {k} = {row[k]!r}" for k in SHOWN_KEYS if k in row]
    out.append(f"alembic version: {version or 'unknown (treated as pre-0013)'}")
    out.append("check-6 violations now:")
    out += [f"  - {v}" for v in p.before] or ["  (none)"]
    if _at_0013(version):
        out.append("migration 0013: already applied")
    else:
        out.append(f"migration 0013 would rewrite: {p.migration_updates or '{}'}")
        out.append("violations after 0013 alone:")
        out += [f"  - {v}" for v in p.after_migration] or ["  (none)"]
    out.append(f"proposed correction (on the schema as it is now): {p.updates or '{}'}")
    out.append("violations after the correction:")
    out += [f"  - {v}" for v in p.after] or ["  (none)"]
    out += [f"note: {n}" for n in p.notes]
    if p.updates:
        out.append("SQL:")
        out.append("  " + render_sql(p.updates))
    return out


async def _dry_run(Session) -> tuple[dict, str | None, Proposal]:
    async with Session() as db:
        try:
            if db.get_bind().dialect.name == "postgresql":
                await db.execute(text("SET TRANSACTION READ ONLY"))
            row, version = await _read_state(db)
        finally:
            await db.rollback()
    return row, version, _propose(row, version)


def _check_apply(row: dict, p: Proposal) -> None:
    if str(row.get("mode")) != "PAPER":
        raise Refused(f"mode is {row.get('mode')!r}, expected 'PAPER'")
    if bool(row.get("live_mode_confirmed")):
        raise Refused("live_mode_confirmed is true")
    if not p.updates:
        raise Refused("nothing to correct")
    if p.after:
        raise Refused(f"the correction would still fail validation: {p.after}")


async def _apply(Session) -> Proposal:
    async with Session() as db:
        async with db.begin():
            row, version = await _read_state(db)
            p = _propose(row, version)
            _check_apply(row, p)
            sql, bound = build_update_sql(p.updates)
            await db.execute(text(sql), bound)
            row2, _ = await _read_state(db)
            residual = propose_corrections(row2, schema_at_0013=True, migration_updates={}).before
            if residual:
                raise Refused(f"re-validation failed, rolled back: {residual}")
    return p


async def main_async(a: argparse.Namespace) -> int:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(a.database_url, poolclass=NullPool)
    try:
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        row, version, p = await _dry_run(Session)
        if a.print_sql:
            print(render_sql(p.updates) if p.updates else "-- nothing to correct")
        else:
            print("\n".join(_report(row, version, p)))
        if not a.apply:
            print("DRY RUN — nothing written")
            return 0
        _check_apply(row, p)
        await _apply(Session)
        print("APPLIED: " + render_sql(p.updates))
        return 0
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    a = parse_args(argv)
    if a.apply:
        expected = make_url(a.database_url).database
        if not a.confirm_db or a.confirm_db != expected:
            print(f"refusing --apply: --confirm-db must equal the database name {expected!r}",
                  file=sys.stderr)
            return 2
    try:
        return asyncio.run(main_async(a))
    except Refused as exc:
        print(f"refusing --apply: {exc}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"reconcile_live_config failed: {exc!r}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
