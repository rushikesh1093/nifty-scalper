"""Migration 0010 model columns round-trip on an isolated SQLite DB."""
from datetime import date, datetime, timezone

from tests.helpers import add_position, get_config_row, get_position, run, seed_config


def test_new_columns_round_trip(sqlite_db):
    run(seed_config(sqlite_db, trading_halted=True, halted_reason="test",
                    halted_at=datetime(2026, 10, 6, 4, 0, tzinfo=timezone.utc),
                    daily_trades_date=date(2026, 10, 6)))
    pid = run(add_position(sqlite_db, initial_stop_loss=85.0, statutory_charges=12.5))

    cfg = run(get_config_row(sqlite_db))
    assert cfg.trading_halted is True
    assert cfg.halted_reason == "test"
    assert cfg.daily_trades_date == date(2026, 10, 6)

    pos = run(get_position(sqlite_db, pid))
    assert pos.initial_stop_loss == 85.0
    assert pos.statutory_charges == 12.5


def test_defaults_backward_compatible(sqlite_db):
    run(seed_config(sqlite_db))
    pid = run(add_position(sqlite_db, initial_stop_loss=None, statutory_charges=None))
    cfg = run(get_config_row(sqlite_db))
    assert cfg.trading_halted is False
    assert cfg.daily_trades_date is None
    pos = run(get_position(sqlite_db, pid))
    assert pos.initial_stop_loss is None


def test_migration_0010_chain():
    import importlib.util
    import pathlib

    path = pathlib.Path(__file__).resolve().parents[1] / "migrations" / "versions" / \
        "0010_live_fixes_risk_columns.py"
    spec = importlib.util.spec_from_file_location("m0010", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0010"
    assert mod.down_revision == "0009"
