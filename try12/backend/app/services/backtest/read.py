"""
Backtest read assembly (design §4.5, §4.6, §4.9).

Every figure is measured against the run's OWN frozen record
(``replay_runs.params`` / ``sections.1.params``): capital from ``cfg.capital``,
the display knobs from the rebuilt TradingParams. The live runtime context is
never consulted. Metrics come from app.services.metrics (R15.6); nothing here
re-implements them.

Tier 1 is a replay store (app.db.replay_store); tier 2 is the report artefact
(app.services.backtest.artifacts) for run ids no store holds.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from fastapi import HTTPException
from sqlalchemy import desc, func, select

from app.api.payloads import EMPTY_REASON, jsonable, page, trade_payload
from app.core.config import settings
from app.core.trading_params import TradingParams, TradingParamsError
from app.db import replay_store
from app.db.models.replay import ReplayRun
from app.db.models.risk import NoTradeDecision
from app.db.replay_store import Dataset
from app.services.backtest import artifacts, series
from app.services.metrics import stats
from app.services.metrics import trade_metrics as tm

BACKTEST_MAX_STORE_ROWS = 200
NOT_INITIALISED = "replay_store_not_initialised"
NO_RUNS = "no_runs"
OUTCOMES = ("WIN", "LOSS", "FLAT")
ARTIFACT_DIMENSIONS = {"strategy": "by_strategy", "regime": "by_regime",
                       "strategy_regime": "by_strategy_regime", "underlying": "by_underlying"}


# ── shared derivations ───────────────────────────────────────────────────────

def run_params(record: dict) -> tuple[TradingParams, list[str]]:
    """The run's own frozen params, with a defaults fallback that is never silent."""
    raw = (record or {}).get("params") or {}
    try:
        return TradingParams.from_mapping(raw), []
    except (TradingParamsError, TypeError, ValueError):
        # a record written by an older code_version may carry a renamed or removed key
        return TradingParams(), ["params_defaults_used"]


def run_capital(record: dict) -> float:
    """record['cfg']['capital'], else record['params']['capital'], else 0.0 → the
    percentage fields come back None rather than wrong."""
    rec = record or {}
    for src in (rec.get("cfg"), rec.get("params")):
        if isinstance(src, dict) and src.get("capital"):
            try:
                return float(src["capital"])
            except (TypeError, ValueError):
                continue
    return 0.0


def sessions_of(coverage: Optional[dict]) -> int:
    """Distinct TRADEABLE calendar days — not a sum of per-underlying counts."""
    cov = coverage or {}
    days = cov.get("days") or []
    if days:
        return len({d.get("day") for d in days
                    if isinstance(d, dict) and d.get("status") == "TRADEABLE"})
    summary = cov.get("summary") or {}
    if isinstance(summary, dict) and summary:
        return max(int((u or {}).get("tradeable") or 0) for u in summary.values())
    return 0


def trades_per_day(trades: Optional[int], sessions: Optional[int]) -> Optional[float]:
    if trades is None or not sessions:
        return None
    return trades / sessions


def label_of(record: Optional[dict], validation: Optional[dict]) -> Optional[str]:
    """replay_runs has no label column; the engine freezes it inside the record."""
    return (record or {}).get("label") or (validation or {}).get("label") or None


def synthetic_state(validation: Optional[dict], dataset_kind: str,
                    label: Optional[str]) -> tuple[str, str]:
    """(synthetic_state, synthetic_source), first matching row wins (§4.9).
    There is no REAL state: unknown provenance is UNVERIFIED."""
    if isinstance(validation, dict) and validation.get("synthetic") is True:
        return "SYNTHETIC", "validation"
    if dataset_kind == "synthetic":
        return "SYNTHETIC", "dataset"
    if label and "synthetic" in label.lower():
        return "SYNTHETIC", "label"
    return "UNVERIFIED", "none"


_WIN_PATH = re.compile(r"[A-Za-z]:[\\/][^\s\"'<>|,;)]*")
_POSIX_PATH = re.compile(r"(?<![\w:/.])/(?:[\w.\-]+/)+[\w.\-]*")


