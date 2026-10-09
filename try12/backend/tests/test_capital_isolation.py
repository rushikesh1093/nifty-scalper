"""Capital in use is DERIVED per run_id (design §9.2, R5 finding 2): nothing is
reserved, so nothing can leak; one run's capital never constrains another's; and the
checks that make it so are enforced by static scans.

All five cases of §23 R5 finding 2 live here: (a) and (b) read the shared
four-trade replay runs (tests/replay_fixtures.py), (c) restarts the lifespan
over a FAILED replay's leftover open position, (d) and (e) are below them."""
import ast
import asyncio
from datetime import time as dtime
from pathlib import Path

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import PaperPosition
from app.runtime.context import using
from app.services.trading import risk_gate, risk_state
from app.services.trading.mode_manager import mode_manager
from tests.helpers import (
    add_position, allow_regime, position_fields, pin_ist_time, run, seed_config, with_cfg,
)

APP = Path(__file__).resolve().parents[1] / "app"
SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


@pytest.fixture
def day(runtime_ctx, sqlite_db, monkeypatch):
    run(seed_config(sqlite_db, capital=200_000.0))        # the live row says ₹200,000
    pin_ist_time(monkeypatch, dtime(10, 0))
    allow_regime(monkeypatch)
    return runtime_ctx


def _used(Session, run_id="PAPER"):
    async def _go():
        async with Session() as db:
            return await risk_state.open_capital_used(db, run_id)
    return run(_go())


def _count(Session):
    async def _go():
        async with Session() as db:
            return len((await db.execute(select(PaperPosition))).scalars().all())
    return run(_go())


def _enter(Session, ctx, *, capital_used, advisory_used=0.0, insert=None, timeout=5.0):
    async def _insert(db):
        db.add(PaperPosition(**position_fields(run_id="PAPER", entry_time=ctx.clock.now_utc(),
                                               trade_amount_used=capital_used)))
        await db.flush()
        return "inserted"

    async def _go():
        async with Session() as db:
            return await risk_gate.enter(
                db, run_id="PAPER", trading_day=ctx.clock.trading_day(),
                capital_used=capital_used, advisory_used=advisory_used,
                insert=insert or _insert, lock_timeout_s=timeout)
    return _go()


# ── case (a): a live open position does not constrain a replay ───────────────

def test_a_replay_beside_a_live_open_position_matches_a_clean_database(four_trade):
    """DB A holds an open PAPER position using ₹60,000 of a ₹200,000 row; DB B has
    neither. The replay (capital ₹200,000) completes and trades identically, and
    the live figures are untouched."""
    from tests.replay_fixtures import assert_complete
    assert_complete(four_trade.run1)
    assert_complete(four_trade.runB)
    assert four_trade.run1.metrics.trades == four_trade.runB.metrics.trades == 4
    assert four_trade.run1.determinism_hash == four_trade.runB.determinism_hash
    assert four_trade.row_after_run1.used_capital == 0.0          # never written by replay
    assert _used(four_trade.SessA, "PAPER") == 60_000.0
    assert _used(four_trade.SessA, four_trade.run1.run_id) == 0.0  # squared off


# ── case (b): mutating the live row between two replays changes nothing ─────

def test_two_replays_ignore_the_live_rows_capital_and_used_capital(four_trade):
    assert four_trade.row_after_run2.capital == 50_000.0
    assert four_trade.row_after_run2.used_capital == 150_000.0
    assert four_trade.run1.params_hash == four_trade.run2.params_hash
    assert four_trade.run1.determinism_hash == four_trade.run2.determinism_hash


# ── case (c): a FAILED run's leftover open position is invisible to live ────

FAILED_RUN = "RPL-01FAILEDRUN000000000000000"[:30]


