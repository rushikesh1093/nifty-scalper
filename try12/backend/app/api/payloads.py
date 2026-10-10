"""
Shared response helpers (design §4.4), moved name-for-name out of
app/api/routes/analytics.py so the analytics and backtest routers format
pages and trades through the same functions. Behaviour is unchanged.
"""
from __future__ import annotations

import dataclasses
from datetime import date, datetime
from typing import Any

from app.services.metrics import trade_metrics as tm

EMPTY_REASON = "no_data_for_filter"


def jsonable(v: Any) -> Any:
    if dataclasses.is_dataclass(v) and not isinstance(v, type):
        return {f.name: jsonable(getattr(v, f.name)) for f in dataclasses.fields(v)}
    if isinstance(v, (datetime, date)):
        return v.isoformat()
    if isinstance(v, dict):
        return {str(k): jsonable(x) for k, x in v.items()}
    if isinstance(v, (list, tuple, set, frozenset)):
        items = [jsonable(x) for x in v]
        return sorted(items, key=str) if isinstance(v, (set, frozenset)) else items
    return getattr(v, "value", v)


def page(items: list, total: int) -> dict:
    out = {"items": items, "total": total}
    if total == 0:
        out["empty_reason"] = EMPTY_REASON
    return out


def trade_payload(t: tm.TradeRow, flat_eps: float) -> dict:
    d = jsonable(t)
    d["outcome"] = tm.outcome_of(t, flat_eps)
    d["r_valid"] = t.r_valid
    return d
