"""
reconcile_performance (design §7, §9.5, R15.6).

An idempotent rebuild of strategy_perf, regime_performance and
daily_pnl_summary for one run from its closed paper_positions: DELETE the
run's rows, INSERT the recomputed ones, one transaction per table. The report
lists every key whose values changed, so running it is also the consistency
check between the incrementally maintained aggregates and the positions.

Two classifications, never mixed (§7):
  * net P&L (FLAT_EPSILON = params.flat_epsilon, ₹1): win/loss/flat counts,
    win_rate, expectancy_inr, gross_profit / gross_loss;
  * R sign (R_FLAT_EPSILON = params.r_flat_epsilon, 0.01R) over R-valid rows
    only (r_source = 'ENTRY', initial_risk > 0, r_multiple set): sum_r_win /
    sum_r_loss, winning_trades_r / losing_trades_r, avg_win_r / avg_loss_r.

Comparison is by declared tolerance, never equality: 1e-6 for R fields,
₹0.01 for money fields, exact for integer counters.

CLI: python -m app.services.trading.reconcile --run-id PAPER
(an operator step against the application database; design §11 step 4).
"""
from __future__ import annotations

import argparse
import asyncio
import logging
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Iterable, Optional

from sqlalchemy import Float, Integer, Numeric, delete, select

from app.db.models.paper_trade import DailyPnLSummary, PaperPosition, StrategyPerf
from app.db.models.regime import RegimePerformanceModel
from app.runtime.context import params, sessions
from app.services.trading.session_times import trading_day as ist_day

logger = logging.getLogger(__name__)

R_TOLERANCE = 1e-6
INR_TOLERANCE = 0.01

R_FIELDS = frozenset({
    "sum_r", "sum_r_win", "sum_r_loss", "avg_win_r", "avg_loss_r", "expectancy_r",
    "avg_r_multiple", "avg_rr",
})


@dataclass(frozen=True)
class _Outcome:
    pos_id: int
    strategy_name: str
    underlying: str
    regime: Optional[str]
    mode: Any
    day: Optional[date]
    exit_time: Optional[datetime]
    net: float
    gross: float
    brokerage: float
    charges: float
    lots: int
    is_win: bool
    is_loss: bool
    r: Optional[float]           # None when not R-valid
    r_bucket: Optional[str]      # R_WIN / R_LOSS / R_FLAT, None when not R-valid


@dataclass
class ReconcileReport:
    run_id: str
    before: dict[str, int] = field(default_factory=dict)
    after: dict[str, int] = field(default_factory=dict)
    changed_keys: list[tuple[str, tuple]] = field(default_factory=list)


def _outcome(pos: PaperPosition, flat_eps: float, r_eps: float) -> _Outcome:
    net = float(pos.net_pnl or 0.0)
    risk = pos.initial_risk
    r_valid = (pos.r_source == "ENTRY" and risk is not None and float(risk) > 0
               and pos.r_multiple is not None)
    r = float(pos.r_multiple) if r_valid else None
    bucket = None
    if r is not None:
        bucket = "R_WIN" if r > r_eps else "R_LOSS" if r < -r_eps else "R_FLAT"
    ts = pos.exit_time or pos.entry_time
    return _Outcome(
        pos_id=pos.id, strategy_name=pos.strategy_name or "UNKNOWN",
        underlying=pos.underlying, regime=pos.entry_regime, mode=pos.mode,
        day=ist_day(ts) if ts is not None else None, exit_time=ts,
        net=net, gross=float(pos.gross_pnl or 0.0), brokerage=float(pos.brokerage or 0.0),
        charges=float(pos.statutory_charges or 0.0),
        lots=int(pos.original_lots or pos.lots or 0),
        is_win=net > flat_eps, is_loss=net < -flat_eps, r=r, r_bucket=bucket,
    )


def _aggregate(rows: list[_Outcome]) -> dict:
    n = len(rows)
    wins = sum(o.is_win for o in rows)
    losses = sum(o.is_loss for o in rows)
    r_rows = [o for o in rows if o.r is not None]
    r_win = [o.r for o in r_rows if o.r_bucket == "R_WIN"]
    r_loss = [o.r for o in r_rows if o.r_bucket == "R_LOSS"]
    sum_r = sum(o.r for o in r_rows)
    total_net = sum(o.net for o in rows)
    return {
        "total_trades": n,
        "winning_trades": wins,
        "losing_trades": losses,
        "flat_trades": n - wins - losses,
        "win_rate": (wins / n * 100.0) if n else None,
        "net": total_net,
        "gross_profit": sum(o.net for o in rows if o.net > 0),
        "gross_loss": -sum(o.net for o in rows if o.net < 0),
        "sum_r": sum_r,
        "sum_r_win": sum(r_win),
        "sum_r_loss": sum(r_loss),
        "r_sample_size": len(r_rows),
        "winning_trades_r": len(r_win),
        "losing_trades_r": len(r_loss),
        "avg_win_r": (sum(r_win) / len(r_win)) if r_win else None,
        "avg_loss_r": (abs(sum(r_loss)) / len(r_loss)) if r_loss else None,
        "expectancy_r": (sum_r / len(r_rows)) if r_rows else None,
        "expectancy_inr": (total_net / n) if n else None,
    }


