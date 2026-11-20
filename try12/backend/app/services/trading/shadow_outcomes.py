"""
Shadow (counterfactual) outcomes of refused signals.

A signal the risk gate refuses (puts-only, the SUPERTREND block, the low-
volatility skip, a daily cap, no suitable option, ...) is saved as SKIPPED with
its spot exit plan. After the close, ``evaluate_day`` replays each plan on that
day's real 1-min spot candles exactly as the research harness does
(app.research.sim: fill at the next candle's open, pessimistic intrabar path,
the live exit_plan.tick_action, and the strategy's own candle_exit for trails),
and stores the net R after the research cost model (MEASURED:
app.research.walkforward.COST_MODELS). The evidence endpoint compares these with
the trades actually taken, so every refusal rule is tested forward on what it
gave up. Spot-based R: an estimate of what the option would have done, not a
fill.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import select

from app.services.market_data.timestamps import IST, try_normalize_candle_timestamp

logger = logging.getLogger(__name__)

NO_FILL = "NO_FILL"            # no candle after the signal (refused at the close)


def _candles(raw: list[dict]) -> list[dict]:
    """Normalised, sorted, valid in-session 1-min candles with datetime timestamps."""
    out = []
    for c in raw or []:
        t = try_normalize_candle_timestamp(c.get("timestamp"))
        if t is None:
            continue
        hm = (t.hour, t.minute)
        if not ((9, 15) <= hm < (15, 30)):
            continue
        try:
            out.append({"timestamp": t, "open": float(c["open"]), "high": float(c["high"]),
                        "low": float(c["low"]), "close": float(c["close"]),
                        "volume": float(c.get("volume") or 0)})
        except (KeyError, TypeError, ValueError):
            continue
    out.sort(key=lambda c: c["timestamp"])
    return out


def shadow_outcome(plan_dict: dict, strategy_name: str, signal_time: datetime,
                   today: list[dict], history_before: list[dict]) -> dict:
    """{r_net, exit, hold_min} for one plan on *today*'s candles (normalised).
    *history_before*: earlier sessions' candles, for strategies whose bar exits
    need warm-up (Supertrend trail)."""
    from app.research import sim
    from app.research.walkforward import COST_MODELS, PRIMARY_COST
    from app.services.trading import exit_plan as xp
    from app.strategies.strategies import build_strategies

    plan = xp.ExitPlan.from_dict(plan_dict)
    t = signal_time if signal_time.tzinfo else signal_time.replace(tzinfo=IST)
    start = t.astimezone(IST).replace(second=0, microsecond=0)
    idx = next((i for i, c in enumerate(today) if c["timestamp"] >= start), None)
    risk = plan.sign * (plan.spot_entry - plan.spot_stop)
    if idx is None or risk <= 0:
        return {"r_net": None, "exit": NO_FILL, "hold_min": None}
    fill = today[idx]
    tr = sim.Trade(strategy=strategy_name, variant="SHADOW", day=str(start.date()), side=plan.side,
                   signal_time=start.strftime("%H:%M"), entry_time=fill["timestamp"].strftime("%H:%M"),
                   plan_entry=plan.spot_entry, entry=fill["open"], stop0=plan.spot_stop,
                   target=plan.spot_target, risk_pts=round(risk, 2), entry_idx=idx)
    pts = (tr.entry + plan.sign * plan.premium_target_pts / sim.ASSUMED_DELTA
           if plan.premium_target_pts else None)
    pos = sim._OpenPosition(tr, plan, None, fill["timestamp"], pts)
    strat = next((s for s in build_strategies("NIFTY") if s.name == strategy_name), None)
    closed = False
    for i in range(idx, len(today)):
        if sim._step_candle(pos, today[i], i):
            closed = True
            break
        if strat is not None:
            strat.history = (history_before + today[:i + 1])[-sim.HISTORY_CANDLES:]
            reason = strat.candle_exit(plan, today[:i + 1], {})
            if reason:
                sim._finish(pos, i, today[i]["close"], reason,
                            today[i]["timestamp"] + timedelta(minutes=1))
                closed = True
                break
    if not closed:
        last = today[-1]
        sim._finish(pos, len(today) - 1, last["close"], "DATA_END",
                    last["timestamp"] + timedelta(minutes=1))
    cost_r = COST_MODELS[PRIMARY_COST].pts(tr.hold_min, False) / tr.risk_pts
    return {"r_net": round(tr.r_gross - cost_r, 4), "exit": tr.exit_reason[:20],
            "hold_min": tr.hold_min}


async def evaluate_day(db, run_id: str, day, raw_candles: list[dict]) -> int:
    """Fill the shadow outcome of every SKIPPED NIFTY signal of *day* that has an
    exit plan and no outcome yet. Returns how many were written."""
    from app.db.models.paper_trade import SignalStatus, StrategySignal
    candles = _candles(raw_candles)
    today = [c for c in candles if c["timestamp"].date() == day]
    before = [c for c in candles if c["timestamp"].date() < day]
    if not today:
        return 0
    start = datetime.combine(day, datetime.min.time(), IST)
    rows = (await db.execute(select(StrategySignal).where(
        StrategySignal.run_id == run_id, StrategySignal.underlying == "NIFTY",
        StrategySignal.status == SignalStatus.SKIPPED,
        StrategySignal.exit_plan.is_not(None), StrategySignal.shadow_exit.is_(None),
        StrategySignal.signal_time >= start,
        StrategySignal.signal_time < start + timedelta(days=1)))).scalars().all()
    n = 0
    for r in rows:
        try:
            res = shadow_outcome(r.exit_plan, r.strategy_name, r.signal_time, today, before)
        except Exception as exc:                       # one bad plan must not stop the rest
            logger.warning("[shadow] signal %s not evaluated: %s", r.id, exc)
            continue
        r.shadow_r_net, r.shadow_exit, r.shadow_hold_min = res["r_net"], res["exit"], res["hold_min"]
        n += 1
    if n:
        await db.commit()
    logger.info("[shadow] %s: %d refused signal(s) evaluated", day, n)
    return n


def summarize(rows: list[dict]) -> dict:
    """{strategy: {skip_reason_code: {n, mean_r_net}}} from evaluated shadow rows
    ({strategy, code, r_net})."""
    from collections import defaultdict
    g: dict = defaultdict(lambda: defaultdict(list))
    for r in rows:
        if r.get("r_net") is not None:
            g[r["strategy"]][r["code"] or "OTHER"].append(float(r["r_net"]))
    return {s: {c: {"n": len(v), "mean_r_net": round(sum(v) / len(v), 4)} for c, v in by.items()}
            for s, by in g.items()}


def skip_code(reason: Optional[str]) -> str:
    """A stable bucket for a skip_reason text (the refusing rule)."""
    text = (reason or "").lower()
    for code, needle in (("DIRECTION_POLICY", "puts only"), ("EVIDENCE_BLOCKED", "blocked by research"),
                         ("EVIDENCE_BLOCKED", "own paper trades"), ("LOW_VOL_SKIP", "low volatility"),
                         ("DAILY_TRADE_CAP", "daily trade limit"), ("BLACKOUT", "blackout"),
                         ("ENTRY_CUTOFF", "entry cutoff")):
        if needle in text:
            return code
    return "OTHER"
