"""
Item 5 — dry-run-by-default trading_config reconciliation (blockers-plan item 5).

The live row (capital 15000, max_lots_per_trade 1, trade_amount 3000, ...) fails
startup check 6 twice. Migration 0013's guarded default updates would already fix
both; the script must show that first, then the residual fix. ``--apply`` is only
exercised on its REFUSAL paths here — a successful apply is never run.
"""
import importlib.util
from pathlib import Path

import pytest
from sqlalchemy import event, text
from sqlalchemy.engine import Engine

from app.core.config_reconcile import build_update_sql, overlay_from_row, propose_corrections
from app.core.trading_params import validate_overrides
from app.runtime.context import using
from app.services.trading.startup_checks import StartupAssertionError, run_startup_checks
from tests.helpers import run, seed_config, with_params

BACKEND = Path(__file__).resolve().parents[1]
MIGRATION_0013 = BACKEND / "migrations" / "versions" / "0013_config_params_and_renames.py"
SCRIPT = BACKEND / "scripts" / "reconcile_live_config.py"

LIVE_ROW = dict(id=1, mode="PAPER", live_mode_confirmed=False, capital=15000.0,
                max_lots_per_trade=1, trade_amount=3000.0, max_trades_per_day=10,
                max_open_positions=2, max_daily_loss=500.0)


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _migration_updates(row):
    updates, _divergent = _load(MIGRATION_0013, "_m0013").plan_default_updates(row)
    return updates


# ── pure proposal ────────────────────────────────────────────────────────────

def test_the_live_row_has_both_violations():
    v = validate_overrides(overlay_from_row(LIVE_ROW))
    assert any(s.startswith("max_lots_per_trade=1") for s in v)
    assert any(s.startswith("trade_amount=3000.0") for s in v)
    assert len(v) == 2


def test_overlay_keeps_only_tradingparams_fields_that_are_not_null():
    o = overlay_from_row(dict(LIVE_ROW, trade_amount=None, strategy_flags={}))
    assert "id" not in o and "mode" not in o and "live_mode_confirmed" not in o
    assert "strategy_flags" not in o and "trade_amount" not in o
    assert o["capital"] == 15000.0 and o["max_lots_per_trade"] == 1


def test_migration_0013_alone_would_fix_both():
    mig = _migration_updates(LIVE_ROW)
    assert mig == {"max_trades_per_day": 6, "max_lots_per_trade": 3, "trade_amount": None}
    p = propose_corrections(LIVE_ROW, schema_at_0013=False, migration_updates=mig)
    assert len(p.before) == 2
    assert p.migration_updates == mig
    assert p.after_migration == []


def test_pre_0013_fix_keeps_trade_amount_not_null():
    p = propose_corrections(LIVE_ROW, schema_at_0013=False,
                            migration_updates=_migration_updates(LIVE_ROW))
    assert p.updates == {"max_lots_per_trade": 2, "trade_amount": 1500.0}
    assert p.after == []
    assert "capital" not in p.updates and "fixed_fraction_pct" not in p.updates
    assert any("item 6" in n for n in p.notes)


def test_post_0013_fix_nulls_trade_amount():
    p = propose_corrections(LIVE_ROW, schema_at_0013=True,
                            migration_updates=_migration_updates(LIVE_ROW))
    assert p.updates == {"max_lots_per_trade": 2, "trade_amount": None}
    assert p.after == []


def test_build_update_sql_is_scoped_and_parameterised():
    sql, params = build_update_sql({"max_lots_per_trade": 2, "trade_amount": 1500.0})
    assert sql.startswith("UPDATE trading_config SET ")
    assert sql.endswith("WHERE id = 1 AND mode = 'PAPER'")
    assert ":max_lots_per_trade" in sql and ":trade_amount" in sql
    assert "1500" not in sql
    assert params == {"max_lots_per_trade": 2, "trade_amount": 1500.0}
    with pytest.raises(ValueError):
        build_update_sql({"capital; DROP TABLE x": 1})
    with pytest.raises(ValueError):
        build_update_sql({})


# ── the CLI (dry run + refusal paths only) ───────────────────────────────────

def _raw_row(Session):
    async def _go():
        async with Session() as db:
            return dict((await db.execute(text(
                "SELECT * FROM trading_config WHERE id = 1"))).mappings().one())
    return run(_go())


@pytest.fixture
def live_like_db(sqlite_db, tmp_path):
    run(seed_config(sqlite_db, max_lots_per_trade=1, trade_amount=3000.0, capital=15000.0))
    return sqlite_db, f"sqlite+aiosqlite:///{tmp_path / 't.db'}"


@pytest.fixture
def statements():
    seen: list[str] = []

    def _record(conn, cursor, statement, parameters, context, executemany):
        seen.append(statement)

    event.listen(Engine, "before_cursor_execute", _record)
    try:
        yield seen
    finally:
        event.remove(Engine, "before_cursor_execute", _record)


def test_dry_run_prints_the_plan_and_writes_nothing(live_like_db, statements, capsys):
    Session, url = live_like_db
    before = _raw_row(Session)
    statements.clear()
    script = _load(SCRIPT, "_reconcile_live_config")
    assert script.main(["--database-url", url]) == 0
    out = capsys.readouterr().out
    assert "DRY RUN" in out and "nothing written" in out
    assert "max_lots_per_trade" in out and "trade_amount" in out
    assert "0013" in out
    assert "UPDATE trading_config SET" in out
    assert statements, "the dry run executed no SQL"
    bad = [s for s in statements if not s.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
    assert bad == []
    assert _raw_row(Session) == before


def test_apply_without_confirm_db_refuses(live_like_db, capsys):
    Session, url = live_like_db
    before = _raw_row(Session)
    script = _load(SCRIPT, "_reconcile_live_config")
    assert script.main(["--database-url", url, "--apply"]) == 2
    assert "confirm-db" in capsys.readouterr().err
    assert _raw_row(Session) == before


def test_apply_with_the_wrong_confirm_db_refuses(live_like_db, capsys):
    Session, url = live_like_db
    before = _raw_row(Session)
    script = _load(SCRIPT, "_reconcile_live_config")
    assert script.main(["--database-url", url, "--apply", "--confirm-db", "wrong"]) == 2
    assert "confirm-db" in capsys.readouterr().err
    assert _raw_row(Session) == before


def test_database_url_is_required():
    script = _load(SCRIPT, "_reconcile_live_config")
    with pytest.raises(SystemExit) as ei:
        script.main([])
    assert ei.value.code == 2


# ── the actionable startup error ─────────────────────────────────────────────

def test_startup_error_names_the_field_and_the_preview_command(runtime_ctx, sqlite_db, caplog):
    run(seed_config(sqlite_db))
    with using(with_params(runtime_ctx, max_lots_per_trade=1)):
        with pytest.raises(StartupAssertionError) as ei:
            run(run_startup_checks(require_config_row=True))
    msg = str(ei.value)
    assert "max_lots_per_trade" in msg and "reconcile_live_config.py" in msg
    last = ei.value.violations[-1]
    assert last.startswith("fix trading_config:") and "max_lots_per_trade (1 ->" in last
    # the original check-6 string is unchanged
    assert "params: max_lots_per_trade=1 must be >= 2 when partial_exit_enabled" \
        in ei.value.violations
    assert any(r.levelname == "CRITICAL" and "reconcile_live_config.py" in r.getMessage()
               for r in caplog.records)