def test_a_failed_runs_open_position_is_ignored_after_a_lifespan_restart(
        sqlite_db, lifespan_spies, monkeypatch):
    from datetime import date
    from app.db.models.replay import ReplayRun
    from app.main import app, lifespan
    from app.runtime.context import using
    from app.services.trading.paper_engine import paper_engine
    from tests.helpers import get_config_row, get_position
    import httpx

    run(seed_config(sqlite_db, capital=200_000.0, used_capital=0.0, max_lots_per_trade=3,
                    max_open_positions=3, max_daily_loss=6_000.0, max_daily_loss_pct=3.0,
                    max_trades_per_day=6))
    leftover = run(add_position(sqlite_db, run_id=FAILED_RUN, trade_amount_used=50_000.0))

    async def _failed_run():
        async with sqlite_db() as db:
            db.add(ReplayRun(run_id=FAILED_RUN, date_from=date(2026, 9, 1),
                             date_to=date(2026, 9, 1), underlyings=["NIFTY"], params={},
                             params_hash="x", seed=1, code_version="test", status="FAILED",
                             notes="Traceback: simulated"))
            await db.commit()
    run(_failed_run())

    checked = []

    async def _spy_check(pos, now_ist, square_off=None):
        checked.append(pos.id)

    monkeypatch.setattr(paper_engine, "_check_position", _spy_check)

    async def _restart_and_probe():
        async with lifespan(app):                    # ensure_config + reconcile_used_capital
            with using(app.state.runtime_context):
                await paper_engine.monitor_once()    # the live open-position load
                squared = await paper_engine.square_off_all("EOD")   # the EOD site
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://test") as c:
                resp = await c.put("/api/v1/trading/capital/set", params={"capital": 250_000})
        return squared, resp
    squared, resp = run(_restart_and_probe())

    assert run(get_config_row(sqlite_db)).used_capital == 0.0      # reconcile ignored it
    assert checked == []                                          # monitor never loaded it
    assert squared == {"closed": 0, "stale": [], "failed": []}   # EOD never saw it
    assert run(get_position(sqlite_db, leftover)).is_open is True
    assert resp.status_code == 200, resp.text                     # not blocked by it
    assert run(get_config_row(sqlite_db)).capital == 250_000.0
    assert _used(sqlite_db, FAILED_RUN) == 50_000.0               # left under its own run id


# ── the four mutators are gone ───────────────────────────────────────────────

@pytest.mark.parametrize("name", ["reserve_capital", "release_reserved_capital",
                                  "on_position_opened", "on_position_closed",
                                  "kelly_trade_amount"])
def test_mode_manager_has_no_capital_mutators(name):
    assert not hasattr(mode_manager, name)


# ── case (d): the ceiling is the frozen RunConfig, never the live row ────────

def test_the_ceiling_is_ctx_cfg_capital_not_the_live_row(day, sqlite_db):
    run(add_position(sqlite_db, trade_amount_used=20_000.0))
    with using(with_cfg(day, capital=40_000.0)):
        d = run(_enter(sqlite_db, day, capital_used=20_500.0))      # 20,000 + 20,500 > 40,000
    assert d.allowed is False and d.code.value == "CAPITAL_RACE_LOST"
    assert d.context["capital"] == 40_000.0 and d.context["locked_used"] == 20_000.0
    assert _count(sqlite_db) == 1
    # the same entry is fine against the row's ₹200,000 — the row is not consulted
    assert run(_enter(sqlite_db, day, capital_used=20_500.0)).allowed is True


def test_a_concurrent_commit_between_check_12_and_13b_loses_the_race(day, sqlite_db):
    """Check 12 reads free capital (advisory); another entry commits; 13b catches it."""
    with using(with_cfg(day, capital=30_000.0)):
        advisory = _used(sqlite_db)                                  # 0: looks free
        run(add_position(sqlite_db, trade_amount_used=20_000.0))     # the concurrent entry
        d = run(_enter(sqlite_db, day, capital_used=20_000.0, advisory_used=advisory))
    assert d.allowed is False and d.code.value == "CAPITAL_RACE_LOST"
    assert d.context["advisory_used"] == 0.0 < d.context["locked_used"] == 20_000.0
    assert _count(sqlite_db) == 1


def test_another_runs_positions_do_not_consume_this_runs_headroom(day, sqlite_db):
    run(add_position(sqlite_db, run_id="RPL-failed", trade_amount_used=190_000.0))
    assert _used(sqlite_db, "PAPER") == 0.0 and _used(sqlite_db, "RPL-failed") == 190_000.0
    assert run(_enter(sqlite_db, day, capital_used=20_000.0)).allowed is True


