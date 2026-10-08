"""
Startup check 1 — the paper guard is falsifiable (design §6.3 check 1, §11,
R1.3, R1.5).

ensure_config no longer silently corrects mode / live_mode_confirmed on an
existing row; it records CONFIG_INVALID and leaves the value in place, so the
raw-scalar assertion can see it and refuse to start.
"""
import logging

import pytest
from sqlalchemy import text

from app.db.models.paper_trade import TradingMode
from app.services.trading.mode_manager import mode_manager
from app.services.trading.startup_checks import (
    StartupAssertionError, assert_paper_mode, run_startup_checks,
)
from tests.helpers import run, seed_config


async def _raw_row(Session):
    async with Session() as db:
        return (await db.execute(text(
            "SELECT mode, live_mode_confirmed FROM trading_config WHERE id = 1"
        ))).one_or_none()


def _error_records(caplog):
    return [r for r in caplog.records if r.levelno >= logging.ERROR]


async def _full_startup_path(Session):
    """The live order (§6.6) minus the feed: preflight, ensure_config, checks."""
    v = await assert_paper_mode(Session, allow_absent=True)
    if v:
        raise StartupAssertionError(v)
    await mode_manager.ensure_config(session_factory=Session)
    await run_startup_checks(require_config_row=True)


def test_live_mode_row_refuses_to_start_and_is_not_rewritten(sqlite_db, caplog):
    run(seed_config(sqlite_db, mode=TradingMode.LIVE))
    caplog.set_level(logging.ERROR)
    with pytest.raises(StartupAssertionError) as ei:
        run(_full_startup_path(sqlite_db))
    assert "mode" in str(ei.value)
    assert _error_records(caplog)
    assert run(_raw_row(sqlite_db))[0] == "LIVE"


def test_live_confirmed_row_refuses_to_start_and_is_not_rewritten(sqlite_db, caplog):
    run(seed_config(sqlite_db, live_mode_confirmed=True))
    caplog.set_level(logging.ERROR)
    with pytest.raises(StartupAssertionError) as ei:
        run(_full_startup_path(sqlite_db))
    assert "live_mode_confirmed" in str(ei.value)
    assert _error_records(caplog)
    assert bool(run(_raw_row(sqlite_db))[1]) is True


def test_unparseable_mode_refuses_to_start_naming_mode(sqlite_db, caplog):
    """Defence, not reproduction: PostgreSQL's native enum type makes this state
    unrepresentable in production. It guards against a dialect or migration that
    drops the constraint. The raw read must raise StartupAssertionError naming
    `mode`, not leak the ORM's LookupError."""
    run(seed_config(sqlite_db))

    async def _corrupt():
        async with sqlite_db() as db:
            await db.execute(text("UPDATE trading_config SET mode = 'FOO'"))
            await db.commit()

    run(_corrupt())
    caplog.set_level(logging.ERROR)
    with pytest.raises(StartupAssertionError) as ei:
        run(_full_startup_path(sqlite_db))
    assert "mode" in str(ei.value) and "FOO" in str(ei.value)
    assert _error_records(caplog)
    assert run(_raw_row(sqlite_db))[0] == "FOO"


def test_ensure_config_on_live_row_halts_and_does_not_patch(sqlite_db, caplog):
    run(seed_config(sqlite_db, mode=TradingMode.LIVE, live_mode_confirmed=True))
    caplog.set_level(logging.ERROR)
    run(mode_manager.ensure_config(session_factory=sqlite_db))
    mode, confirmed = run(_raw_row(sqlite_db))
    assert mode == "LIVE" and bool(confirmed) is True
    assert mode_manager.is_halted()
    assert any("CONFIG_INVALID" in r.getMessage() for r in _error_records(caplog))


def test_check_runs_on_its_own_and_lists_every_violation(sqlite_db):
    run(seed_config(sqlite_db, mode=TradingMode.LIVE, live_mode_confirmed=True))
    with pytest.raises(StartupAssertionError) as ei:
        run(run_startup_checks(require_config_row=True))
    msg = str(ei.value)
    assert "mode" in msg and "live_mode_confirmed" in msg
    assert len(ei.value.violations) >= 2


def test_preflight_accepts_absent_row_but_checks_require_it(sqlite_db):
    assert run(assert_paper_mode(sqlite_db, allow_absent=True)) == []
    assert run(assert_paper_mode(sqlite_db, allow_absent=False))
    with pytest.raises(StartupAssertionError):
        run(run_startup_checks(require_config_row=True))


def test_fresh_db_creates_paper_row_and_passes(sqlite_db):
    run(_full_startup_path(sqlite_db))
    mode, confirmed = run(_raw_row(sqlite_db))
    assert mode == "PAPER" and not confirmed
    assert not mode_manager.is_halted()


def test_update_config_forces_paper(sqlite_db):
    run(seed_config(sqlite_db))
    cfg = run(mode_manager.update_config(mode=TradingMode.LIVE, live_mode_confirmed=True))
    assert cfg.mode == TradingMode.PAPER and cfg.live_mode_confirmed is False
    mode, confirmed = run(_raw_row(sqlite_db))
    assert mode == "PAPER" and not confirmed


def test_ensure_config_requires_explicit_session_factory():
    with pytest.raises(TypeError):
        run(mode_manager.ensure_config())
