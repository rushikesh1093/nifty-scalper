"""
Analytics API (design §17.1), prefix /api/v1/analytics.

  GET  /trades                       paginated closed trades (every R15.1 field)
  GET  /metrics                      MetricSet + DailyMetricSet for a filter
  GET  /metrics/by/{dimension}       strategy | regime | strategy_regime | underlying | day
  GET  /no-trade-decisions           paginated no-trade rows
  GET  /no-trade-decisions/summary   counts per (stage, code)
  GET  /selection-rejections         option-selection rejections per ReasonCode / day
  GET  /risk-state                   the live risk picture of the bound run
  GET  /validation                   the D10 go/no-go over the PAPER run (read-only)
  GET  /runs                         replay runs (cross-run by design)
  GET  /runs/{run_id}                one replay run with its rendered report
  POST /reconcile                    admin key; rebuild one run's aggregates

Every query against a run-shared table carries ``run_id`` (default PAPER); no
response mixes runs (D12). Empty results are HTTP 200 with an explicit empty
payload, never a bare 0 (§17.3). Metrics come only from
app/services/metrics/trade_metrics.py (R15.6).
"""
from __future__ import annotations

from datetime import date
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import desc, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.guards import date_range, require_admin_key, run_id_query, validate_run_id
from app.api.payloads import EMPTY_REASON
from app.api.payloads import jsonable as _jsonable
from app.api.payloads import page as _page
from app.api.payloads import trade_payload as _trade_payload
from app.db.base import get_db
from app.db.models.replay import ReplayRun
from app.db.models.risk import NoTradeDecision, PositionEvent, RiskHalt
from app.runtime.context import ctx, params
from app.services.metrics import trade_metrics as tm
from app.strategies.names import ALL_STRATEGY_NAMES

router = APIRouter(prefix="/analytics", tags=["analytics"])

OUTCOMES = ("WIN", "LOSS", "FLAT")


def _filter(run_id: str, date_from: Optional[str], date_to: Optional[str],
            underlying: Optional[str], strategy: Optional[str], regime: Optional[str],
            outcome: Optional[str], mode: str) -> tm.TradeFilter:
    d_from, d_to = date_range(date_from, date_to)
    if strategy is not None and strategy not in ALL_STRATEGY_NAMES:
        raise HTTPException(status_code=400,
                            detail=f"strategy must be one of {sorted(ALL_STRATEGY_NAMES)}")
    if outcome is not None and outcome.upper() not in OUTCOMES:
        raise HTTPException(status_code=400, detail=f"outcome must be one of {OUTCOMES}")
    if mode.upper() != "PAPER":
        raise HTTPException(status_code=400, detail="mode must be PAPER (paper trading only)")
    return tm.TradeFilter(run_id=run_id, date_from=d_from, date_to=d_to,
                          underlying=underlying.upper() if underlying else None,
                          strategy=strategy, regime=regime,
                          outcome=outcome.upper() if outcome else None, mode="PAPER")


async def _run_capital(db: AsyncSession, run_id: str) -> float:
    """Capital the run's percentages are measured against: the live RunConfig for
    PAPER, the replay's frozen RunConfig for an RPL- run."""
    if run_id != ctx().run_id:
        run = (await db.execute(select(ReplayRun).where(ReplayRun.run_id == run_id))).scalar_one_or_none()
        cfg = ((run.params or {}).get("cfg") or {}) if run is not None else {}
        if cfg.get("capital"):
            return float(cfg["capital"])
    return float(ctx().cfg.capital)


# ── trades and metrics ───────────────────────────────────────────────────────