def redact_paths(text: Optional[str]) -> Optional[str]:
    """Absolute filesystem paths (e.g. in a FAILED run's stored traceback) reduced
    to their basename, so no response carries a path (C7)."""
    if not text:
        return text

    def _base(m: re.Match) -> str:
        s = m.group(0).rstrip("\\/")
        return re.split(r"[\\/]", s)[-1] if s else ""
    return _POSIX_PATH.sub(_base, _WIN_PATH.sub(_base, text))


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def _iso(v) -> Optional[str]:
    if isinstance(v, datetime):
        return _aware(v).isoformat()
    return v.isoformat() if v is not None else None


def _sort_key(item: dict) -> datetime:
    try:
        return _aware(datetime.fromisoformat(item["created_at"]))
    except (TypeError, ValueError, KeyError):
        return datetime.min.replace(tzinfo=timezone.utc)


def _section(rep: Optional[dict], n: str) -> dict:
    s = ((rep or {}).get("sections") or {}).get(n)
    return s if isinstance(s, dict) else {}


def _not_found(run_id: str) -> HTTPException:
    return HTTPException(status_code=404, detail=f"run '{run_id}' not found")


# ── tier-1 queries ───────────────────────────────────────────────────────────

async def _store_rows(ds: Dataset) -> tuple[list, bool]:
    async with replay_store.store_session(ds) as db:
        rows = (await db.execute(select(ReplayRun).order_by(desc(ReplayRun.created_at))
                                 .limit(BACKTEST_MAX_STORE_ROWS + 1))).scalars().all()
    return list(rows[:BACKTEST_MAX_STORE_ROWS]), len(rows) > BACKTEST_MAX_STORE_ROWS


async def _fetch_row(ds: Dataset, run_id: str):
    async with replay_store.store_session(ds) as db:
        return (await db.execute(select(ReplayRun).where(ReplayRun.run_id == run_id))
                ).scalar_one_or_none()


async def _count_rows(ds: Dataset) -> int:
    async with replay_store.store_session(ds) as db:
        return int((await db.execute(select(func.count(ReplayRun.run_id)))).scalar() or 0)


async def _load_trades(ds: Dataset, f: tm.TradeFilter, flat_eps: float) -> list:
    async with replay_store.store_session(ds) as db:
        return await tm.load_trades(db, f, flat_epsilon=flat_eps)


# ── list rows ────────────────────────────────────────────────────────────────

def _store_item(r: ReplayRun, ds: Dataset, art: dict) -> dict:
    m = r.metrics if isinstance(r.metrics, dict) else None
    v: Optional[dict] = r.validation if isinstance(r.validation, dict) and r.validation else None
    vsrc = "db"
    if v is None:
        a = art.get(r.run_id)
        if a is not None:
            v = {"recommendation": a.recommendation, "failing_criteria": a.failing_criteria,
                 "caps": a.caps, "synthetic": a.synthetic, "label": a.label}
            vsrc = "artifact"
        else:
            vsrc = "none"
    label = label_of(r.params, v)
    state, source = synthetic_state(v, ds.kind, label)
    sessions = sessions_of(r.coverage)
    mm = m or {}
    trades = mm.get("trades") if m is not None else None
    return {
        "run_id": r.run_id, "dataset": ds.dataset_id, "status": r.status,
        "created_at": _iso(r.created_at), "created_at_source": "row", "label": label,
        "date_from": _iso(r.date_from), "date_to": _iso(r.date_to),
        "underlyings": r.underlyings, "seed": r.seed,
        "trades": trades, "sessions": sessions, "trades_per_day": trades_per_day(trades, sessions),
        "win_rate": mm.get("win_rate"), "expectancy_r": mm.get("expectancy_r"),
        "expectancy_inr": mm.get("expectancy_inr"),
        "max_drawdown_pct": mm.get("max_equity_drawdown_pct"),
        "recommendation": (v or {}).get("recommendation"),
        "failing_criteria": (v or {}).get("failing_criteria"),
        "caps": (v or {}).get("caps"),
        "synthetic_state": state, "synthetic_source": source,
        "params_hash": r.params_hash, "determinism_hash": mm.get("determinism_hash"),
        "code_version": r.code_version,
        "per_trade_available": True, "metrics_available": True, "breakdowns_available": True,
        "validation_source": vsrc,
    }


def _artifact_status(sessions: int) -> str:
    # a report is only built on the COMPLETE and NO_DATA paths; NO_DATA ⇔ no TRADEABLE day
    return "COMPLETE" if sessions else "NO_DATA"


