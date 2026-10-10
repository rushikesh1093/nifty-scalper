"""The read-only PAPER validation gate: build_paper_validation, GET
/api/v1/analytics/validation and the CLI (blockers-plan item 3, design §18, D10).

The six D10 criteria come from report.build_validation unchanged; these tests
prove the PAPER inputs are scoped to run_id='PAPER', that the route executes
only SELECT statements and that the CLI never needs a default database."""
import json
from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from sqlalchemy import event, func, select

from app.core.trading_params import TradingParams
from app.db.models.paper_trade import (
    DailyPnLSummary, PaperPosition, StrategyPerf, StrategySignal,
)
from app.db.models.regime import RegimePerformanceModel
from app.db.models.risk import CycleStats, NoTradeDecision
from app.services.replay import report
from tests.helpers import position_fields, run, seed_config

START = date(2026, 8, 3)                       # a Monday
R_PATTERN = [2.0, 2.0, -1.0, 2.0, -1.0]
NETS = [1950.0, 1950.0, -1050.0, 1950.0, -1050.0]
COUNTED_TABLES = (PaperPosition, StrategySignal, NoTradeDecision, CycleStats,
                  DailyPnLSummary, StrategyPerf, RegimePerformanceModel)


def _days(n, start=START):
    out, d = [], start
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def _closed(day, i, r, net, run_id="PAPER"):
    # 10:30 IST + i minutes on *day*: the exit's IST day is *day*
    at = datetime(day.year, day.month, day.day, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=i)
    return PaperPosition(**position_fields(
        is_open=False, net_pnl=net, gross_pnl=net + 50.0, exit_time=at,
        entry_time=at - timedelta(minutes=5), exit_reason="TARGET" if r > 0 else "SL",
        actual_exit=100.0 + 15.0 * r, initial_risk=15.0, r_source="ENTRY", r_multiple=r,
        r_multiple_net=r - 0.05, entry_regime="BULL_TREND_NORMAL_VOL",
        entry_fill_source="ASK", exit_fill_source="BID",
        sizing_detail={"method": "FIXED_FRACTION"}, run_id=run_id))


def _seed_book(Session, days, rs, nets, *, rpl_days=()):
    async def _go():
        async with Session() as db:
            for d in days:
                for i, (r, n) in enumerate(zip(rs, nets)):
                    db.add(_closed(d, i, r, n))
                db.add(CycleStats(run_id="PAPER", trading_day=d, underlying="NIFTY",
                                  candles_evaluated=300, strategies_evaluated=12,
                                  signals_emitted=len(rs), rejections_recorded=1,
                                  oi_iv_context_missing=0))
                db.add(NoTradeDecision(run_id="PAPER", trading_day=d,
                                       decided_at=datetime(d.year, d.month, d.day, 6, 0,
                                                           tzinfo=timezone.utc),
                                       stage="CYCLE", code="ATR_BELOW_FLOOR", underlying="NIFTY",
                                       reason="paper", context={}))
            for d in rpl_days:                 # a replay run's rows must never be counted
                for i in range(7):
                    db.add(_closed(d, i, -1.0, -1050.0, run_id="RPL-X"))
                db.add(CycleStats(run_id="RPL-X", trading_day=d, underlying="NIFTY",
                                  candles_evaluated=1))
            await db.commit()
    run(_go())


def _build(Session, **kw):
    from app.services.replay.paper_validation import build_paper_validation

    async def _go():
        async with Session() as db:
            return await build_paper_validation(db, params=TradingParams(), capital=200_000.0,
                                                iterations=300, seed=20260901, **kw)
    return run(_go())


def _criteria(rep):
    return {c["n"]: c for c in rep["sections"]["11"]["criteria"]}


# ── build_paper_validation ───────────────────────────────────────────────────

def test_paper_report_has_six_criteria_over_paper_rows_only(runtime_ctx, sqlite_db):
    days = _days(24)
    rpl = _days(30, start=date(2026, 6, 1))
    _seed_book(sqlite_db, days, R_PATTERN, NETS, rpl_days=rpl)
    rep = _build(sqlite_db)
    crit = _criteria(rep)
    assert sorted(crit) == [1, 2, 3, 4, 5, 6]
    assert crit[1]["actual"] == "120 closed trades over 24 trading days"     # no RPL-X rows
    assert rep["source"] == "PAPER" and rep["run_id"] == "PAPER"
    assert rep["synthetic"] is False
    one = rep["sections"]["1"]
    assert (one["date_from"], one["date_to"]) == (days[0].isoformat(), days[-1].isoformat())
    assert rep["sections"]["8"]["decisions"] == [
        {"stage": "CYCLE", "code": "ATR_BELOW_FLOOR", "count": 24}]
    assert rep["sections"]["8"]["cycle_totals"]["NIFTY"]["candles_evaluated"] == 300 * 24
    assert rep["sections"]["7"]["in_sample_days"] + rep["sections"]["7"]["oos_days"] == 24
    assert rep["recommendation"] in ("CONSIDER", "NOT YET")
    md = report.render_markdown(rep)
    assert "# Validation report — PAPER" in md and rep["recommendation_line"] in md