# ── every abort path leaves no row and capital unchanged ─────────────────────

def _abort_paths(day):
    """(label, ctx to run under, enter kwargs, expected code or exception)."""
    async def _boom(db):
        db.add(PaperPosition(**position_fields(run_id="PAPER", trade_amount_used=5_000.0)))
        await db.flush()
        raise RuntimeError("insert failed")
    return [
        ("DAILY_TRADE_CAP", with_cfg(day, max_trades_per_day=1), dict(capital_used=1_000.0)),
        ("CAPITAL_RACE_LOST", with_cfg(day, capital=10_000.0), dict(capital_used=11_000.0)),
        ("DAILY_TRADE_CAP_RACE", day, dict(capital_used=1_000.0, timeout=0.05)),
        ("insert exception", day, dict(capital_used=5_000.0, insert=_boom)),
    ]


@pytest.mark.parametrize("label", ["DAILY_TRADE_CAP", "CAPITAL_RACE_LOST",
                                   "DAILY_TRADE_CAP_RACE", "insert exception"])
def test_every_abort_path_leaves_no_row_and_open_capital_unchanged(day, sqlite_db, label):
    run(add_position(sqlite_db, trade_amount_used=3_000.0,         # the one existing position
                     entry_time=day.clock.now_utc()))
    before = (_count(sqlite_db), _used(sqlite_db))
    _, rc, kwargs = next(p for p in _abort_paths(day) if p[0] == label)

    async def _go():
        if label == "DAILY_TRADE_CAP_RACE":
            lock = risk_gate._sqlite_locks.setdefault(("PAPER", day.clock.trading_day()),
                                                      asyncio.Lock())
            await lock.acquire()
            try:
                return await _enter(sqlite_db, day, **kwargs)
            finally:
                lock.release()
        if label == "insert exception":
            with pytest.raises(RuntimeError):
                await _enter(sqlite_db, day, **kwargs)
            return None
        return await _enter(sqlite_db, day, **kwargs)

    with using(rc):
        d = run(_go())
    if label != "insert exception":
        assert d.allowed is False and d.code.value == label
    assert (_count(sqlite_db), _used(sqlite_db)) == before        # nothing to release


def test_partial_and_final_close_shrink_open_capital_without_any_release_call(day, sqlite_db,
                                                                               fake_redis):
    """The sum follows trade_amount_used, which the close paths already maintain."""
    from tests.helpers import get_position
    from app.services.trading.paper_engine import paper_engine
    pid = run(add_position(sqlite_db, lots=2, quantity=130, original_quantity=130,
                           original_lots=2, trade_amount_used=13_000.0, initial_risk=15.0,
                           r_source="ENTRY"))
    assert _used(sqlite_db) == 13_000.0
    fake_redis.put_tick("OPT1", 125.0, bid=124.9, ask=125.1, age_s=1)
    pos = run(get_position(sqlite_db, pid))
    run(paper_engine._check_position(pos, dtime(10, 0), dtime(15, 15)))     # progress 0.83
    after_partial = _used(sqlite_db)
    assert after_partial == pytest.approx(6_500.0)                          # half released
    run(paper_engine._close_position(pid, 125.0, "MANUAL"))
    assert _used(sqlite_db) == 0.0


# ── case (e): static scans ───────────────────────────────────────────────────

SHARED_MODELS = {"PaperPosition", "StrategySignal", "DailyPnLSummary", "StrategyPerf",
                 "RegimePerformanceModel", "RegimeStateModel", "PositionEvent"}
BUILDERS = {"select", "update", "delete"}


def _parents(tree):
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _chain_calls(top):
    calls, cur = [], top
    while isinstance(cur, ast.Call):
        calls.append(cur)
        cur = cur.func.value if isinstance(cur.func, ast.Attribute) else None
    return calls