def _artifact_item(a: artifacts.ArtifactRow) -> dict:
    sessions = sessions_of(a.coverage)
    state, source = synthetic_state({"synthetic": a.synthetic}, "artifacts", a.label)
    return {
        "run_id": a.run_id, "dataset": replay_store.ARTIFACTS_ID,
        "status": _artifact_status(sessions),
        "created_at": a.created_at, "created_at_source": "file_mtime", "label": a.label,
        "date_from": a.date_from, "date_to": a.date_to, "underlyings": a.underlyings,
        "seed": a.seed, "trades": a.trades, "sessions": sessions,
        "trades_per_day": trades_per_day(a.trades, sessions),
        "win_rate": a.win_rate, "expectancy_r": a.expectancy_r,
        "expectancy_inr": a.expectancy_inr, "max_drawdown_pct": a.max_drawdown_pct,
        "recommendation": a.recommendation, "failing_criteria": a.failing_criteria,
        "caps": a.caps, "synthetic_state": state, "synthetic_source": source,
        "params_hash": a.params_hash, "determinism_hash": None, "code_version": a.code_version,
        "per_trade_available": False, "metrics_available": a.metrics_available,
        "breakdowns_available": a.breakdowns_available, "validation_source": "artifact",
    }


# ── GET /backtest/datasets ───────────────────────────────────────────────────

async def datasets_view() -> dict:
    reg = replay_store.registry()
    error = reg.error
    items = []
    for ds in reg.datasets:
        v = ds.view()
        v["error"] = None
        if ds.kind == "artifacts":
            v["runs"] = len(artifacts.index())
        elif not ds.exists:
            v["runs"] = 0
        else:
            try:
                v["runs"] = await _count_rows(ds)
            except HTTPException as exc:
                if exc.status_code != 503:
                    raise
                v["runs"], v["error"] = None, "unreachable"
                if ds.dataset_id in (replay_store.REPLAY_DB_ID, reg.alias_of):
                    error = "unreachable"
        items.append(v)
    return {"items": items, "total": len(items),
            "replay_store_configured": bool(settings.REPLAY_DATABASE_URL),
            "replay_store_error": error, "replay_db_alias_of": reg.alias_of}


# ── GET /backtest/runs ───────────────────────────────────────────────────────

async def list_runs(dataset: Optional[str], limit: int, offset: int) -> dict:
    reg = replay_store.registry()
    warnings: list[str] = []
    caps: dict = {}
    items: list[dict] = []
    echo = None
    if dataset:
        ds = replay_store.resolve(dataset, reg)
        echo = ds.dataset_id
        if ds.kind == "artifacts":
            rows, trunc = artifacts.scan()
            items = [_artifact_item(a) for a in rows]
            if trunc:
                caps["artifacts"] = settings.BACKTEST_MAX_ARTIFACT_FILES
        elif not ds.exists:
            return {"items": [], "total": 0, "dataset": echo, "warnings": [],
                    "truncated": False, "caps": {}, "empty_reason": NOT_INITIALISED}
        else:
            art = {a.run_id: a for a in artifacts.index()}
            rows, trunc = await _store_rows(ds)
            items = [_store_item(r, ds, art) for r in rows]
            if trunc:
                caps["store"] = BACKTEST_MAX_STORE_ROWS
    else:
        if artifacts.artifact_dir() is None:
            warnings.append("artifacts_unavailable")
        art_rows, art_trunc = artifacts.scan()
        art = {a.run_id: a for a in art_rows}
        seen: set[str] = set()
        for ds in reg.datasets:                       # registry order is the tie-break
            if ds.kind == "artifacts" or not ds.exists:
                continue
            try:
                rows, trunc = await _store_rows(ds)
            except HTTPException as exc:
                if exc.status_code != 503:
                    raise
                warnings.append(f"store_unreachable:{ds.dataset_id}")
                continue
            if trunc:
                caps["store"] = BACKTEST_MAX_STORE_ROWS
            for r in rows:
                if r.run_id not in seen:
                    seen.add(r.run_id)
                    items.append(_store_item(r, ds, art))
        for a in art_rows:                            # tier 2 only for ids no store has
            if a.run_id not in seen:
                seen.add(a.run_id)
                items.append(_artifact_item(a))
        if art_trunc:
            caps["artifacts"] = settings.BACKTEST_MAX_ARTIFACT_FILES
    items.sort(key=_sort_key, reverse=True)
    total = len(items)
    return {"items": items[offset:offset + limit], "total": total, "dataset": echo,
            "warnings": warnings, "truncated": bool(caps), "caps": caps,
            "empty_reason": NO_RUNS if total == 0 else None}


