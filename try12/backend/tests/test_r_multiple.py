"""Bug 5 (design §8.4, R6): R is measured against the INITIAL risk, written once
at entry, never against a trailed stop.

test_trailed_and_untrailed_trades_report_the_same_r imports only names master
also has (tests/portable.py), so it runs against master for the fail-before:
there R comes from the trailed stop_loss.
"""
import ast
import re
from datetime import time as dtime
from pathlib import Path

import pytest

from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests import portable

APP_DIR = Path(__file__).resolve().parents[1] / "app"


@pytest.fixture
def pdb(tmp_path, monkeypatch):
    return portable.make_sqlite(tmp_path, monkeypatch)


def test_trailed_and_untrailed_trades_report_the_same_r(pdb, monkeypatch):
    redis = portable.PortableRedis(monkeypatch)
    portable.seed_config(pdb, used_capital=13000.0)
    trailed = portable.add_position(pdb, stop_loss=115.0, trailing_sl=115.0,
                                    entry_regime="BULL_TREND_NORMAL_VOL")
    plain = portable.add_position(pdb, entry_regime="BULL_TREND_NORMAL_VOL")
    seen = []

    async def _spy(*args, **kwargs):
        outcome = kwargs.get("outcome")
        seen.append(outcome.r_multiple if outcome is not None else kwargs.get("rr"))

    monkeypatch.setattr(strategy_orchestrator, "record_trade_result", _spy)
    redis.put_tick("OPT1", 130.0)
    for pid in (trailed, plain):
        portable.run(paper_engine._close_position(pid, 130.0, "TARGET"))
    expected = (round(130.0 * 0.999, 2) - 100.0) / 15.0          # exit 129.87, R = 15
    assert len(seen) == 2
    assert seen[0] == pytest.approx(seen[1])
    assert seen[0] == pytest.approx(expected, abs=1e-3)


# ── persisted R (branch fixtures) ────────────────────────────────────────────

def test_r_multiple_is_persisted_and_ignores_the_trailed_stop(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import add_position, get_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    common = dict(initial_risk=15.0, r_source="ENTRY", original_quantity=65, original_lots=1)
    trailed = run(add_position(sqlite_db, stop_loss=115.0, trailing_sl=115.0, **common))
    plain = run(add_position(sqlite_db, **common))
    fake_redis.put_tick("OPT1", 130.0, age_s=1)
    for pid in (trailed, plain):
        run(paper_engine._close_position(pid, 130.0, "TARGET"))
    a, b = run(get_position(sqlite_db, trailed)), run(get_position(sqlite_db, plain))
    expected = (round(130.0 * 0.999, 2) - 100.0) / 15.0
    assert a.r_multiple == pytest.approx(expected) and b.r_multiple == pytest.approx(expected)
    assert a.exit_value_weighted == pytest.approx(129.87)
    # net R carries the costs per unit of the original quantity
    cost_per_unit = (a.brokerage + a.statutory_charges) / 65
    assert a.r_multiple_net == pytest.approx((129.87 - 100.0 - cost_per_unit) / 15.0)
    assert a.r_multiple_net < a.r_multiple
    assert (a.initial_stop_loss, a.initial_risk, a.r_source) == (85.0, 15.0, "ENTRY")


def test_r_multiple_weights_the_partial_and_final_legs(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import add_position, get_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pid = run(add_position(sqlite_db, lots=2, quantity=130, original_quantity=130,
                           original_lots=2, trade_amount_used=13000.0, brokerage=40.0,
                           initial_risk=15.0, r_source="ENTRY"))
    for px in (116.0, 122.5):
        fake_redis.put_tick("OPT1", px, age_s=1)
        pos = run(get_position(sqlite_db, pid))
        run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    fake_redis.put_tick("OPT1", 130.0, age_s=1)
    run(paper_engine._close_position(pid, 130.0, "TARGET"))
    pos = run(get_position(sqlite_db, pid))
    evw = (65 * pos.partial_exit_price + 65 * pos.actual_exit) / 130
    assert pos.exit_value_weighted == pytest.approx(evw)
    assert pos.r_multiple == pytest.approx((evw - 100.0) / 15.0)


def test_missing_initial_risk_means_no_r(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import add_position, get_position, run, seed_config
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, initial_stop_loss=None, initial_risk=None,
                           r_source="BACKFILL_UNVERIFIED"))
    fake_redis.put_tick("OPT1", 130.0, age_s=1)
    run(paper_engine._close_position(pid, 130.0, "TARGET"))
    pos = run(get_position(sqlite_db, pid))
    assert pos.r_multiple is None and pos.r_multiple_net is None
    assert pos.r_source == "BACKFILL_UNVERIFIED"        # left as-is


def test_initial_risk_non_positive_rejects_the_entry(sqlite_db, fake_redis, monkeypatch):
    from sqlalchemy import select

    from app.db.models.paper_trade import PaperPosition
    from app.db.models.risk import NoTradeDecision
    from app.services.trading import paper_engine as pe_mod
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.option_selector import option_selector
    from tests.helpers import allow_regime, fake_candidate, get_config_row, make_signal, run, \
        seed_config

    from tests.helpers import pin_ist_time, stub_select

    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 30))
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))
    allow_regime(monkeypatch)

    assert run(paper_engine.handle_signal(make_signal(sl_pct=0.0))) is False   # SL at entry

    async def _rows():
        async with sqlite_db() as db:
            return ((await db.execute(select(PaperPosition))).scalars().all(),
                    (await db.execute(select(NoTradeDecision))).scalars().all())
    positions, decisions = run(_rows())
    assert positions == []
    assert [d.code for d in decisions] == ["INITIAL_RISK_NON_POSITIVE"]
    cfg = run(get_config_row(sqlite_db))
    assert cfg.used_capital == 0.0 and cfg.daily_trades_count == 0   # nothing opened


