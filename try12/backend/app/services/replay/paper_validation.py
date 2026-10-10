"""
The D10 go/no-go over the LIVE PAPER run (design §18, blockers-plan item 3).

``report.build_validation`` (six criteria, unchanged) used to run only inside
the replay path. This module gathers the same ``ReportInputs`` from the
``run_id='PAPER'`` rows and builds the report, READ-ONLY:

  * only SELECT statements are issued; on PostgreSQL every session first runs
    ``SET TRANSACTION READ ONLY``; the session is always rolled back, never
    committed;
  * only run_id PAPER is accepted (a replay run already stores its verdict on
    replay_runs.validation, served by GET /analytics/runs/{run_id}).

It lives in the replay package, not app/services/metrics, because it composes
replay.report and the decision code must never import app.services.replay
(tests/test_no_replay_branch.py).

Used by GET /api/v1/analytics/validation and by the CLI:

    python -m app.services.replay.paper_validation \\
        --database-url sqlite+aiosqlite:///copy.db [--from YYYY-MM-DD] [--to YYYY-MM-DD] \\
        [--iterations 10000] [--seed N] [--oos-split 0.70] [--out DIR]

``--database-url`` is required (no default: the operator names the database).
Exit codes: 0 report built (whatever the verdict), 2 usage or read error.
PAPER ONLY: nothing here places, modifies or cancels an order, and a CONSIDER
verdict authorises nothing.
"""
from __future__ import annotations

import argparse
import asyncio
import dataclasses
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Optional

from sqlalchemy import func, select, text

from app.services.metrics import trade_metrics as tm
from app.services.replay import report

PAPER_RUN_ID = "PAPER"
LABEL = "PAPER live paper run"


async def make_read_only(db) -> None:
    """PostgreSQL: the transaction refuses writes. SQLite has no equivalent; the
    caller's rollback (and the SELECT-only queries) carry the guarantee there."""
    if db.get_bind().dialect.name == "postgresql":
        await db.execute(text("SET TRANSACTION READ ONLY"))


def _today() -> date:
    from app.runtime.context import is_bound, trading_day
    from app.runtime.live import LiveClock          # unbound (CLI): the live clock, never a raw wall-clock read
    from app.services.trading.session_times import trading_day as ist_day
    return trading_day() if is_bound() else ist_day(LiveClock().now_utc())


def _params_record(params, capital: float) -> tuple[dict, str]:
    from app.runtime.serialize import canonical_json
    record = {"params": json.loads(canonical_json(params)), "cfg": {"capital": capital}}
    return record, hashlib.sha256(canonical_json(record).encode("utf-8")).hexdigest()


async def gather_paper_inputs(db, *, run_id: str = PAPER_RUN_ID, params, capital: float,
                              date_from: Optional[date] = None, date_to: Optional[date] = None,
                              iterations: int, seed: int,
                              oos_split: float = 0.70) -> report.ReportInputs:
    """ReportInputs for the PAPER run from its rows (SELECT only), shaped exactly as
    report.gather_inputs shapes a replay run's."""
    from app.db.models.paper_trade import StrategySignal
    from app.db.models.risk import CycleStats, NoTradeDecision
    from app.services.replay import coverage as coverage_mod

    if run_id != PAPER_RUN_ID:
        raise ValueError(f"run_id must be {PAPER_RUN_ID}; replay runs carry their own report")

    trades = await tm.load_trades(
        db, tm.TradeFilter(run_id=run_id, date_from=date_from, date_to=date_to),
        flat_epsilon=params.flat_epsilon)

    cs_q = select(CycleStats).where(CycleStats.run_id == run_id)
    ntd_q = select(NoTradeDecision.stage, NoTradeDecision.code, func.count(NoTradeDecision.id)) \
        .where(NoTradeDecision.run_id == run_id)
    sig_q = select(StrategySignal.strategy_name, func.count(StrategySignal.id)) \
        .where(StrategySignal.run_id == run_id)
    if date_from:
        cs_q = cs_q.where(CycleStats.trading_day >= date_from)
        ntd_q = ntd_q.where(NoTradeDecision.trading_day >= date_from)
        sig_q = sig_q.where(StrategySignal.signal_time >= tm.trading_day_bounds_utc(date_from)[0])
    if date_to:
        cs_q = cs_q.where(CycleStats.trading_day <= date_to)
        ntd_q = ntd_q.where(NoTradeDecision.trading_day <= date_to)
        sig_q = sig_q.where(StrategySignal.signal_time < tm.trading_day_bounds_utc(date_to)[1])
    cs = (await db.execute(cs_q.order_by(CycleStats.trading_day, CycleStats.underlying))
          ).scalars().all()
    ntd = (await db.execute(ntd_q.group_by(NoTradeDecision.stage, NoTradeDecision.code)
                            .order_by(NoTradeDecision.stage, NoTradeDecision.code))).all()
    sigs = (await db.execute(sig_q.group_by(StrategySignal.strategy_name))).all()
    hist = await coverage_mod.regime_histogram(db, run_id)

    totals: dict = {}
    for r in cs:
        t = totals.setdefault(r.underlying, {"candles_evaluated": 0, "strategies_evaluated": 0,
                                             "signals_emitted": 0, "rejections_recorded": 0,
                                             "oi_iv_context_missing": 0})
        t["candles_evaluated"] += r.candles_evaluated
        t["strategies_evaluated"] += r.strategies_evaluated
        t["signals_emitted"] += r.signals_emitted
        t["rejections_recorded"] += r.rejections_recorded
        t["oi_iv_context_missing"] += r.oi_iv_context_missing

    days = sorted({r.trading_day for r in cs} | {t.trading_day for t in trades})
    days = [d for d in days if (date_from is None or d >= date_from)
            and (date_to is None or d <= date_to)]
    d_from = date_from or (days[0] if days else _today())
    d_to = date_to or (days[-1] if days else d_from)

    # The report renders coverage per underlying; PAPER has no coverage pass, so
    # each underlying's "days" are the days it has cycle_stats or trades.
    summary: dict = {}
    for und in sorted({r.underlying for r in cs} | {t.underlying for t in trades}):
        und_days = {r.trading_day for r in cs if r.underlying == und} \
            | {t.trading_day for t in trades if t.underlying == und}
        summary[und] = {"source": "PAPER",
                        "days_with_cycles": len({r.trading_day for r in cs
                                                 if r.underlying == und}),
                        "session_days": len(und_days), "tradeable": len(und_days),
                        "skipped": {}, "mean_option_ticks_per_tradeable_day": None}

    record, params_hash = _params_record(params, capital)
    ltp_only = sum(1 for t in trades if t.entry_fill_source not in (None, "ASK"))
    return report.ReportInputs(
        run_id=run_id, date_from=d_from, date_to=d_to,
        underlyings=tuple(summary) or ("NIFTY", "BANKNIFTY"), seed=seed,
        code_version=report._code_version(), params_record=record, params_hash=params_hash,
        params=params, capital=capital, trades=trades,
        coverage={"summary": summary, "days": []}, tradeable_days=days, label=LABEL,
        stats_={"ltp_only_fallbacks": ltp_only}, regime_hist=hist,
        no_trade=[{"stage": s_, "code": c_, "count": int(n)} for s_, c_, n in ntd],
        cycle_totals=totals,
        oi_iv_by_day=[{"underlying": r.underlying, "day": r.trading_day.isoformat(),
                       "missing": r.oi_iv_context_missing} for r in cs],
        signals_by_strategy={n: int(c) for n, c in sigs},
        prior_runs=[], oos_split=oos_split, iterations=iterations, synthetic=False,
    )