# ── locating one run ─────────────────────────────────────────────────────────

@dataclass
class Located:
    ds: Dataset
    row: Optional[ReplayRun] = None        # tier 1
    report: Optional[dict] = None          # tier 2

    @property
    def tier(self) -> int:
        return 1 if self.row is not None else 2


async def locate(run_id: str, dataset: Optional[str]) -> Located:
    artifacts.check_run_id(run_id)
    reg = replay_store.registry()
    if dataset:
        ds = replay_store.resolve(dataset, reg)
        if ds.kind == "artifacts":
            rep = artifacts.load(run_id)
            if rep is None:
                raise _not_found(run_id)
            return Located(ds, report=rep)
        if not ds.exists:
            raise _not_found(run_id)
        row = await _fetch_row(ds, run_id)
        if row is None:
            raise _not_found(run_id)
        return Located(ds, row=row)
    failed: Optional[HTTPException] = None
    for ds in reg.datasets:
        if ds.kind == "artifacts" or not ds.exists:
            continue
        try:
            row = await _fetch_row(ds, run_id)
        except HTTPException as exc:
            if exc.status_code != 503:
                raise
            failed = failed or exc
            continue
        if row is not None:
            return Located(ds, row=row)
    art = next((d for d in reg.datasets if d.kind == "artifacts"), None)
    if art is not None:
        rep = artifacts.load(run_id)
        if rep is not None:
            return Located(art, report=rep)
    if failed is not None:
        raise failed
    raise _not_found(run_id)


def _validation_for(loc: Located) -> tuple[Optional[dict], str, list[str]]:
    """(validation, validation_source, warnings): db → artifact → none."""
    if loc.tier == 2:
        return loc.report, "artifact", []
    v = loc.row.validation
    if isinstance(v, dict) and v:
        return v, "db", []
    rep = artifacts.load(loc.row.run_id)
    if rep is not None:
        return rep, "artifact", ["validation_from_artifact"]
    return None, "none", []


def _record(loc: Located) -> dict:
    if loc.tier == 1:
        return loc.row.params if isinstance(loc.row.params, dict) else {}
    rec = _section(loc.report, "1").get("params")
    return rec if isinstance(rec, dict) else {}


# ── GET /backtest/runs/{run_id} ──────────────────────────────────────────────

async def run_detail(run_id: str, dataset: Optional[str]) -> dict:
    loc = await locate(run_id, dataset)
    validation, vsrc, warnings = _validation_for(loc)
    record = _record(loc)
    _, pw = run_params(record)
    warnings = warnings + pw
    label = label_of(record, validation)
    state, source = synthetic_state(validation, loc.ds.kind, label)
    split = _section(validation, "7").get("split_fraction")
    if loc.tier == 1:
        r = loc.row
        metrics_row = r.metrics if isinstance(r.metrics, dict) else None
        out = {
            "run_id": r.run_id, "dataset": loc.ds.dataset_id, "status": r.status,
            "created_at": _iso(r.created_at), "created_at_source": "row", "label": label,
            "notes": redact_paths(r.notes),
            "date_from": _iso(r.date_from), "date_to": _iso(r.date_to),
            "underlyings": r.underlyings, "seed": r.seed, "oos_split": split,
            "code_version": r.code_version, "params_hash": r.params_hash,
            "determinism_hash": (metrics_row or {}).get("determinism_hash"),
            "params": r.params, "coverage": r.coverage, "metrics_row": metrics_row,
            "per_trade_available": True, "metrics_available": True,
            "breakdowns_available": True, "sessions": sessions_of(r.coverage),
        }
    else:
        rep, s1 = loc.report, _section(loc.report, "1")
        a = artifacts.row_for(run_id)
        coverage = _section(rep, "2") or None
        sessions = sessions_of(coverage)
        out = {
            "run_id": run_id, "dataset": loc.ds.dataset_id, "status": _artifact_status(sessions),
            "created_at": a.created_at if a else None, "created_at_source": "file_mtime",
            "label": label, "notes": _section(rep, "9").get("declared") or None,
            "date_from": s1.get("date_from"), "date_to": s1.get("date_to"),
            "underlyings": s1.get("underlyings"), "seed": s1.get("seed"), "oos_split": split,
            "code_version": s1.get("code_version"), "params_hash": s1.get("params_hash"),
            "determinism_hash": None, "params": record or None, "coverage": coverage,
            "metrics_row": None, "per_trade_available": False,
            "metrics_available": isinstance(_section(rep, "4").get("metrics"), dict),
            "breakdowns_available": "6" in (rep.get("sections") or {}), "sessions": sessions,
        }
    out.update({"validation": validation, "validation_source": vsrc,
                "synthetic_state": state, "synthetic_source": source,
                "capital": run_capital(record), "warnings": warnings})
    return out


