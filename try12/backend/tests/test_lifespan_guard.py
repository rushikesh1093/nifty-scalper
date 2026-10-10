"""
The lifespan cannot swallow the paper guard (design §6.6, §23 R4 finding 1).

Before this change main.py ran ensure_config inside `except Exception: log`,
so any assertion placed there was caught and the API served anyway. These
tests drive `async with lifespan(app)` itself, plus a static check that the
guard calls are never re-wrapped in a swallowing handler.
"""
import ast
from pathlib import Path

import pytest
from sqlalchemy import text

from app.db.models.paper_trade import TradingMode
from tests.helpers import run, seed_config

MAIN = Path(__file__).resolve().parents[1] / "app" / "main.py"
GUARD_CALLS = {"assert_paper_mode", "run_startup_checks"}


# `lifespan_spies` lives in conftest.py (shared with test_request_context.py).


async def _enter_lifespan():
    from app.main import app, lifespan
    reached = False
    async with lifespan(app):
        reached = True
    return reached


# ── (a) bad rows refuse to start, before the feed ────────────────────────────

@pytest.mark.parametrize("overrides, key", [
    ({"mode": TradingMode.LIVE}, "mode"),
    ({"live_mode_confirmed": True}, "live_mode_confirmed"),
])
def test_bad_row_raises_and_never_starts_feed(sqlite_db, lifespan_spies, overrides, key):
    from app.services.trading.startup_checks import StartupAssertionError
    run(seed_config(sqlite_db, **overrides))
    with pytest.raises(StartupAssertionError) as ei:
        run(_enter_lifespan())
    assert key in str(ei.value)
    assert "market.startup" not in lifespan_spies
    assert "runner.start" not in lifespan_spies
    assert "redis.disconnect" in lifespan_spies


def test_unparseable_mode_raises_startup_error_not_lookup_error(sqlite_db, lifespan_spies):
    from app.services.trading.startup_checks import StartupAssertionError
    run(seed_config(sqlite_db))

    async def _corrupt():
        async with sqlite_db() as db:
            await db.execute(text("UPDATE trading_config SET mode = 'FOO'"))
            await db.commit()

    run(_corrupt())
    with pytest.raises(StartupAssertionError) as ei:
        run(_enter_lifespan())
    assert "mode" in str(ei.value)
    assert not isinstance(ei.value.__cause__, LookupError)
    assert "market.startup" not in lifespan_spies


# ── (b) an unreadable DB is fatal ────────────────────────────────────────────

def test_unreadable_db_is_fatal(lifespan_spies):
    # no sqlite_db: the conftest raising session factory stands in for a dead DB
    from app.services.trading.startup_checks import StartupAssertionError
    with pytest.raises(StartupAssertionError) as ei:
        run(_enter_lifespan())
    assert "startup could not prove paper mode" in str(ei.value)
    assert "market.startup" not in lifespan_spies
    assert "redis.disconnect" in lifespan_spies


# ── (c) loop-start failures are still tolerated ──────────────────────────────

def test_valid_row_with_failing_runner_still_serves(sqlite_db, lifespan_spies, monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner
    run(seed_config(sqlite_db, max_lots_per_trade=2, max_daily_loss=500.0))
    monkeypatch.setattr(strategy_runner, "start", lifespan_spies.rec("runner.start", RuntimeError("boom")))
    assert run(_enter_lifespan()) is True
    assert lifespan_spies.index("market.startup") < lifespan_spies.index("runner.start")


# ── (d) static: the guard calls are never inside a swallowing handler ────────

def _parents(tree: ast.AST) -> dict:
    out = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            out[child] = node
    return out


def _catches_broad(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    if t is None:
        return True
    names = [e for e in (t.elts if isinstance(t, ast.Tuple) else [t])]
    return any(isinstance(n, ast.Name) and n.id in {"Exception", "BaseException"} for n in names)


def _reraises_startup_error(handler: ast.ExceptHandler) -> bool:
    t = handler.type
    named = isinstance(t, ast.Name) and t.id == "StartupAssertionError"
    bare_raise = any(isinstance(n, ast.Raise) and n.exc is None for n in ast.walk(handler))
    return named and bare_raise


def _swallowing_wrappers(src: str) -> list[str]:
    tree = ast.parse(src)
    parents = _parents(tree)
    problems, found = [], 0
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and getattr(node.func, "id", None) in GUARD_CALLS):
            continue
        found += 1
        child, cur = node, parents.get(node)
        while cur is not None:
            if isinstance(cur, ast.Try) and child in cur.body:
                safe = False
                for h in cur.handlers:
                    if _reraises_startup_error(h):
                        safe = True
                    elif _catches_broad(h) and not safe:
                        problems.append(f"{node.func.id} at line {node.lineno} is swallowed "
                                        f"by the handler at line {h.lineno}")
            child, cur = cur, parents.get(cur)
    if found < 2:
        problems.append(f"expected both guard calls in main.py, found {found}")
    return problems


def test_guard_calls_not_inside_swallowing_try():
    assert _swallowing_wrappers(MAIN.read_text(encoding="utf-8")) == []


def test_ast_check_detects_a_swallowing_wrapper():
    """Negative control: re-wrapping the guard in `except Exception: log` fails."""
    bad = (
        "async def lifespan(app):\n"
        "    try:\n"
        "        await assert_paper_mode(f, allow_absent=True)\n"
        "        await run_startup_checks(require_config_row=True)\n"
        "    except Exception as exc:\n"
        "        logger.error(exc)\n"
    )
    assert len(_swallowing_wrappers(bad)) == 2