# ── R-sign buckets with pinned quotes (§23, R5 finding 6) ────────────────────

def _breakeven_exit(Session, fake_redis, *, bid, ask, stop=200.0, ltp=200.0):
    from tests.helpers import add_position, get_position, run
    pid = run(add_position(
        Session, entry_price=200.0, actual_entry=200.0, stop_loss=stop, trailing_sl=stop,
        initial_stop_loss=170.0, initial_risk=30.0, r_source="ENTRY", target=260.0,
        original_quantity=65, original_lots=1, trade_amount_used=13000.0,
    ))
    fake_redis.put_tick("OPT1", ltp, bid=bid, ask=ask, age_s=1)
    pos = run(get_position(Session, pid))
    run(paper_engine._check_position(pos, dtime(11, 0), dtime(15, 15)))
    return run(get_position(Session, pid))


def test_breakeven_exit_on_a_tight_quote_is_r_flat(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pos = _breakeven_exit(sqlite_db, fake_redis, bid=199.85, ask=200.15)
    assert pos.exit_reason == "SL" and pos.exit_fill_source == "BID"
    assert pos.actual_exit == 199.75                    # min(floor_tick(199.85 × 0.9995), 200)
    assert pos.r_multiple == pytest.approx(-0.25 / 30)  # −0.0083: inside ±R_FLAT_EPSILON
    assert pos.net_pnl < 0                              # a loss by its costs


def test_small_positive_r_with_negative_net_is_r_win(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pos = _breakeven_exit(sqlite_db, fake_redis, bid=201.05, ask=201.35, stop=201.0, ltp=201.0)
    assert pos.actual_exit == 200.90 and pos.r_multiple == pytest.approx(0.03)
    assert pos.net_pnl < -1.0


def test_breakeven_exit_at_the_spread_cap_is_r_loss(runtime_ctx, sqlite_db, fake_redis):
    from tests.helpers import run, seed_config
    run(seed_config(sqlite_db, used_capital=13000.0))
    pos = _breakeven_exit(sqlite_db, fake_redis, bid=195.00, ask=205.00)
    assert pos.exit_fill_source == "BID" and pos.actual_exit == 194.90
    assert pos.r_multiple == pytest.approx(-5.1 / 30)   # −0.17


def test_reconcile_buckets_the_three_fixtures_by_r_sign(runtime_ctx, sqlite_db, fake_redis):
    """Buckets key on the sign of the gross r_multiple, never on net P&L (§9.5)."""
    from sqlalchemy import select

    from app.db.models.paper_trade import StrategyPerf
    from app.services.trading.reconcile import reconcile_performance
    from tests.helpers import run, seed_config

    run(seed_config(sqlite_db, used_capital=39000.0))
    _breakeven_exit(sqlite_db, fake_redis, bid=199.85, ask=200.15)                 # R_FLAT
    _breakeven_exit(sqlite_db, fake_redis, bid=201.05, ask=201.35, stop=201.0, ltp=201.0)  # R_WIN
    _breakeven_exit(sqlite_db, fake_redis, bid=195.00, ask=205.00)                 # R_LOSS
    run(reconcile_performance("PAPER"))

    async def _perf():
        async with sqlite_db() as db:
            return (await db.execute(select(StrategyPerf))).scalar_one()
    sp = run(_perf())
    assert sp.r_sample_size == 3
    assert (sp.winning_trades_r, sp.losing_trades_r) == (1, 1)     # the flat one is in neither
    assert sp.losing_trades == 3 and sp.winning_trades == 0        # all three lose money
    assert sp.avg_loss_r == pytest.approx(0.17)


# ── static: write-once fields (§8.4, §23) ────────────────────────────────────

WRITE_ONCE = {"initial_stop_loss", "initial_risk", "r_source"}


def _write_once_hits(tree):
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            f = node.func
            name = f.attr if isinstance(f, ast.Attribute) else getattr(f, "id", "")
            if name in ("values", "update"):
                for kw in node.keywords:
                    if kw.arg in WRITE_ONCE:
                        hits.append((node.lineno, kw.arg))
    return hits


def test_write_once_fields_never_appear_in_an_update():
    hits = []
    for path in APP_DIR.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        hits += [f"{path.relative_to(APP_DIR).as_posix()}:{ln} {k}"
                 for ln, k in _write_once_hits(tree)]
    assert hits == []


def test_write_once_scan_catches_a_synthetic_update():
    tree = ast.parse("db.execute(update(P).values(stop_loss=1, initial_risk=2))\n")
    assert _write_once_hits(tree) == [(1, "initial_risk")]


def test_no_initial_stop_loss_or_default_under_app():
    pattern = re.compile(r"initial_stop_loss\s+or\s+")
    hits = [f"{p.relative_to(APP_DIR).as_posix()}:{i}"
            for p in APP_DIR.rglob("*.py")
            for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1)
            if pattern.search(line)]
    assert hits == []