# ── GET /backtest/runs/{run_id}/metrics ──────────────────────────────────────

def _ci(validation: Optional[dict]) -> tuple:
    s4 = _section(validation, "4")
    if not s4:
        return None, None, "none"
    return s4.get("expectancy_r_ci_day_block"), s4.get("expectancy_inr_ci_day_block"), "validation"


def _cost_figures(trades: list) -> dict:
    pairs = [(t.r_multiple, t.r_multiple_net) for t in trades
             if t.r_valid and t.r_multiple is not None and t.r_multiple_net is not None]
    return {
        "mean_cost_inr": stats.mean([t.brokerage + t.statutory_charges for t in trades])
        if trades else None,
        "cost_sample_inr": len(trades),
        "mean_cost_r": stats.mean([a - b for a, b in pairs]) if pairs else None,
        "cost_sample_r": len(pairs),
    }


async def run_metrics(run_id: str, dataset: Optional[str]) -> dict:
    loc = await locate(run_id, dataset)
    validation, _, _ = _validation_for(loc)
    record = _record(loc)
    capital = run_capital(record)
    r_ci, inr_ci, ci_source = _ci(validation)
    if loc.tier == 1:
        p, warnings = run_params(record)
        trades = await _load_trades(loc.ds, tm.TradeFilter(run_id=run_id), p.flat_epsilon)
        m = tm.compute(trades, capital, p)
        sessions = sessions_of(loc.row.coverage)
        out = {"run_id": run_id, "dataset": loc.ds.dataset_id, "source": "store",
               "capital": capital, "metrics": jsonable(m),
               "daily": jsonable(tm.compute_daily(trades, capital)),
               "sessions": sessions, "trades_per_day": trades_per_day(m.trades, sessions),
               "win_rate_wilson_95": list(stats.wilson_interval(m.wins, m.trades)),
               **_cost_figures(trades)}
        empty = not trades
    else:
        rep = loc.report
        metrics = _section(rep, "4").get("metrics")
        if not isinstance(metrics, dict):
            raise HTTPException(status_code=409, detail="this artefact has no metrics section")
        warnings = []
        sessions = sessions_of(_section(rep, "2"))
        n, wins = int(metrics.get("trades") or 0), int(metrics.get("wins") or 0)
        out = {"run_id": run_id, "dataset": loc.ds.dataset_id, "source": "artifact",
               "capital": capital, "metrics": metrics, "daily": _section(rep, "5") or None,
               "sessions": sessions, "trades_per_day": trades_per_day(n, sessions),
               "win_rate_wilson_95": list(stats.wilson_interval(wins, n)),
               "mean_cost_inr": None, "cost_sample_inr": 0,
               "mean_cost_r": None, "cost_sample_r": 0}
        empty = n == 0
    out.update({"expectancy_r_ci_day_block": r_ci, "expectancy_inr_ci_day_block": inr_ci,
                "ci_source": ci_source, "empty_reason": EMPTY_REASON if empty else None,
                "warnings": warnings})
    return out


# ── GET /backtest/runs/{run_id}/metrics/by/{dimension} ───────────────────────