@router.get("/trades")
async def list_trades(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None, strategy: Optional[str] = None,
    regime: Optional[str] = None, outcome: Optional[str] = None, mode: str = "PAPER",
    include_events: bool = False,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    f = _filter(run_id, date_from, date_to, underlying, strategy, regime, outcome, mode)
    p = params()
    trades = await tm.load_trades(db, f, flat_epsilon=p.flat_epsilon)
    page = trades[offset:offset + limit]
    items = [_trade_payload(t, p.flat_epsilon) for t in page]
    if include_events and page:
        ids = [t.position_id for t in page]
        events = (await db.execute(
            select(PositionEvent).where(PositionEvent.run_id == run_id,
                                        PositionEvent.position_id.in_(ids))
            .order_by(PositionEvent.position_id, PositionEvent.occurred_at, PositionEvent.id)
        )).scalars().all()
        by_pos: dict[int, list] = {}
        for e in events:
            by_pos.setdefault(e.position_id, []).append({
                "event_type": e.event_type, "occurred_at": _jsonable(e.occurred_at),
                "price": e.price, "from_value": e.from_value, "to_value": e.to_value,
                "tier_label": e.tier_label, "detail": e.detail,
            })
        for item in items:
            item["events"] = by_pos.get(item["position_id"], [])
    return {"run_id": run_id, **_page(items, len(trades))}


@router.get("/metrics")
async def get_metrics(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None, strategy: Optional[str] = None,
    regime: Optional[str] = None, outcome: Optional[str] = None, mode: str = "PAPER",
    db: AsyncSession = Depends(get_db),
):
    f = _filter(run_id, date_from, date_to, underlying, strategy, regime, outcome, mode)
    p = params()
    trades = await tm.load_trades(db, f, flat_epsilon=p.flat_epsilon)
    capital = await _run_capital(db, run_id)
    out = {
        "run_id": run_id, "capital": capital,
        "metrics": _jsonable(tm.compute(trades, capital, p)),
        "daily": _jsonable(tm.compute_daily(trades, capital)),
    }
    if not trades:
        out["empty_reason"] = EMPTY_REASON
    return out


@router.get("/strategy-report")
async def strategy_report(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    include_trades: bool = False,
    db: AsyncSession = Depends(get_db),
):
    """Per-strategy report from the trade log: win rate, average win / loss,
    expectancy before and after charges, exit reasons, expiry day vs other days,
    P&L by entry hour (app.services.metrics.strategy_report)."""
    from app.services.metrics import strategy_report as sr
    d_from, d_to = date_range(date_from, date_to)
    log = await sr.load_closed(db, run_id, d_from, d_to)
    out = {"run_id": run_id, "date_from": date_from, "date_to": date_to,
           "report": sr.build_report(log)}
    if include_trades:
        out["trades"] = _jsonable(log)
    if not log:
        out["empty_reason"] = EMPTY_REASON
    return out


async def _shadow_summary(db: AsyncSession, run_id: str) -> dict:
    """What refused signals would have made, per strategy and refusing rule
    (app.services.trading.shadow_outcomes), since each strategy's rule date."""
    from app.db.models.paper_trade import StrategySignal
    from app.services.metrics.strategy_evidence import RULE_SINCE
    from app.services.trading import shadow_outcomes as so
    rows = (await db.execute(
        select(StrategySignal.strategy_name, StrategySignal.signal_time, StrategySignal.skip_reason,
               StrategySignal.shadow_r_net, NoTradeDecision.code)
        .outerjoin(NoTradeDecision, NoTradeDecision.id == StrategySignal.no_trade_decision_id)
        .where(StrategySignal.run_id == run_id, StrategySignal.shadow_r_net.is_not(None)))).all()
    out = []
    for name, t, reason, r, code in rows:
        since = RULE_SINCE.get(name)
        if since is not None and t is not None and t.date() < since:
            continue
        out.append({"strategy": name, "code": code or so.skip_code(reason), "r_net": r})
    return so.summarize(out)


@router.get("/strategy-evidence")
async def strategy_evidence(run_id: str = Depends(run_id_query),
                            db: AsyncSession = Depends(get_db)):
    """Forward evidence per strategy from this run's closed paper trades, counted
    only since each strategy's current rules went live, against the research
    promotion gates (app.services.metrics.strategy_evidence). Advice only: no
    flag is changed."""
    from app.research.walkforward import criteria_dict
    from app.services.metrics import strategy_evidence as se
    from app.services.metrics import strategy_report as sr
    log = await sr.load_closed(db, run_id)
    p = params()
    out = {"run_id": run_id, "criteria": criteria_dict(),
           "rule_since": {k: str(v) for k, v in se.RULE_SINCE.items()},
           # The policy the risk gate applies now (TradingParams), so the dashboard
           # shows the engine's settings, not a copy of them.
           "policy": {"evidence_blocked_strategies": list(p.evidence_blocked_strategies),
                      "evidence_auto_block": p.evidence_auto_block,
                      "pe_only_strategies": list(p.pe_only_strategies),
                      "low_vol_skip_strategies": list(p.low_vol_skip_strategies),
                      "max_option_buys_per_day": p.max_option_buys_per_day,
                      "regime_gating": p.regime_gating},
           "strategies": _jsonable(se.evaluate(log)),
           "shadow": await _shadow_summary(db, run_id)}
    if not log:
        out["empty_reason"] = EMPTY_REASON
    return out


@router.get("/metrics/by/{dimension}")
async def get_metrics_by(
    dimension: str,
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None, strategy: Optional[str] = None,
    regime: Optional[str] = None, mode: str = "PAPER",
    db: AsyncSession = Depends(get_db),
):
    if dimension not in tm.DIMENSIONS:
        raise HTTPException(status_code=400, detail=f"dimension must be one of {tm.DIMENSIONS}")
    f = _filter(run_id, date_from, date_to, underlying, strategy, regime, None, mode)
    p = params()
    trades = await tm.load_trades(db, f, flat_epsilon=p.flat_epsilon)
    capital = await _run_capital(db, run_id)
    groups = tm.group_by(trades, dimension, capital, p)
    out = {"run_id": run_id, "dimension": dimension,
           "groups": {k: _jsonable(v) for k, v in groups.items()}, "total": len(groups)}
    if not groups:
        out["empty_reason"] = EMPTY_REASON
    return out


# ── no-trade decisions ───────────────────────────────────────────────────────

def _ntd_query(q, run_id, d_from, d_to, underlying, strategy, stage, code):
    q = q.where(NoTradeDecision.run_id == run_id)
    if d_from:
        q = q.where(NoTradeDecision.trading_day >= d_from)
    if d_to:
        q = q.where(NoTradeDecision.trading_day <= d_to)
    if underlying:
        q = q.where(NoTradeDecision.underlying == underlying.upper())
    if strategy:
        q = q.where(NoTradeDecision.strategy_name == strategy)
    if stage:
        q = q.where(NoTradeDecision.stage == stage.upper())
    if code:
        q = q.where(NoTradeDecision.code == code.upper())
    return q


@router.get("/no-trade-decisions")
async def list_no_trade_decisions(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None, strategy: Optional[str] = None,
    stage: Optional[str] = None, code: Optional[str] = None,
    limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0),
    db: AsyncSession = Depends(get_db),
):
    d_from, d_to = date_range(date_from, date_to)
    total = int((await db.execute(_ntd_query(
        select(func.count(NoTradeDecision.id)), run_id, d_from, d_to, underlying,
        strategy, stage, code))).scalar() or 0)
    rows = (await db.execute(_ntd_query(
        select(NoTradeDecision), run_id, d_from, d_to, underlying, strategy, stage, code)
        .order_by(NoTradeDecision.id).offset(offset).limit(limit))).scalars().all()
    items = [{
        "id": r.id, "run_id": r.run_id, "trading_day": _jsonable(r.trading_day),
        "decided_at": _jsonable(r.decided_at), "stage": r.stage, "code": r.code,
        "underlying": r.underlying, "strategy_name": r.strategy_name,
        "signal_id": r.signal_id, "position_id": r.position_id,
        "reason": r.reason, "context": r.context,
    } for r in rows]
    return {"run_id": run_id, **_page(items, total)}