def _scoped(top) -> bool:
    """A run_id predicate, or an identity predicate (Model.id == …: the id came from a
    run-scoped query), in any where()/filter() of the builder chain."""
    for call in _chain_calls(top):
        if not (isinstance(call.func, ast.Attribute)
                and call.func.attr in ("where", "filter", "filter_by")):
            continue
        for cmp in (n for a in call.args for n in ast.walk(a) if isinstance(n, ast.Compare)):
            for side in [cmp.left, *cmp.comparators]:
                if isinstance(side, ast.Attribute):
                    if side.attr == "run_id":
                        return True
                    if side.attr == "id" and isinstance(side.value, ast.Name) \
                            and side.value.id in SHARED_MODELS:
                        return True
    return False


def scan_unscoped(source: str, *, writes_only: bool = False) -> list[int]:
    """Line numbers of select/update/delete over a run-shared model that carry no
    run_id (or identity) predicate. ``writes_only`` exempts select(): the rule for
    read-only operator diagnostics under scripts/."""
    tree = ast.parse(source)
    parents = _parents(tree)
    hits = []
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in BUILDERS):
            continue
        if writes_only and node.func.id == "select":
            continue
        if not ({n.id for a in node.args for n in ast.walk(a) if isinstance(n, ast.Name)}
                & SHARED_MODELS):
            continue
        top = node
        while True:
            p = parents.get(top)
            if isinstance(p, ast.Attribute) and p.value is top:
                pp = parents.get(p)
                if isinstance(pp, ast.Call) and pp.func is p:
                    top = pp
                    continue
            break
        if not _scoped(top):
            hits.append(node.lineno)
    return hits


def _scan_tree(root: Path, **kw) -> dict:
    out = {}
    for path in sorted(root.rglob("*.py")):
        hits = scan_unscoped(path.read_text(encoding="utf-8"), **kw)
        if hits:
            out[path.relative_to(root.parent).as_posix()] = hits
    return out


def test_trading_package_queries_are_run_scoped():
    assert _scan_tree(APP / "services" / "trading") == {}


def test_the_scan_fails_on_an_unscoped_load(tmp_path):
    bad = ("from sqlalchemy import select\n"
           "async def f(db):\n"
           "    return await db.execute(select(PaperPosition).where(PaperPosition.is_open == True))\n")
    assert scan_unscoped(bad) == [3]
    # an unscoped count and an unscoped delete are caught too
    assert scan_unscoped("x = select(func.count(PaperPosition.id)).where(PaperPosition.is_open)") == [1]
    assert scan_unscoped("x = delete(StrategySignal)") == [1]


def test_the_scan_passes_run_scoped_and_identity_scoped_queries():
    ok = ("a = select(PaperPosition).where(PaperPosition.run_id == rid, PaperPosition.is_open)\n"
          "b = update(PaperPosition).where(PaperPosition.id == pid).values(x=1)\n"
          "c = select(Instrument).where(Instrument.name == 'x')\n")
    assert scan_unscoped(ok) == []


def test_scripts_rule_exempts_selects_but_not_writes():
    src = ("a = select(PaperPosition).where(PaperPosition.is_open)\n"
           "b = update(PaperPosition).where(PaperPosition.strategy_name == 'X').values(x=1)\n")
    assert scan_unscoped(src, writes_only=True) == [2]
    assert scan_unscoped(src) == [1, 2]


def test_nothing_reads_the_used_capital_cache_outside_reconcile():
    """trading_config.used_capital is a PAPER display cache with one writer; no
    decision and no API response may read it (design §9.2)."""
    hits = []
    for path in sorted(APP.rglob("*.py")):
        rel = path.relative_to(APP.parent).as_posix()
        if rel == "app/db/models/paper_trade.py":                  # the column declaration
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        allowed = set()
        for fn in ast.walk(tree):
            if isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                    and rel == "app/services/trading/mode_manager.py" \
                    and fn.name == "reconcile_used_capital":
                allowed |= {id(n) for n in ast.walk(fn)}
        for n in ast.walk(tree):
            named = (isinstance(n, ast.Attribute) and n.attr == "used_capital") or \
                    (isinstance(n, ast.keyword) and n.arg == "used_capital")
            if named and id(n) not in allowed:
                hits.append(f"{rel}:{getattr(n, 'lineno', getattr(n.value, 'lineno', 0))}")
    assert hits == []
