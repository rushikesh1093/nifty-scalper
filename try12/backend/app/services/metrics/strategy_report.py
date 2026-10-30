"""
Trade log and per-strategy report (Phase 0 of the strategy-fix plan).

``trade_log_row`` turns a closed paper_positions row into the trade log's
fields; ``build_report`` aggregates those rows per strategy: trades, win rate,
average win / loss, expectancy before and after charges, a count of each exit
category, expiry day vs other days, and P&L by entry hour. Pure apart from
``load_closed``.

Reading it: PREMIUM_STOP as the commonest exit means the premium stop, not the
chart stop, is ending trades; expectancy positive before charges and negative
after means the target is too small for the costs.
"""
from __future__ import annotations

from collections import Counter, defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

IST = timezone(timedelta(hours=5, minutes=30))

# paper_positions.exit_reason -> trade-log category ("_STALE" suffixes folded in)
EXIT_CATEGORY = {
    "SL": "PREMIUM_STOP",
    "SPOT_SL": "SPOT_STOP",
    "TARGET": "TARGET",
    "TIME_EXIT": "TIME_EXIT",
    "SESSION_EXIT": "TIME_EXIT",
    "ST_FLIP": "FLIP",
    "EMA9_TRAIL": "TRAIL",
    "TRAIL": "TRAIL",
    "VWAP_EXIT": "VWAP_EXIT",
}


def exit_category(reason: Optional[str], exit_plan: Optional[dict]) -> str:
    """The trade log's exit_reason. A spot stop after the plan moved its stop to
    entry (break-even trigger or T1) is BREAKEVEN."""
    if not reason:
        return "UNKNOWN"
    base = reason[:-len("_STALE")] if reason.endswith("_STALE") else reason
    cat = EXIT_CATEGORY.get(base, base)
    if cat == "SPOT_STOP" and exit_plan and (exit_plan.get("breakeven_done") or exit_plan.get("t1_done")):
        return "BREAKEVEN"
    return cat


def _ist(t: Optional[datetime]) -> Optional[datetime]:
    if t is None:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t.astimezone(IST)


def trade_log_row(pos, seq: int = 1) -> dict:
    """The trade log's fields for one closed position (*seq*: its number among the
    strategy's trades that day, for trade_id)."""
    plan = pos.exit_plan or {}
    entry, exit_ = _ist(pos.entry_time), _ist(pos.exit_time)
    qty = pos.original_quantity or pos.quantity or 0
    charges = round((pos.brokerage or 0.0) + (pos.statutory_charges or 0.0), 2)
    slippage = round(((pos.entry_slippage or 0.0) + (pos.exit_slippage or 0.0)) * qty, 2)
    kind = getattr(pos.option_type, "value", pos.option_type)
    return {
        "trade_id": f"{entry:%Y-%m-%d}-{pos.strategy_name}-{seq}" if entry else str(pos.id),
        "position_id": pos.id,
        "strategy": pos.strategy_name,
        "direction": kind,
        "strike": pos.strike,
        "expiry": pos.expiry.isoformat() if pos.expiry else None,
        "is_expiry_day": bool(entry and pos.expiry and pos.expiry == entry.date()),
        "entry_time": entry.strftime("%H:%M") if entry else None,
        "exit_time": exit_.strftime("%H:%M") if exit_ else None,
        "trading_day": entry.date().isoformat() if entry else None,
        "spot_entry": pos.spot_entry if pos.spot_entry is not None else plan.get("spot_entry"),
        "spot_exit": pos.spot_exit,
        "opt_entry": pos.actual_entry,
        "opt_exit": pos.actual_exit,
        "delta_at_entry": pos.delta_at_entry,
        "iv_entry": pos.iv_entry,
        "iv_exit": pos.iv_exit,
        "spot_stop": plan.get("spot_stop"),
        "spot_target": plan.get("spot_target") or plan.get("spot_t1"),
        "premium_stop": pos.initial_stop_loss,
        "exit_reason": exit_category(pos.exit_reason, plan),
        "raw_exit_reason": pos.exit_reason,
        "gross_pnl": round(pos.gross_pnl or 0.0, 2),
        "charges": charges,
        "slippage": slippage,
        "net_pnl": round(pos.net_pnl or 0.0, 2),
        "quantity": qty,
    }