@router.get("/no-trade-decisions/summary")
async def no_trade_summary(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None, strategy: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    d_from, d_to = date_range(date_from, date_to)
    rows = (await db.execute(_ntd_query(
        select(NoTradeDecision.stage, NoTradeDecision.code, func.count(NoTradeDecision.id)),
        run_id, d_from, d_to, underlying, strategy, None, None)
        .group_by(NoTradeDecision.stage, NoTradeDecision.code)
        .order_by(NoTradeDecision.stage, NoTradeDecision.code))).all()
    items = [{"stage": s, "code": c, "count": int(n)} for s, c, n in rows]
    return {"run_id": run_id, **_page(items, len(items))}


@router.get("/selection-rejections")
async def selection_rejections_summary(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    underlying: Optional[str] = None,
    db: AsyncSession = Depends(get_db),
):
    """Per-ReasonCode option-selection rejections (item 7), SELECT only, summed in
    Python from the tallies the rows already store:

    - ``no_trade_decisions.context['rejections']`` of NO_OPTION_AVAILABLE rows
      (every strike was rejected);
    - ``strategy_signals.selection_rejections`` of signals whose option WAS
      selected (the other strikes' rejections). A NO_OPTION_AVAILABLE signal
      carries the same tally as its decision row, so it is not counted twice.
    """
    from app.db.models.paper_trade import StrategySignal
    from app.services.trading.session_times import trading_day as ist_day

    d_from, d_to = date_range(date_from, date_to)
    by_code: dict[str, int] = {}
    by_day: dict[str, dict[str, int]] = {}

    def _add(day: date, tally) -> bool:
        if not isinstance(tally, dict) or not tally:
            return False
        bucket = by_day.setdefault(day.isoformat(), {})
        for code, n in tally.items():
            try:
                n = int(n)
            except (TypeError, ValueError):
                continue
            by_code[str(code)] = by_code.get(str(code), 0) + n
            bucket[str(code)] = bucket.get(str(code), 0) + n
        return True

    ntd_rows = (await db.execute(_ntd_query(
        select(NoTradeDecision.trading_day, NoTradeDecision.context),
        run_id, d_from, d_to, underlying, None, None, "NO_OPTION_AVAILABLE")
        .order_by(NoTradeDecision.id))).all()
    n_ntd = sum(1 for day, ctx_ in ntd_rows
                if _add(day, (ctx_ or {}).get("rejections") if isinstance(ctx_, dict) else None))

    q = select(StrategySignal.signal_time, StrategySignal.suggested_token,
               StrategySignal.selection_rejections).where(StrategySignal.run_id == run_id)
    if underlying:
        q = q.where(StrategySignal.underlying == underlying.upper())
    n_sig = 0
    for at, token, tally in (await db.execute(q.order_by(StrategySignal.id))).all():
        if not token or at is None:
            continue
        day = ist_day(at)
        if (d_from and day < d_from) or (d_to and day > d_to):
            continue
        n_sig += 1 if _add(day, tally) else 0

    out = {"run_id": run_id, "by_code": by_code,
           "by_day": dict(sorted(by_day.items())),
           "sources": {"no_option_available": n_ntd, "selected_signals": n_sig}}
    if not by_code:
        out["empty_reason"] = EMPTY_REASON
    return out


# ── risk state ───────────────────────────────────────────────────────────────

@router.get("/risk-state")
async def risk_state_view(db: AsyncSession = Depends(get_db)):
    """The full risk picture of the bound run (R16.4). Capital in use is derived
    from open positions; the trading_config display cache is never read."""
    from app.services.trading import risk_state
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.session_times import entry_cutoff

    rc = ctx()
    cfg, p, day = rc.cfg, params(), rc.clock.trading_day()
    used = await risk_state.open_capital_used(db, rc.run_id)
    count = await risk_state.derived_trade_count(db, rc.run_id, day)
    loss = await risk_state.daily_loss_figure(db)
    streak = await risk_state.streak_state(db, rc.run_id, day)
    halts = (await db.execute(select(RiskHalt).where(
        RiskHalt.run_id == rc.run_id, RiskHalt.trading_day == day).order_by(RiskHalt.id)
    )).scalars().all()
    degraded = (await db.execute(select(NoTradeDecision.code).where(
        NoTradeDecision.run_id == rc.run_id, NoTradeDecision.trading_day == day,
        NoTradeDecision.stage == "DEGRADATION").distinct())).scalars().all()
    halted, why = await mode_manager.kill_switch_state()
    now = rc.clock.now_utc()
    remaining = None
    if streak.cooldown_until is not None and streak.cooldown_until > now:
        remaining = int((streak.cooldown_until - now).total_seconds() // 60) + 1
    violations = p.validate()
    return {
        "run_id": rc.run_id, "trading_day": day.isoformat(), "mode": cfg.mode,
        "capital": cfg.capital, "used_capital": round(used, 2),
        "available_capital": round(cfg.capital - used, 2),
        "open_exposure": round(used, 2),
        "open_exposure_cap": round(cfg.open_exposure_cap_pct / 100.0 * cfg.capital, 2),
        "trade_count": count, "max_trades_per_day": cfg.max_trades_per_day,
        "daily_loss": _jsonable(loss),
        "streak": {
            "consecutive_losses": streak.consecutive_losses,
            "halt_n": p.losing_streak_halt_n, "global_halt": streak.global_halt,
            "per_strategy": dict(streak.per_strategy_losses),
            "strategy_halt_n": p.strategy_streak_halt_n,
            "halted_strategies": sorted(streak.halted_strategies),
            "cooldown_until": _jsonable(streak.cooldown_until),
            "cooldown_minutes_remaining": remaining,
        },
        "halts": [{
            "halt_type": h.halt_type, "scope": h.scope, "reason": h.reason,
            "engaged_at": _jsonable(h.engaged_at), "released_at": _jsonable(h.released_at),
            "released_by": h.released_by, "release_note": h.release_note,
            "expires": "end of trading day",
        } for h in halts],
        "kill_switch": {"halted": halted, "reason": why or None},
        "entry_cutoff_ist": entry_cutoff(cfg.square_off_time, cfg.entry_cutoff_buffer_min)
        .strftime("%H:%M"),
        "square_off_ist": cfg.square_off_time,
        "degraded": sorted(degraded),
        "config_valid": not violations, "config_violations": violations,
    }


# ── the PAPER go/no-go (design §18, D10; read-only) ──────────────────────────

@router.get("/validation")
async def paper_validation_view(
    run_id: str = Depends(run_id_query),
    date_from: Optional[str] = None, date_to: Optional[str] = None,
    iterations: int = Query(2000, ge=200, le=20000),
    seed: Optional[int] = None,
    oos_split: float = Query(0.70, gt=0, lt=1),
    db: AsyncSession = Depends(get_db),
):
    """The six-criterion validation report over the PAPER run's rows. SELECT only,
    never commits. A replay run's report is stored on its row: /analytics/runs/{run_id}."""
    from app.core.config import settings
    from app.services.replay.paper_validation import PAPER_RUN_ID, build_paper_validation

    if run_id != PAPER_RUN_ID:
        raise HTTPException(status_code=400,
                            detail=f"validation is computed for run_id=PAPER only; a replay "
                                   f"run's report is at /api/v1/analytics/runs/{run_id}")
    d_from, d_to = date_range(date_from, date_to)
    capital = await _run_capital(db, run_id)
    return await build_paper_validation(
        db, run_id=run_id, params=params(), capital=capital, date_from=d_from, date_to=d_to,
        iterations=iterations, seed=settings.REPLAY_DEFAULT_SEED if seed is None else seed,
        oos_split=oos_split)


# ── replay runs (cross-run by design: the allowlisted listing) ───────────────

def _run_payload(r: ReplayRun, *, full: bool) -> dict:
    validation = r.validation or {}
    out = {
        "run_id": r.run_id, "status": r.status, "created_at": _jsonable(r.created_at),
        "date_from": _jsonable(r.date_from), "date_to": _jsonable(r.date_to),
        "underlyings": r.underlyings, "params": r.params, "params_hash": r.params_hash,
        "seed": r.seed, "code_version": r.code_version, "coverage": r.coverage,
        "metrics": r.metrics, "notes": r.notes,
        "recommendation": validation.get("recommendation"),
    }
    if full:
        out["validation"] = validation
    return out


@router.get("/runs")
async def list_runs(limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0),
                    db: AsyncSession = Depends(get_db)):
    total = int((await db.execute(select(func.count(ReplayRun.run_id)))).scalar() or 0)
    rows = (await db.execute(select(ReplayRun).order_by(desc(ReplayRun.created_at))
                             .offset(offset).limit(limit))).scalars().all()
    return _page([_run_payload(r, full=False) for r in rows], total)


@router.get("/runs/{run_id}")
async def get_run(run_id: str, db: AsyncSession = Depends(get_db)):
    validate_run_id(run_id)
    r = (await db.execute(select(ReplayRun).where(ReplayRun.run_id == run_id))).scalar_one_or_none()
    if r is None:
        raise HTTPException(status_code=404, detail=f"replay run {run_id} not found")
    return _run_payload(r, full=True)


@router.post("/reconcile")
async def reconcile(run_id: str = Depends(run_id_query),
                    _label: str = Depends(require_admin_key)):
    from app.services.trading.reconcile import reconcile_performance
    report = await reconcile_performance(run_id)
    return _jsonable(report)