def _equity_drawdown(rows: list[_Outcome]) -> float:
    cum = peak = dd = 0.0
    for o in sorted(rows, key=lambda o: (o.exit_time or datetime.min, o.pos_id)):
        cum += o.net
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def _group(outcomes: Iterable[_Outcome], key) -> dict[tuple, list[_Outcome]]:
    out: dict[tuple, list[_Outcome]] = {}
    for o in outcomes:
        k = key(o)
        if k is not None:
            out.setdefault(k, []).append(o)
    return out


# ── per-table builders: key → column values ──────────────────────────────────

def _strategy_rows(run_id: str, outcomes: list[_Outcome]) -> dict[tuple, dict]:
    rows = {}
    for (name,), group in _group(outcomes, lambda o: (o.strategy_name,)).items():
        a = _aggregate(group)
        rows[(name, run_id)] = dict(
            strategy_name=name, run_id=run_id,
            total_trades=a["total_trades"], winning_trades=a["winning_trades"],
            losing_trades=a["losing_trades"], flat_trades=a["flat_trades"],
            win_rate=a["win_rate"], avg_rr=a["expectancy_r"],
            sum_r=a["sum_r"], sum_r_win=a["sum_r_win"], sum_r_loss=a["sum_r_loss"],
            r_sample_size=a["r_sample_size"], winning_trades_r=a["winning_trades_r"],
            losing_trades_r=a["losing_trades_r"], avg_win_r=a["avg_win_r"],
            avg_loss_r=a["avg_loss_r"], expectancy_r=a["expectancy_r"],
            expectancy_inr=a["expectancy_inr"], gross_profit=a["gross_profit"],
            gross_loss=a["gross_loss"],
        )
    return rows


def _regime_rows(run_id: str, outcomes: list[_Outcome]) -> dict[tuple, dict]:
    rows = {}
    grouped = _group(outcomes, lambda o: (o.regime, o.underlying, o.strategy_name)
                     if o.regime else None)
    for (regime, und, name), group in grouped.items():
        a = _aggregate(group)
        times = [o.exit_time for o in group if o.exit_time is not None]
        rows[(regime, und, name, run_id)] = dict(
            regime=regime, underlying=und, strategy_name=name, run_id=run_id,
            total_trades=a["total_trades"], winning_trades=a["winning_trades"],
            losing_trades=a["losing_trades"], flat_trades=a["flat_trades"],
            total_pnl=a["net"], avg_pnl=a["expectancy_inr"], win_rate=a["win_rate"],
            avg_rr=a["expectancy_r"], avg_r_multiple=a["expectancy_r"],
            sum_r=a["sum_r"], sum_r_win=a["sum_r_win"], sum_r_loss=a["sum_r_loss"],
            r_sample_size=a["r_sample_size"], expectancy_r=a["expectancy_r"],
            expectancy_inr=a["expectancy_inr"], gross_profit=a["gross_profit"],
            gross_loss=a["gross_loss"],
            first_trade_at=min(times) if times else None,
            last_trade_at=max(times) if times else None,
        )
    return rows


def _daily_rows(run_id: str, outcomes: list[_Outcome], since: Optional[date]) -> dict[tuple, dict]:
    rows = {}
    grouped = _group(outcomes, lambda o: (o.day, o.strategy_name, o.mode)
                     if o.day is not None and (since is None or o.day >= since) else None)
    for (day, name, mode), group in grouped.items():
        a = _aggregate(group)
        losses = [-o.net for o in group if o.net < 0]
        rows[(day, name, getattr(mode, "value", mode), run_id)] = dict(
            date=day, strategy_name=name, mode=mode, run_id=run_id,
            total_trades=a["total_trades"], winning_trades=a["winning_trades"],
            losing_trades=a["losing_trades"], flat_trades=a["flat_trades"],
            win_rate=a["win_rate"],
            gross_pnl=sum(o.gross for o in group), brokerage=sum(o.brokerage for o in group),
            statutory_charges=sum(o.charges for o in group), net_pnl=a["net"],
            worst_trade_loss=max(losses) if losses else None,
            max_equity_drawdown=_equity_drawdown(group),
            avg_rr=a["expectancy_r"], total_lots=sum(o.lots for o in group),
            sum_r=a["sum_r"], sum_r_win=a["sum_r_win"], sum_r_loss=a["sum_r_loss"],
            r_sample_size=a["r_sample_size"], expectancy_r=a["expectancy_r"],
            expectancy_inr=a["expectancy_inr"], gross_profit=a["gross_profit"],
            gross_loss=a["gross_loss"],
        )
    return rows