def _block(rows: list[dict]) -> dict:
    n = len(rows)
    if not n:
        return {"trades": 0}
    wins = [r["net_pnl"] for r in rows if r["net_pnl"] > 0]
    losses = [r["net_pnl"] for r in rows if r["net_pnl"] <= 0]
    return {
        "trades": n,
        "wins": len(wins),
        "win_rate": round(len(wins) / n, 4),
        "avg_win": round(sum(wins) / len(wins), 2) if wins else None,
        "avg_loss": round(sum(losses) / len(losses), 2) if losses else None,
        "expectancy_gross": round(sum(r["gross_pnl"] for r in rows) / n, 2),
        "expectancy_net": round(sum(r["net_pnl"] for r in rows) / n, 2),
        "net_pnl": round(sum(r["net_pnl"] for r in rows), 2),
        "charges": round(sum(r["charges"] for r in rows), 2),
        "slippage": round(sum(r["slippage"] for r in rows), 2),
    }


def build_report(log: Iterable[dict]) -> dict:
    """Per strategy and for ALL: _block figures, exit-category counts, expiry day
    vs other days, and net P&L / trades by entry hour."""
    by_strategy: dict[str, list[dict]] = defaultdict(list)
    rows = list(log)
    for r in rows:
        by_strategy[r["strategy"]].append(r)
    out = {}
    for name, group in sorted(by_strategy.items()) + [("ALL", rows)]:
        if not group:
            continue
        hours: dict[str, list[dict]] = defaultdict(list)
        for r in group:
            hours[(r["entry_time"] or "??")[:2]].append(r)
        b = _block(group)
        out[name] = {
            **b,
            "exit_reasons": dict(Counter(r["exit_reason"] for r in group).most_common()),
            "expiry_day": _block([r for r in group if r["is_expiry_day"]]),
            "other_days": _block([r for r in group if not r["is_expiry_day"]]),
            "by_entry_hour": {h: {"trades": len(v), "net_pnl": round(sum(r["net_pnl"] for r in v), 2)}
                              for h, v in sorted(hours.items())},
            "flags": _flags(b, group),
        }
    return out


def _flags(b: dict, group: list[dict]) -> list[str]:
    """The report's reading rules, applied."""
    out = []
    reasons = Counter(r["exit_reason"] for r in group)
    if reasons and reasons.most_common(1)[0][0] == "PREMIUM_STOP":
        out.append("PREMIUM_STOP is the commonest exit: the premium stop is ending trades before the chart stop")
    if b.get("expectancy_gross", 0) > 0 >= b.get("expectancy_net", 0):
        out.append("positive before charges, negative after: the target is too small for the costs")
    exp, oth = _block([r for r in group if r["is_expiry_day"]]), _block([r for r in group if not r["is_expiry_day"]])
    if exp.get("trades") and oth.get("trades") and exp["expectancy_net"] < oth["expectancy_net"] - abs(oth["expectancy_net"]):
        out.append("expiry days are much worse than other days")
    return out


async def load_closed(db, run_id: str, date_from: Optional[date] = None,
                      date_to: Optional[date] = None) -> list[dict]:
    """Trade-log rows for the run's closed positions, oldest first."""
    from sqlalchemy import select
    from app.db.models.paper_trade import PaperPosition
    q = select(PaperPosition).where(PaperPosition.run_id == run_id, PaperPosition.is_open == False)  # noqa: E712
    if date_from is not None:
        q = q.where(PaperPosition.entry_time >= datetime.combine(date_from, datetime.min.time(), IST))
    if date_to is not None:
        q = q.where(PaperPosition.entry_time < datetime.combine(date_to + timedelta(days=1),
                                                                datetime.min.time(), IST))
    positions = (await db.execute(q.order_by(PaperPosition.entry_time, PaperPosition.id))).scalars().all()
    seq: Counter = Counter()
    out = []
    for p in positions:
        day = _ist(p.entry_time).date() if p.entry_time else None
        seq[(day, p.strategy_name)] += 1
        out.append(trade_log_row(p, seq[(day, p.strategy_name)]))
    return out