def test_a_thin_sample_is_not_yet_naming_criterion_1(runtime_ctx, sqlite_db):
    days = _days(3)
    _seed_book(sqlite_db, days[:1], [2.0, 2.0, -1.0], NETS[:3])
    _seed_book(sqlite_db, days[1:], [2.0], NETS[:1])
    rep = _build(sqlite_db)
    assert _criteria(rep)[1]["actual"] == "5 closed trades over 3 trading days"
    assert rep["recommendation"] == "NOT YET" and 1 in rep["failing_criteria"]
    assert "criterion 1 (Sample size)" in rep["recommendation_line"]
    report.render_markdown(rep)


def test_an_empty_run_is_not_yet_without_an_exception(runtime_ctx, sqlite_db):
    rep = _build(sqlite_db)
    assert rep["recommendation"] == "NOT YET" and 1 in rep["failing_criteria"]
    assert _criteria(rep)[1]["actual"] == "0 closed trades over 0 trading days"
    assert rep["sections"]["1"]["date_from"] == runtime_ctx.clock.trading_day().isoformat()
    report.render_markdown(rep)


def test_a_date_window_bounds_the_trades(runtime_ctx, sqlite_db):
    days = _days(10)
    _seed_book(sqlite_db, days, R_PATTERN, NETS)
    rep = _build(sqlite_db, date_from=days[2], date_to=days[5])
    assert _criteria(rep)[1]["actual"] == "20 closed trades over 4 trading days"


# ── the route ────────────────────────────────────────────────────────────────

def _get(path, **params):
    from app.main import app

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            return await c.get(path, params=params)
    return run(_go())


def _counts(Session):
    async def _go():
        async with Session() as db:
            return {m.__tablename__: int((await db.execute(
                select(func.count()).select_from(m))).scalar()) for m in COUNTED_TABLES}
    return run(_go())


def test_the_route_executes_only_selects_and_changes_no_row(runtime_ctx, sqlite_db):
    run(seed_config(sqlite_db, capital=200_000.0))
    days = _days(24)
    _seed_book(sqlite_db, days, R_PATTERN, NETS, rpl_days=_days(3, start=date(2026, 6, 1)))
    before = _counts(sqlite_db)
    statements = []
    engine = sqlite_db.kw["bind"].sync_engine

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = _get("/api/v1/analytics/validation", iterations=200)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["source"] == "PAPER" and len(body["sections"]["11"]["criteria"]) == 6
    assert _criteria(body)[1]["actual"] == "120 closed trades over 24 trading days"
    assert body["sections"]["1"]["bootstrap_iterations"] == 200
    assert statements, "the route ran no SQL at all"
    bad = [s for s in statements if not s.lstrip().upper().startswith(("SELECT", "PRAGMA"))]
    assert bad == []
    assert _counts(sqlite_db) == before


def test_the_route_rejects_replay_and_malformed_run_ids(runtime_ctx, sqlite_db):
    r = _get("/api/v1/analytics/validation", run_id="RPL-X")
    assert r.status_code == 400 and "/analytics/runs/RPL-X" in r.json()["detail"]
    assert _get("/api/v1/analytics/validation", run_id="bad").status_code == 400
    assert _get("/api/v1/analytics/validation", iterations=10).status_code == 422
    assert _get("/api/v1/analytics/validation", date_from="2026-09-10",
                date_to="2026-09-01").status_code == 400


# ── the CLI ──────────────────────────────────────────────────────────────────

def test_the_cli_reads_a_database_and_writes_paper_json(runtime_ctx, sqlite_db, tmp_path, capsys):
    from app.services.replay import paper_validation
    run(seed_config(sqlite_db, capital=200_000.0, max_lots_per_trade=2))
    _seed_book(sqlite_db, _days(5), R_PATTERN, NETS)
    before = _counts(sqlite_db)
    out = tmp_path / "out"
    url = f"sqlite+aiosqlite:///{tmp_path / 't.db'}"
    assert paper_validation.main(["--database-url", url, "--out", str(out),
                                  "--iterations", "200"]) == 0
    data = json.loads((out / "PAPER.json").read_text(encoding="utf-8"))
    assert "recommendation" in data and data["source"] == "PAPER"
    assert data["sections"]["1"]["params"]["cfg"] == {"capital": 200_000.0}
    assert data["sections"]["1"]["params"]["params"]["max_lots_per_trade"] == 2
    assert (out / "PAPER.md").exists()
    assert "RECOMMENDATION:" in capsys.readouterr().out
    assert _counts(sqlite_db) == before


def test_the_cli_requires_a_database_url():
    from app.services.replay import paper_validation
    with pytest.raises(SystemExit) as ei:
        paper_validation.main([])
    assert ei.value.code == 2


def test_the_cli_refuses_a_replay_run_id(tmp_path):
    from app.services.replay import paper_validation
    with pytest.raises(SystemExit) as ei:
        paper_validation.main(["--database-url", f"sqlite+aiosqlite:///{tmp_path / 'x.db'}",
                               "--run-id", "RPL-X"])
    assert ei.value.code == 2


def test_the_cli_returns_2_when_the_database_has_no_schema(tmp_path, capsys):
    from app.services.replay import paper_validation
    url = f"sqlite+aiosqlite:///{tmp_path / 'empty.db'}"
    assert paper_validation.main(["--database-url", url, "--iterations", "200"]) == 2
    assert "paper_validation failed" in capsys.readouterr().err
