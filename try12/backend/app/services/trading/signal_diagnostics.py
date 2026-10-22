"""
Pure helpers for GET /trading/signals/diagnostics — no I/O, no clock reads.

The route gathers a state snapshot (dict); this module derives the session-window
predicate and the blocker list from it, so the taxonomy is unit-testable without
a DB, Redis or a running feed. Codes mirror backend/scripts/diagnose_blockers.py.
"""
from __future__ import annotations

from datetime import datetime, time
from typing import Optional

SESSION_WINDOW_NOTE = "clock-based; exchange holidays are not detected"
EXCHANGE_CLOSE_IST = time(15, 30)


def session_window_open(now_ist: datetime, start: time, end: time) -> bool:
    """IST weekday and start <= HH:MM <= end — the same inclusive minute test the candle loop uses."""
    if now_ist.weekday() >= 5:
        return False
    mins = now_ist.hour * 60 + now_ist.minute
    return start.hour * 60 + start.minute <= mins <= end.hour * 60 + end.minute


def format_session_window(start: time, end: time) -> str:
    return f"{start:%H:%M}-{end:%H:%M} IST"


def vix_gate(ltp: Optional[float], vix_min: float, vix_max: float) -> str:
    """Mirror of the runner's VIX gate: no VIX in Redis bypasses the gate."""
    if ltp is None:
        return "BYPASSED"
    return "BLOCKED" if (ltp < vix_min or ltp > vix_max) else "PASS"


def _market_closed_detail(now_ist: datetime, start: time, end: time) -> str:
    if now_ist.weekday() >= 5:
        return f"signal generation runs on weekdays {format_session_window(start, end)}; today is a weekend"
    mins = now_ist.hour * 60 + now_ist.minute
    if mins < start.hour * 60 + start.minute:
        return f"signal generation starts at {start:%H:%M} IST"
    return f"signal generation stops at {end:%H:%M} IST; the exchange closes at {EXCHANGE_CLOSE_IST:%H:%M}"


def _b(code: str, severity: str, message: str, *, underlying: Optional[str] = None,
       detail: Optional[str] = None) -> dict:
    out = {"code": code, "severity": severity, "message": message}
    if underlying is not None:
        out["underlying"] = underlying
    if detail is not None:
        out["detail"] = detail
    return out


