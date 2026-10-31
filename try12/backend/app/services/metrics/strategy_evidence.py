"""
Forward evidence from the paper book: does each strategy pass the research
promotion gates on trades it actually took? Pure apart from the caller's
trade-log load (strategy_report.load_closed).

Backtests on the same three years cannot prove a rule any further
(docs/WALK_FORWARD_REPORT.md); paper trades taken after a rule was fixed are
data that rule never saw. So only trades entered on or after the strategy's
RULE_SINCE date count: a trade made under an older entry or exit is a
different rule.

Gates (app.research.walkforward.PromotionCriteria, same as the research):
>= 100 trades on >= 60 sessions, net expectancy in R > 0 with the session
bootstrap 95 % CI above 0, profit factor >= 1.2, still positive without the
best session, max drawdown >= -25 R, >= half of the months positive.
Statuses: INSUFFICIENT_EVIDENCE, DISABLED_FOR_RESEARCH (negative on enough
trades, or a CI wholly below 0), PASSES_GATES. Nothing here switches a flag:
the status is advice for the operator.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date
from typing import Iterable, Optional

from app.research import metrics
from app.research.walkforward import PromotionCriteria, classify_status

# The first trading day each strategy's current rules were live. Bump a date
# whenever that strategy's entry or exit logic changes.
RULE_SINCE: dict[str, date] = {
    "FIRST_CANDLE_BREAKOUT": date(2026, 11, 2),      # ATR-chandelier exit, puts only
    "EMA_RSI_PULLBACK_15M": date(2026, 11, 2),       # Supertrend trail exit + low-vol entry skip
    "ORB_BREAKOUT": date(2026, 11, 2),               # puts only
    "PIVOT_BOUNCE": date(2026, 11, 2),               # puts only
}


def evaluate(log: Iterable[dict], criteria: PromotionCriteria = PromotionCriteria(),
             rule_since: Optional[dict[str, date]] = None) -> dict:
    """{strategy: {status, reasons, counted, excluded, stats}} from trade-log rows
    (strategy_report.trade_log_row). Rows without an R (no recorded initial
    risk) are excluded and counted."""
    since = RULE_SINCE if rule_since is None else rule_since
    groups: dict[str, list[dict]] = defaultdict(list)
    skipped: dict[str, dict] = defaultdict(lambda: {"before_rule_change": 0, "no_r": 0})
    for r in log:
        name = r["strategy"]
        day = r.get("trading_day")
        start = since.get(name)
        if start is not None and (day is None or date.fromisoformat(day) < start):
            skipped[name]["before_rule_change"] += 1
            continue
        if r.get("r_net") is None or day is None:
            skipped[name]["no_r"] += 1
            continue
        groups[name].append(r)
    out = {}
    for name in sorted(set(groups) | set(skipped)):
        rows = sorted(groups.get(name, []), key=lambda r: (r["trading_day"], r.get("entry_time") or ""))
        stats = metrics.summarize(rows, r_key="r_net", day_key="trading_day") if rows else {"n": 0}
        months: dict[str, float] = defaultdict(float)
        for r in rows:
            months[r["trading_day"][:7]] += float(r["r_net"])
        status, reasons = classify_status(stats, None, len(months),
                                          sum(1 for v in months.values() if v > 0), criteria)
        if status == "ELIGIBLE_FOR_PAPER":
            status = "PASSES_GATES"
        out[name] = {"status": status, "reasons": reasons,
                     "rule_since": str(since[name]) if name in since else None,
                     "counted": len(rows), "excluded": dict(skipped.get(name, {})),
                     "months_positive": f"{sum(1 for v in months.values() if v > 0)}/{len(months)}",
                     "stats": stats}
    return out