async def run_breakdown(run_id: str, dimension: str, dataset: Optional[str]) -> dict:
    artifacts.check_run_id(run_id)
    if dimension not in tm.DIMENSIONS:
        raise HTTPException(status_code=400, detail=f"dimension must be one of {tm.DIMENSIONS}")
    loc = await locate(run_id, dataset)
    record = _record(loc)
    if loc.tier == 1:
        p, _ = run_params(record)
        trades = await _load_trades(loc.ds, tm.TradeFilter(run_id=run_id), p.flat_epsilon)
        groups = {k: jsonable(v) for k, v in
                  tm.group_by(trades, dimension, run_capital(record), p).items()}
        source = "store"
    else:
        if dimension == "day":
            raise HTTPException(status_code=400, detail="dimension 'day' needs per-trade rows, "
                                                        "which the report artefact does not contain")
        s6 = (loc.report.get("sections") or {}).get("6")
        if not isinstance(s6, dict):
            raise HTTPException(status_code=409, detail="this artefact has no breakdown section")
        raw = s6.get(ARTIFACT_DIMENSIONS[dimension])
        groups = dict(sorted(raw.items())) if isinstance(raw, dict) else {}
        source = "artifact"
    return {"run_id": run_id, "dataset": loc.ds.dataset_id, "dimension": dimension,
            "groups": groups, "total": len(groups), "source": source,
            "empty_reason": None if groups else EMPTY_REASON}


# ── GET /backtest/runs/{run_id}/no-trade-decisions/summary ───────────────────

async def no_trade_summary(run_id: str, dataset: Optional[str]) -> dict:
    loc = await locate(run_id, dataset)
    if loc.tier == 1:
        async with replay_store.store_session(loc.ds) as db:
            rows = (await db.execute(
                select(NoTradeDecision.stage, NoTradeDecision.code,
                       func.count(NoTradeDecision.id))
                .where(NoTradeDecision.run_id == run_id)
                .group_by(NoTradeDecision.stage, NoTradeDecision.code))).all()
        items = [{"stage": s, "code": c, "count": int(n)} for s, c, n in rows]
        source = "store"
    else:
        raw = _section(loc.report, "8").get("decisions") or []
        items = [{"stage": d.get("stage"), "code": d.get("code"), "count": int(d.get("count") or 0)}
                 for d in raw if isinstance(d, dict)]
        source = "artifact"
    items.sort(key=lambda x: (-x["count"], str(x["stage"]), str(x["code"])))
    by_stage: dict = {}
    for x in items:
        by_stage[x["stage"]] = by_stage.get(x["stage"], 0) + x["count"]
    stages = sorted(({"stage": s, "count": n} for s, n in by_stage.items()),
                    key=lambda x: (-x["count"], str(x["stage"])))
    return {"run_id": run_id, "dataset": loc.ds.dataset_id, "items": items,
            "total": len(items), "by_stage": stages, "source": source,
            "empty_reason": None if items else EMPTY_REASON}


# ── GET /backtest/runs/{run_id}/trades and /series (tier 1 only) ─────────────

def _require_per_trade(loc: Located) -> None:
    if loc.tier != 1:
        raise HTTPException(status_code=409,
                            detail="per-trade rows are not stored in the report artefact")


async def trades_page(run_id: str, dataset: Optional[str], limit: int, offset: int,
                      strategy: Optional[str], regime: Optional[str], outcome: Optional[str],
                      exit_reason: Optional[str]) -> dict:
    artifacts.check_run_id(run_id)
    if outcome is not None and outcome.upper() not in OUTCOMES:
        raise HTTPException(status_code=400, detail=f"outcome must be one of {OUTCOMES}")
    loc = await locate(run_id, dataset)
    _require_per_trade(loc)
    p, _ = run_params(_record(loc))
    f = tm.TradeFilter(run_id=run_id, strategy=strategy or None, regime=regime or None,
                       outcome=outcome.upper() if outcome else None)
    trades = await _load_trades(loc.ds, f, p.flat_epsilon)
    if exit_reason:
        trades = [t for t in trades if (t.exit_reason or "").upper() == exit_reason.upper()]
    items = [trade_payload(t, p.flat_epsilon) for t in trades[offset:offset + limit]]
    return {"run_id": run_id, "dataset": loc.ds.dataset_id, **page(items, len(trades))}


async def run_series(run_id: str, dataset: Optional[str], bin_width: float) -> dict:
    artifacts.check_run_id(run_id)
    if bin_width not in series.BIN_WIDTHS:
        raise HTTPException(status_code=400, detail=f"bin_width must be one of {series.BIN_WIDTHS}")
    loc = await locate(run_id, dataset)
    _require_per_trade(loc)
    record = _record(loc)
    p, _ = run_params(record)
    trades = await _load_trades(loc.ds, tm.TradeFilter(run_id=run_id), p.flat_epsilon)
    return {"run_id": run_id, "dataset": loc.ds.dataset_id,
            **series.build(trades, run_capital(record), p.flat_epsilon, bin_width)}