def derive_blockers(state: dict) -> list[dict]:
    """
    State keys (all optional unless noted; None means "unknown", never "healthy-false"):
      now_ist (required), session_start, session_end (required),
      db_available, runner_running, study_phase,
      feed: {connected}, vix: {ltp, min, max},
      capital: {available, daily_trades, max_trades_per_day, open_positions,
                max_open_positions, circuit_breaker, circuit_reason},
      strategies: {total, enabled},
      underlyings: [{underlying, candles_today, candles_required, atr, atr_min, regime,
                     regime_allows_trading, in_cooldown, prev_day_snapshot,
                     lot_cost, atm_premium, lot_size, lot_cost_unavailable}],
      probe_errors: [{code, detail}]
    Returns [] when nothing blocks signal generation or trade conversion.
    """
    now: datetime = state["now_ist"]
    start: time = state["session_start"]
    end: time = state["session_end"]
    in_window = session_window_open(now, start, end)
    blockers: list[dict] = []

    if not in_window:
        blockers.append(_b("MARKET_CLOSED", "blocking", "Outside the signal-generation window.",
                           detail=_market_closed_detail(now, start, end)))

    if state.get("db_available") is False:
        blockers.append(_b("DB_UNAVAILABLE", "blocking", "Database is unreachable.",
                           detail="signals cannot be persisted; counts are unavailable"))

    if state.get("runner_running") is False:
        blockers.append(_b("RUNNER_STOPPED", "blocking", "Strategy runner is not running."))

    if (state.get("feed") or {}).get("connected") is False:
        blockers.append(_b("FEED_DOWN", "blocking", "Market feed is not connected."))

    if state.get("study_phase") is True:
        blockers.append(_b("STUDY_PHASE", "blocking", "Study phase is active.",
                           detail="signals are recorded as SKIPPED; no paper positions are opened"))

    vix = state.get("vix") or {}
    if vix.get("ltp") is not None and vix_gate(vix["ltp"], vix["min"], vix["max"]) == "BLOCKED":
        blockers.append(_b("VIX_OUT_OF_RANGE", "blocking",
                           f"India VIX {vix['ltp']:.2f} is outside {vix['min']:g}-{vix['max']:g}.",
                           detail="the runner skips every strategy while VIX is out of range"))

    available = (state.get("capital") or {}).get("available")

    for u in state.get("underlyings") or []:
        name = u.get("underlying")
        candles_today = u.get("candles_today")
        required = u.get("candles_required")
        if in_window and candles_today is not None and required is not None:
            if candles_today == 0:
                blockers.append(_b("NO_CANDLES_TODAY", "warning", f"No {name} candles today.",
                                   underlying=name,
                                   detail="feed down or an exchange holiday (holidays are not detected)"))
            if candles_today < required:
                blockers.append(_b("INSUFFICIENT_CANDLES", "blocking",
                                   f"{name} has {candles_today} of {required} candles needed today.",
                                   underlying=name))
            elif u.get("atr") is not None and u.get("atr_min") is not None and u["atr"] < u["atr_min"]:
                blockers.append(_b("ATR_BELOW_MIN", "blocking",
                                   f"{name} ATR {u['atr']:.2f} is below {u['atr_min']:g}.",
                                   underlying=name, detail="the runner treats the market as too choppy"))

        if u.get("regime_allows_trading") is False:
            blockers.append(_b("REGIME_BLOCKS_ALL", "blocking",
                               f"Regime {u.get('regime')} permits no strategies.", underlying=name,
                               detail=f"regime_rules.py gives {u.get('regime')} an empty enabled_strategies set. "
                                      "No signal can convert to a trade while this regime holds."))

        if u.get("in_cooldown") is True:
            blockers.append(_b("REGIME_TRANSITION_COOLDOWN", "warning",
                               f"{name} is in a post-transition regime cooldown.", underlying=name))

        lot_cost = u.get("lot_cost")
        if lot_cost is not None and available is not None:
            if available < lot_cost:
                blockers.append(_b(
                    "CAPITAL_BELOW_LOT_COST", "blocking",
                    f"Available capital {available:,.0f} is below the {lot_cost:,.0f} needed for 1 {name} lot.",
                    underlying=name,
                    detail=f"Observed ATM premium {u.get('atm_premium')} x lot size {u.get('lot_size')}. "
                           "Increase capital or trade the cheaper underlying.",
                ))
        elif u.get("lot_cost_unavailable"):
            blockers.append(_b("CAPITAL_BELOW_LOT_COST", "warning",
                               f"Lot cost for {name} could not be computed.", underlying=name,
                               detail="no live ATM premium available to compute lot cost"))

    strategies = state.get("strategies") or {}
    if strategies.get("total") and strategies.get("enabled") == 0:
        blockers.append(_b("ALL_STRATEGIES_DISABLED", "blocking", "Every strategy is disabled."))

    cap = state.get("capital") or {}
    if cap.get("max_trades_per_day") and cap.get("daily_trades") is not None \
            and cap["daily_trades"] >= cap["max_trades_per_day"]:
        blockers.append(_b("DAILY_TRADE_CAP_REACHED", "blocking",
                           f"Daily trade cap reached ({cap['daily_trades']}/{cap['max_trades_per_day']})."))
    if cap.get("max_open_positions") and cap.get("open_positions") is not None \
            and cap["open_positions"] >= cap["max_open_positions"]:
        blockers.append(_b("MAX_OPEN_POSITIONS_REACHED", "blocking",
                           f"Max open positions reached ({cap['open_positions']}/{cap['max_open_positions']})."))
    if cap.get("circuit_breaker") is True:
        blockers.append(_b("CIRCUIT_BREAKER", "blocking", "Daily-loss circuit breaker is active.",
                           detail=cap.get("circuit_reason") or None))

    for e in state.get("probe_errors") or []:
        blockers.append(_b(e.get("code", "PROBE_FAILED"), "warning", "probe unavailable",
                           detail=str(e.get("detail"))))

    return blockers