async def build_paper_validation(db, **kw) -> dict:
    """The six-criterion report over the PAPER rows. Read-only; always rolls back."""
    try:
        await make_read_only(db)
        inp = await gather_paper_inputs(db, **kw)
    finally:
        await db.rollback()
    rep = await asyncio.to_thread(report.build_validation, inp)
    rep["source"] = "PAPER"
    return rep


# ── CLI ──────────────────────────────────────────────────────────────────────

def _coerce(default, value):
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return value


async def load_config(db):
    """(TradingParams, capital) from the trading_config row via a raw SELECT *.
    Tolerates a pre-0013 column set: only columns that are TradingParams fields
    overlay the defaults; no row means TradingParams() defaults."""
    from app.core.trading_params import TradingParams

    defaults = TradingParams()
    row = (await db.execute(text("SELECT * FROM trading_config ORDER BY id LIMIT 1"))
           ).mappings().first()
    if row is None:
        return defaults, float(defaults.capital)
    fields = TradingParams.field_names()
    overrides = {k: _coerce(getattr(defaults, k), v) for k, v in row.items()
                 if k in fields and v is not None}
    p = dataclasses.replace(defaults, **overrides)
    return p, float(row.get("capital") or p.capital)


def parse_args(argv=None) -> argparse.Namespace:
    from app.core.config import settings
    ap = argparse.ArgumentParser(prog="python -m app.services.replay.paper_validation",
                                 description="Read-only D10 go/no-go over the PAPER run.")
    ap.add_argument("--database-url", required=True,
                    help="SQLAlchemy async URL of the database to READ (no default)")
    ap.add_argument("--run-id", default=PAPER_RUN_ID)
    ap.add_argument("--from", dest="date_from", type=date.fromisoformat, default=None)
    ap.add_argument("--to", dest="date_to", type=date.fromisoformat, default=None)
    ap.add_argument("--iterations", type=int, default=10_000)
    ap.add_argument("--seed", type=int, default=settings.REPLAY_DEFAULT_SEED)
    ap.add_argument("--oos-split", type=float, default=0.70)
    ap.add_argument("--out", type=Path, default=None, help="artefact directory")
    a = ap.parse_args(argv)
    if a.run_id != PAPER_RUN_ID:
        ap.error(f"--run-id must be {PAPER_RUN_ID}; a replay run's report is on replay_runs")
    if not 0.0 < a.oos_split < 1.0:
        ap.error("--oos-split must be in (0, 1)")
    if a.date_from and a.date_to and a.date_from > a.date_to:
        ap.error("--from must be <= --to")
    return a


async def main_async(a: argparse.Namespace) -> dict:
    from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import NullPool

    engine = create_async_engine(a.database_url, poolclass=NullPool)
    try:
        Session = sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with Session() as db:
            try:
                await make_read_only(db)
                p, capital = await load_config(db)
            finally:
                await db.rollback()
        async with Session() as db:
            return await build_paper_validation(
                db, run_id=a.run_id, params=p, capital=capital, date_from=a.date_from,
                date_to=a.date_to, iterations=a.iterations, seed=a.seed, oos_split=a.oos_split)
    finally:
        await engine.dispose()


def main(argv=None) -> int:
    a = parse_args(argv)
    try:
        rep = asyncio.run(main_async(a))
    except Exception as exc:
        print(f"paper_validation failed: {exc!r}", file=sys.stderr)
        return 2
    print(rep["recommendation_line"])
    if a.out is not None:
        for path in report.write_artefacts(rep, Path(a.out)):
            print(f"artefact: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