# ── persistence ──────────────────────────────────────────────────────────────

def _fit(model, values: dict) -> dict:
    """Round values bound for pre-existing Numeric columns to their scale, so a
    second run compares equal to what the first run stored."""
    out = dict(values)
    for k, v in values.items():
        col = model.__table__.c[k]
        if v is not None and isinstance(col.type, Numeric) and not isinstance(col.type, Float) \
                and col.type.scale is not None:
            out[k] = round(float(v), col.type.scale)
    return out


def _differs(model, name: str, old: Any, new: Any) -> bool:
    old, new = getattr(old, "value", old), getattr(new, "value", new)
    if old is None or new is None:
        return (old is None) != (new is None)
    col_type = model.__table__.c[name].type
    if isinstance(col_type, Integer):
        return int(old) != int(new)
    if isinstance(col_type, Numeric):           # Float is a Numeric subclass
        tol = R_TOLERANCE if name in R_FIELDS else INR_TOLERANCE
        return abs(float(old) - float(new)) > tol
    if isinstance(old, datetime) and isinstance(new, datetime):
        return old.replace(tzinfo=None) != new.replace(tzinfo=None)
    return old != new


async def _rebuild(model, table: str, run_id: str, new_rows: dict[tuple, dict], key_of,
                   report: ReconcileReport, extra_where=(), carry: tuple[str, ...] = ()) -> None:
    async with sessions()() as db:
        q = select(model).where(model.run_id == run_id, *extra_where)
        old = {key_of(r): r for r in (await db.execute(q)).scalars().all()}
        report.before[table] = len(old)

        fitted = {k: _fit(model, v) for k, v in new_rows.items()}
        for k in sorted(set(old) | set(fitted), key=repr):
            o, n = old.get(k), fitted.get(k)
            if o is None or n is None or any(_differs(model, c, getattr(o, c), v)
                                             for c, v in n.items()):
                report.changed_keys.append((table, k))

        carried = {k: {c: getattr(r, c) for c in carry} for k, r in old.items()}
        await db.execute(delete(model).where(model.run_id == run_id, *extra_where))
        for k, values in fitted.items():
            db.add(model(**values, **carried.get(k, {})))
        await db.commit()
        report.after[table] = len(fitted)


async def reconcile_performance(run_id: str, since: Optional[date] = None) -> ReconcileReport:
    """Rebuild the run's aggregates from its closed positions. Idempotent.
    *since* limits the daily_pnl_summary rebuild to days >= since; the all-time
    tables are always rebuilt in full."""
    p = params()
    async with sessions()() as db:
        positions = (await db.execute(
            select(PaperPosition)
            .where(PaperPosition.run_id == run_id, PaperPosition.is_open == False)  # noqa: E712
            .order_by(PaperPosition.exit_time, PaperPosition.id)
        )).scalars().all()
    outcomes = [_outcome(pos, p.flat_epsilon, p.r_flat_epsilon) for pos in positions]

    report = ReconcileReport(run_id=run_id)
    await _rebuild(StrategyPerf, "strategy_perf", run_id, _strategy_rows(run_id, outcomes),
                   lambda r: (r.strategy_name, r.run_id), report, carry=("kelly_fraction",))
    await _rebuild(RegimePerformanceModel, "regime_performance", run_id,
                   _regime_rows(run_id, outcomes),
                   lambda r: (r.regime, r.underlying, r.strategy_name, r.run_id), report)
    daily_where = (DailyPnLSummary.date >= since,) if since is not None else ()
    await _rebuild(DailyPnLSummary, "daily_pnl_summary", run_id,
                   _daily_rows(run_id, outcomes, since),
                   lambda r: (r.date, r.strategy_name, getattr(r.mode, "value", r.mode), r.run_id),
                   report, extra_where=daily_where)
    logger.info("reconcile_performance run_id=%s before=%s after=%s changed=%d",
                run_id, report.before, report.after, len(report.changed_keys))
    return report


def _main() -> None:          # pragma: no cover - operator CLI
    from app.runtime.context import using
    from app.runtime.live import script_context

    ap = argparse.ArgumentParser(description="Rebuild performance aggregates for one run.")
    ap.add_argument("--run-id", default="PAPER")
    ap.add_argument("--since", type=date.fromisoformat, default=None)
    args = ap.parse_args()

    async def _go():
        with using(script_context()):
            rep = await reconcile_performance(args.run_id, args.since)
        print(f"before={rep.before} after={rep.after}")
        for table, key in rep.changed_keys:
            print(f"changed {table} {key}")

    asyncio.run(_go())


if __name__ == "__main__":    # pragma: no cover
    _main()
