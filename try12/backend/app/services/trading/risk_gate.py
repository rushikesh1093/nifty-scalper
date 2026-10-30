"""
The single pre-trade risk gate (design §9.1).

Two phases, because sizing needs the chosen contract and the cap/halt checks
must run before the contract is chosen:

  evaluate_pre_selection   checks 0-11 and 11b   (no I/O beyond reads)
  evaluate_sizing          check 12              (lots from the fill and lot size)
  enter                    checks 13a, 13b       under the (run_id, trading_day) lock

Order for ``paper_engine.handle_signal``:
  evaluate_pre_selection → select(base_amount, regime_multiplier)
  → evaluate_sizing → enter (lock → 13a → 13b → insert → commit)

Nothing is reserved anywhere: capital in use is a sum over this run's committed
open positions, so a rejected or failed entry has nothing to release.

No function here takes a TradingConfig row. Capital and caps come from
``ctx().cfg`` (the frozen RunConfig); tunables from ``params()``.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import logging
import math
from dataclasses import dataclass, field
from datetime import date, time as dtime
from typing import Any, Awaitable, Callable, Optional

from sqlalchemy import func, select, text
from sqlalchemy.exc import DBAPIError

from app.db.models.paper_trade import PaperPosition, StrategyPerf
from app.runtime.context import ctx, now_ist, params, trading_day
from app.services.trading import fills, no_trade, risk_state
from app.services.trading.no_trade import ReasonCode, Stage
from app.services.trading.session_times import is_after_entry_cutoff

logger = logging.getLogger(__name__)

LOCK_TIMEOUT_S = 5.0
_sqlite_locks: dict[tuple, asyncio.Lock] = {}


def _ist_time() -> dtime:
    """Current IST wall time from the ctx clock (naive). Patchable in tests."""
    return now_ist().time().replace(tzinfo=None)


def round_to_500(x: float) -> float:
    """Floor to the nearest ₹500. Rounding DOWN is deliberate: the deployed
    amount must never exceed the fraction of capital the sizing authorised."""
    return math.floor(max(0.0, x) / 500.0) * 500.0


def calc_lots(trade_amount: float, price: float, lot_size: int) -> tuple[int, float]:
    """(lots, capital_used): floor(trade_amount / cost_per_lot), 0 when one lot
    costs more than the amount (never silently rounded up to 1)."""
    if not price or price <= 0 or not lot_size or lot_size <= 0:
        return 0, 0.0
    cost_per_lot = price * lot_size
    lots = math.floor(trade_amount / cost_per_lot)
    return lots, round(lots * cost_per_lot, 2)


# ── records ──────────────────────────────────────────────────────────────────

@dataclass(frozen=True)
class FractionDecision:
    """The candidate-independent part of sizing (phase one)."""
    method: str                              # 'FIXED_FRACTION' | 'KELLY'
    applied_fraction: float
    base_amount: float                       # round_to_500(applied_fraction × capital); UNadjusted
    p: Optional[float] = None
    avg_win: Optional[float] = None
    avg_loss: Optional[float] = None
    b: Optional[float] = None
    kelly_full: Optional[float] = None
    kelly_multiplier: Optional[float] = None
    decided_r: int = 0
    clamps: list = field(default_factory=list)
    reject: Optional[ReasonCode] = None      # KELLY_NO_EDGE only


@dataclass(frozen=True)
class SizingDecision:
    fraction: FractionDecision
    regime_multiplier: float                 # applied here, and ONLY here, to the deployed amount
    expected_entry_fill: float
    lot_size: int
    trade_amount: float
    lots: int
    capital_used: float
    clamps: list = field(default_factory=list)
    binding_clamp: Optional[str] = None
    reject: Optional[ReasonCode] = None


@dataclass(frozen=True)
class GateDecision:
    allowed: bool
    stage: Stage
    code: Optional[ReasonCode]
    reason: str
    context: dict = field(default_factory=dict)
    sizing: Optional[SizingDecision] = None
    regime_multiplier: float = 1.0
    fraction: Optional[FractionDecision] = None
    result: Any = None                       # what ``enter``'s insert callback returned


def _ok(**kw) -> GateDecision:
    return GateDecision(allowed=True, stage=kw.pop("stage", Stage.RISK_GATE), code=None,
                        reason="", **kw)


def _no(stage: Stage, code: ReasonCode, reason: str, **context) -> GateDecision:
    return GateDecision(allowed=False, stage=stage, code=code, reason=reason,
                        context=no_trade.as_context(**context))


def jsonable(obj: Any) -> Any:
    """dataclass / enum / nested containers → JSON-safe (sizing_detail)."""
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        return {f.name: jsonable(getattr(obj, f.name)) for f in dataclasses.fields(obj)}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [jsonable(x) for x in obj]
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if hasattr(obj, "value") and not isinstance(obj, (int, float, str)):
        return obj.value
    return obj


# ── Kelly (design §9.5) ──────────────────────────────────────────────────────

def kelly_from_stats(winning_r: int, losing_r: int, sum_r_win: float, sum_r_loss: float):
    """(p, avg_win, avg_loss, b, kelly_full) over the R-valid, non-R-flat
    population, or a string naming the guard that stopped it:
    'FIXED_FRACTION' (sample too small / avg_loss undefined) or 'NO_EDGE'.
    Guards run in the §9.5 order, BEFORE any division."""
    p_ = params()
    decided_r = winning_r + losing_r
    if decided_r < p_.kelly_min_trades:
        return "FIXED_FRACTION"
    if losing_r == 0:
        return "FIXED_FRACTION"                  # avg_loss undefined
    if winning_r == 0:
        return "NO_EDGE"                         # p == 0
    p = winning_r / decided_r
    avg_win = sum_r_win / winning_r
    avg_loss = abs(sum_r_loss) / losing_r
    if avg_loss <= 0:
        return "FIXED_FRACTION"
    b = avg_win / avg_loss
    kelly = (p * b - (1 - p)) / b
    return p, avg_win, avg_loss, b, kelly


async def fraction_decision(db, strategy: str) -> FractionDecision:
    p_ = params()
    capital = ctx().cfg.capital
    fixed = p_.fixed_fraction_pct / 100.0
    perf = (await db.execute(
        select(StrategyPerf).where(StrategyPerf.strategy_name == strategy,
                                   StrategyPerf.run_id == ctx().run_id)
    )).scalar_one_or_none()

    def _fixed(decided: int = 0) -> FractionDecision:
        return FractionDecision(method="FIXED_FRACTION", applied_fraction=fixed,
                                base_amount=round_to_500(fixed * capital), decided_r=decided)

    if perf is None:
        return _fixed()
    w, l = int(perf.winning_trades_r or 0), int(perf.losing_trades_r or 0)
    res = kelly_from_stats(w, l, float(perf.sum_r_win or 0.0), float(perf.sum_r_loss or 0.0))
    if res == "FIXED_FRACTION":
        return _fixed(w + l)
    if res == "NO_EDGE":
        return FractionDecision(method="KELLY", applied_fraction=0.0, base_amount=0.0,
                                p=0.0, decided_r=w + l, reject=ReasonCode.KELLY_NO_EDGE)
    p, avg_win, avg_loss, b, kelly = res
    if kelly <= 0:
        return FractionDecision(method="KELLY", applied_fraction=0.0, base_amount=0.0, p=p,
                                avg_win=avg_win, avg_loss=avg_loss, b=b, kelly_full=kelly,
                                kelly_multiplier=p_.kelly_multiplier, decided_r=w + l,
                                reject=ReasonCode.KELLY_NO_EDGE)
    cap = p_.kelly_cap_effective_pct / 100.0       # never above the fixed fraction
    scaled = p_.kelly_multiplier * kelly
    clamps = ["fraction_cap"] if scaled > cap else []
    applied = min(scaled, cap)
    decision = FractionDecision(
        method="KELLY", applied_fraction=applied, base_amount=round_to_500(applied * capital),
        p=p, avg_win=avg_win, avg_loss=avg_loss, b=b, kelly_full=kelly,
        kelly_multiplier=p_.kelly_multiplier, decided_r=w + l, clamps=clamps)
    logger.info("[Kelly] %s: p=%.3f b=%.3f kelly=%.4f ×%.2f → %.4f (cap %.2f) base ₹%.0f",
                strategy, p, b, kelly, p_.kelly_multiplier, applied, cap, decision.base_amount)
    return decision


def sizing_decision(*, fraction: FractionDecision, regime_multiplier: float,
                    expected_entry_fill: float, lot_size: int, available_capital: float,
                    open_exposure: float,
                    initial_risk: Optional[float] = None) -> SizingDecision:
    """Phase two: lots from the fill and lot size. The regime multiplier is
    applied here exactly once, to the deployed amount. *initial_risk* is the
    premium points lost per contract if the premium stop is hit; with it the
    lots are also capped so that loss stays within max_risk_per_trade_pct of
    capital (clamp ``max_risk_per_trade``). The loss counted per lot is the
    stop distance plus that lot's round-trip brokerage and charges
    (fills.expected_round_trip_cost): a stopped trade pays both."""
    cfg = ctx().cfg
    clamps = list(fraction.clamps)
    trade_amount = round_to_500(fraction.base_amount * regime_multiplier)
    if regime_multiplier != 1.0:
        clamps.append("regime_multiplier")
    cost_per_lot = expected_entry_fill * lot_size
    lots, _ = calc_lots(trade_amount, expected_entry_fill, lot_size)
    binding: Optional[str] = None

    def _clamp(limit_lots: int, name: str) -> None:
        nonlocal lots, binding
        if limit_lots < lots:
            lots = max(limit_lots, 0)
            binding = name
            clamps.append(name)

    _clamp(cfg.max_lots_per_trade, "max_lots_per_trade")
    if initial_risk is not None and initial_risk > 0 and lot_size > 0:
        risk_budget = params().max_risk_per_trade_pct / 100.0 * cfg.capital
        cost_per_lot_rt = fills.expected_round_trip_cost(expected_entry_fill, lot_size, 1, params())
        _clamp(int(risk_budget // (initial_risk * lot_size + cost_per_lot_rt)), "max_risk_per_trade")
    if cost_per_lot > 0:
        _clamp(int(max(available_capital, 0.0) // cost_per_lot), "available_capital")
        headroom = cfg.open_exposure_cap_pct / 100.0 * cfg.capital - open_exposure
        _clamp(int(max(headroom, 0.0) // cost_per_lot), "open_exposure_cap")

    capital_used = round(lots * cost_per_lot, 2)
    reject: Optional[ReasonCode] = None
    if lots < 1:
        if cost_per_lot > trade_amount:
            reject = ReasonCode.LOT_UNAFFORDABLE
        elif binding in ("available_capital", "open_exposure_cap"):
            reject = ReasonCode.INSUFFICIENT_CAPITAL
        else:
            reject = ReasonCode.SIZED_TO_ZERO
    return SizingDecision(
        fraction=fraction, regime_multiplier=regime_multiplier,
        expected_entry_fill=expected_entry_fill, lot_size=lot_size, trade_amount=trade_amount,
        lots=lots, capital_used=capital_used, clamps=clamps, binding_clamp=binding, reject=reject)


# ── phase one: checks 0-11b ──────────────────────────────────────────────────

async def _group_trades_today(db, run_id: str, underlying: str, group) -> Optional[str]:
    """The first strategy of *group* with a position entered today, else None."""
    from app.services.trading.session_times import trading_day_bounds_utc
    start, end = trading_day_bounds_utc(trading_day())
    row = (await db.execute(select(PaperPosition.strategy_name).where(
        PaperPosition.run_id == run_id, PaperPosition.underlying == underlying,
        PaperPosition.strategy_name.in_(list(group)),
        PaperPosition.entry_time >= start, PaperPosition.entry_time < end,
    ).limit(1))).scalar_one_or_none()
    return row


async def evaluate_pre_selection(db, signal, regime_state) -> GateDecision:
    try:
        return await _pre_selection(db, signal, regime_state)
    except Exception as exc:                     # fail closed (§20.3)
        logger.error("[RiskGate] risk state unreadable — entry refused: %s", exc, exc_info=True)
        return _no(Stage.RISK_GATE, ReasonCode.CONFIG_INVALID, "risk state unreadable",
                   error=f"{type(exc).__name__}: {exc}")


async def _pre_selection(db, signal, regime_state) -> GateDecision:
    from app.services.trading.mode_manager import mode_manager
    from app.services.trading.strategy_orchestrator import strategy_orchestrator

    rc = ctx()
    p_ = params()
    cfg = rc.cfg
    day = trading_day()

    # 0. R:R invariant on the signal's own percentages — pure arithmetic
    passes, rr = fills.rr_ok(signal.sl_pct, signal.target_pct, p_.min_rr)
    if not passes:
        return _no(Stage.SIGNAL_GATE, ReasonCode.RR_INSUFFICIENT,
                   f"R:R {rr:.3f} < minimum {p_.min_rr} "
                   f"(sl_pct={signal.sl_pct}, target_pct={signal.target_pct})",
                   rr=rr, sl_pct=signal.sl_pct, target_pct=signal.target_pct, min_rr=p_.min_rr)

    # 1. paper mode
    if cfg.mode != "PAPER":
        logger.error("[RiskGate] ctx().cfg.mode=%r — refusing to trade", cfg.mode)
        return _no(Stage.RISK_GATE, ReasonCode.NOT_PAPER_MODE,
                   f"mode is {cfg.mode!r}, expected 'PAPER'", mode=cfg.mode)

    # 2. kill switch
    halted, why = await mode_manager.kill_switch_state()
    if halted:
        return _no(Stage.RISK_GATE, ReasonCode.KILL_SWITCH, f"Kill switch active: {why}")

    # 2b. entry blackout windows (lunch chop)
    now_t = _ist_time()
    for start, end in p_.entry_blackout_windows:
        sh, sm = (int(x) for x in start.split(":")[:2])
        eh, em = (int(x) for x in end.split(":")[:2])
        if (sh, sm) <= (now_t.hour, now_t.minute) < (eh, em):
            return _no(Stage.RISK_GATE, ReasonCode.AFTER_ENTRY_CUTOFF,
                       f"Entry blackout {start}-{end} IST", blackout=f"{start}-{end}")

    # 3. entry cutoff
    if is_after_entry_cutoff(_ist_time(), cfg.square_off_time, cfg.entry_cutoff_buffer_min):
        return _no(Stage.RISK_GATE, ReasonCode.AFTER_ENTRY_CUTOFF,
                   f"Entry cutoff reached (square-off {cfg.square_off_time})",
                   square_off=cfg.square_off_time, buffer_min=cfg.entry_cutoff_buffer_min)

    # 4. daily loss, realised + MTM, ratcheted
    loss = await risk_state.daily_loss_figure(db)
    if loss.breached:
        return _no(Stage.RISK_GATE, ReasonCode.DAILY_LOSS_CAP,
                   f"Daily loss cap breached ({loss.breach_source}): realised "
                   f"₹{loss.realised:.0f} + unrealised ₹{loss.unrealised:.0f} = "
                   f"₹{loss.combined:.0f}",
                   realised=loss.realised, unrealised=loss.unrealised, combined=loss.combined,
                   breach_source=loss.breach_source, mtm_degraded=loss.mtm_degraded,
                   threshold_abs=loss.threshold_abs, threshold_pct=loss.threshold_pct)

    # 5-7. streak halts and the post-loss cooldown (derived from closed trades)
    streak = await risk_state.streak_state(db, rc.run_id, day)
    strategy_orchestrator.set_daily_halts(streak.halted_strategies)
    await risk_state.engage_streak_halts(db, rc.run_id, day, streak)
    if streak.global_halt:
        return _no(Stage.RISK_GATE, ReasonCode.LOSING_STREAK_HALT,
                   f"{streak.consecutive_losses} consecutive losses — trading halted for the day",
                   consecutive_losses=streak.consecutive_losses,
                   position_ids=streak.triggering_position_ids)
    for group in p_.exclusive_strategy_groups:
        if signal.strategy_name in group:
            taken = await _group_trades_today(db, rc.run_id, signal.underlying, group)
            if taken:
                return _no(Stage.RISK_GATE, ReasonCode.STRATEGY_DAILY_HALT,
                           f"one trade a day across {'/'.join(group)}: {taken} already traded today",
                           strategy=signal.strategy_name, group=list(group), taken=taken)
    if signal.strategy_name in streak.halted_strategies:
        return _no(Stage.RISK_GATE, ReasonCode.STRATEGY_DAILY_HALT,
                   f"{signal.strategy_name} disabled for the day after "
                   f"{streak.per_strategy_losses.get(signal.strategy_name)} consecutive losses",
                   strategy=signal.strategy_name,
                   losses=streak.per_strategy_losses.get(signal.strategy_name))
    now = rc.clock.now_utc()
    if streak.cooldown_until is not None and now < streak.cooldown_until:
        return _no(Stage.RISK_GATE, ReasonCode.POST_LOSS_COOLDOWN,
                   f"post-loss cooldown until {streak.cooldown_until.isoformat()}",
                   cooldown_until=streak.cooldown_until.isoformat(),
                   last_loss_exit_at=streak.last_loss_exit_at.isoformat())

    # 8. derived daily trade count
    count = await risk_state.derived_trade_count(db, rc.run_id, day)
    if count >= cfg.max_trades_per_day:
        return _no(Stage.RISK_GATE, ReasonCode.DAILY_TRADE_CAP,
                   f"Daily trade limit ({cfg.max_trades_per_day}) reached.",
                   derived_count=count, max_trades_per_day=cfg.max_trades_per_day)

    # 9. open positions
    open_n = await risk_state.open_position_count(db, rc.run_id)
    if open_n >= cfg.max_open_positions:
        return _no(Stage.RISK_GATE, ReasonCode.MAX_OPEN_POSITIONS,
                   f"Max open positions ({cfg.max_open_positions}) reached. "
                   f"Wait for a position to close.",
                   open_positions=open_n, max_open_positions=cfg.max_open_positions)

    # 9b. one bet per direction
    side = "CE" if signal.direction.value == "BUY" else "PE"
    same_side = await risk_state.open_direction_count(db, rc.run_id, signal.underlying, side)
    if same_side >= p_.max_open_per_direction:
        return _no(Stage.RISK_GATE, ReasonCode.MAX_OPEN_POSITIONS,
                   f"Already {same_side} open {side} position(s) on {signal.underlying} "
                   f"(max {p_.max_open_per_direction} per direction)",
                   direction=side, open_same_direction=same_side,
                   max_open_per_direction=p_.max_open_per_direction)

    # 10. aggregate open exposure
    used = await risk_state.open_capital_used(db, rc.run_id)
    exposure_cap = cfg.open_exposure_cap_pct / 100.0 * cfg.capital
    if used >= exposure_cap:
        return _no(Stage.RISK_GATE, ReasonCode.EXPOSURE_CAP,
                   f"Open exposure ₹{used:.0f} ≥ cap ₹{exposure_cap:.0f}",
                   open_exposure=used, exposure_cap=exposure_cap)

    # 11. regime multiplier (fail closed — never restores full size)
    try:
        multiplier = strategy_orchestrator.get_position_multiplier(signal.underlying)
    except Exception as exc:
        logger.error("[RiskGate] regime multiplier lookup failed — entry refused: %s", exc,
                     exc_info=True)
        await no_trade.record_degradation(
            rc.run_id, ReasonCode.REGIME_MULTIPLIER_ERROR, underlying=signal.underlying,
            reason=f"multiplier lookup failed: {type(exc).__name__}: {exc}")
        return _no(Stage.RISK_GATE, ReasonCode.REGIME_UNAVAILABLE,
                   "regime position multiplier unavailable", error=str(exc))
    if multiplier == 0.0:
        regime = getattr(regime_state, "regime", None)
        return _no(Stage.RISK_GATE, ReasonCode.REGIME_MULTIPLIER_ZERO,
                   f"Position_Size_Multiplier=0.0 for regime {regime}", regime=regime)

    # 11b. the candidate-independent sizing fraction
    fraction = await fraction_decision(db, signal.strategy_name)
    if fraction.reject is not None:
        return GateDecision(
            allowed=False, stage=Stage.SIZING, code=fraction.reject,
            reason=f"Kelly shows no edge for {signal.strategy_name} "
                   f"(p={fraction.p}, b={fraction.b}, kelly={fraction.kelly_full})",
            context=no_trade.as_context(**jsonable(fraction)),
            regime_multiplier=multiplier, fraction=fraction)
    return _ok(regime_multiplier=multiplier, fraction=fraction)


# ── phase two: check 12 ──────────────────────────────────────────────────────

async def evaluate_sizing(db, signal, candidate, *, fraction: FractionDecision,
                          regime_multiplier: float) -> GateDecision:
    p_ = params()
    rc = ctx()
    try:
        used = await risk_state.open_capital_used(db, rc.run_id)
    except Exception as exc:                     # fail closed
        logger.error("[RiskGate] sizing could not read capital in use: %s", exc, exc_info=True)
        return _no(Stage.SIZING, ReasonCode.SIZED_TO_ZERO, "capital in use unreadable",
                   error=f"{type(exc).__name__}: {exc}")

    age = getattr(candidate, "tick_age_s", None)
    fill, fill_source = fills.entry_fill(
        candidate.ltp, getattr(candidate, "bid", 0), getattr(candidate, "ask", 0), age, p_)

    # initial risk must be positive (R6.5), checked before any capital is touched
    eff_sl = fills.plan_sl_pct(fill, signal.sl_pct, getattr(signal, "exit_plan", None),
                               getattr(candidate, "delta_est", None), p_)
    sl, tgt, rr, stretched = fills.entry_levels(fill, eff_sl, signal.target_pct, p_.min_rr)
    initial_risk = round(fill - sl, 2)
    if signal.sl_pct <= 0 or initial_risk <= 0:
        return _no(Stage.SIZING, ReasonCode.INITIAL_RISK_NON_POSITIVE,
                   f"Initial risk {initial_risk:.2f} is not positive "
                   f"(actual_entry={fill} sl={sl})", actual_entry=fill, sl=sl)

    sizing = sizing_decision(
        fraction=fraction, regime_multiplier=regime_multiplier, expected_entry_fill=fill,
        lot_size=candidate.lot_size, available_capital=rc.cfg.capital - used,
        open_exposure=used, initial_risk=initial_risk)
    ctx_dict = dict(open_capital_used=used, binding_clamp=sizing.binding_clamp,
                    trade_amount=sizing.trade_amount, lots=sizing.lots,
                    initial_risk_per_lot=round(initial_risk * candidate.lot_size, 2),
                    max_risk_rupees=round(p_.max_risk_per_trade_pct / 100.0 * rc.cfg.capital, 2),
                    expected_entry_fill=fill, fill_source=fill_source,
                    cost_per_lot=round(fill * candidate.lot_size, 2))
    if sizing.reject is not None:
        cost = ctx_dict["cost_per_lot"]
        return GateDecision(
            allowed=False, stage=Stage.SIZING, code=sizing.reject,
            reason=(f"{sizing.reject.value}: 1 lot {getattr(candidate, 'symbol', '')} = "
                    f"₹{cost:.0f} ({fill:.2f} × {candidate.lot_size}); budget "
                    f"₹{sizing.trade_amount:.0f}, free ₹{rc.cfg.capital - used:.0f}"),
            context=no_trade.as_context(**ctx_dict), sizing=sizing,
            regime_multiplier=regime_multiplier, fraction=fraction)

    # costs must not eat more than max_cost_fraction_of_edge of the gross edge at target
    qty = sizing.lots * candidate.lot_size
    cost = fills.expected_round_trip_cost(fill, qty, sizing.lots, p_)
    edge = (tgt - fill) * qty
    if edge <= 0 or cost > p_.max_cost_fraction_of_edge * edge:
        return GateDecision(
            allowed=False, stage=Stage.SIZING, code=ReasonCode.COSTS_EXCEED_EDGE,
            reason=f"round-trip cost ₹{cost:.2f} exceeds "
                   f"{p_.max_cost_fraction_of_edge:.0%} of edge ₹{edge:.2f}",
            context=no_trade.as_context(cost=cost, edge=edge, **ctx_dict), sizing=sizing,
            regime_multiplier=regime_multiplier, fraction=fraction)
    return _ok(stage=Stage.SIZING, sizing=sizing, regime_multiplier=regime_multiplier,
               fraction=fraction, context=no_trade.as_context(**ctx_dict))


# ── entry section: checks 13a, 13b under the advisory lock ───────────────────

def _lock_key(run_id: str, day: date) -> int:
    digest = hashlib.sha256(f"{run_id}|{day.isoformat()}".encode()).digest()
    return int.from_bytes(digest[:8], "big", signed=True)


def _is_postgres(db) -> bool:
    try:
        return db.get_bind().dialect.name == "postgresql"
    except Exception:
        return False


async def enter(db, *, run_id: str, trading_day: date, capital_used: float,
                advisory_used: float, insert: Callable[[Any], Awaitable[Any]],
                duplicate_token: Optional[str] = None,
                duplicate_symbol: Optional[str] = None,
                direction_key: Optional[tuple[str, str]] = None,
                lock_timeout_s: float = LOCK_TIMEOUT_S) -> GateDecision:
    """One transaction: lock → 13a (derived count) → 13b (derived capital) →
    insert → commit. Every early return happens before the insert, with nothing
    written and nothing held. An exception in the insert or commit rolls back
    (releasing the lock) and propagates; no position row exists then, so
    ``open_capital_used`` is unchanged."""
    cfg = ctx().cfg
    sqlite_lock: Optional[asyncio.Lock] = None
    started = asyncio.get_running_loop().time()
    try:
        if _is_postgres(db):
            await db.execute(text(f"SET LOCAL lock_timeout = '{int(lock_timeout_s * 1000)}ms'"))
            try:
                await db.execute(select(
                    func.pg_advisory_xact_lock(_lock_key(run_id, trading_day))))
            except DBAPIError as exc:            # 55P03 lock_not_available
                await db.rollback()
                return _lock_timeout(started, exc)
        else:
            key = (run_id, trading_day)
            sqlite_lock = _sqlite_locks.setdefault(key, asyncio.Lock())
            try:
                await asyncio.wait_for(sqlite_lock.acquire(), timeout=lock_timeout_s)
            except asyncio.TimeoutError:
                sqlite_lock = None
                return _lock_timeout(started, None)

        # Only a named contract can be a duplicate: with neither given, the
        # query used to match ANY open position and refuse every entry.
        duplicate_exists = None
        if duplicate_token is not None or duplicate_symbol:
            duplicate_query = select(PaperPosition.id).where(
                PaperPosition.run_id == run_id,
                PaperPosition.is_open.is_(True),
            )
            if duplicate_token is not None:
                duplicate_query = duplicate_query.where(PaperPosition.token == duplicate_token)
            else:
                duplicate_query = duplicate_query.where(PaperPosition.symbol == duplicate_symbol)
            duplicate_exists = (await db.execute(duplicate_query.limit(1))).scalar_one_or_none()
        if duplicate_exists is not None:
            await db.rollback()
            return _no(
                Stage.ENTRY, ReasonCode.DUPLICATE_OPEN_POSITION,
                f"Open position already exists for {duplicate_symbol or duplicate_token}; duplicate entry blocked.",
                duplicate_position_id=duplicate_exists,
            )

        if direction_key is not None:                                                   # 9b again,
            und, side = direction_key                                                   # under the lock
            same_side = await risk_state.open_direction_count(db, run_id, und, side)
            if same_side >= params().max_open_per_direction:
                await db.rollback()
                return _no(Stage.ENTRY, ReasonCode.MAX_OPEN_POSITIONS,
                           f"Already {same_side} open {side} position(s) on {und} "
                           f"(max {params().max_open_per_direction} per direction) while "
                           f"this signal was in selection",
                           direction=side, open_same_direction=same_side)

        count = await risk_state.derived_trade_count(db, run_id, trading_day)          # 13a
        if count >= cfg.max_trades_per_day:
            await db.rollback()
            return _no(Stage.RISK_GATE, ReasonCode.DAILY_TRADE_CAP,
                       f"Daily trade limit ({cfg.max_trades_per_day}) reached while this "
                       f"signal was in selection",
                       derived_count=count, max_trades_per_day=cfg.max_trades_per_day)
        locked_used = await risk_state.open_capital_used(db, run_id)                   # 13b
        if locked_used + capital_used > cfg.capital:
            await db.rollback()
            return _no(Stage.ENTRY, ReasonCode.CAPITAL_RACE_LOST,
                       f"capital in use ₹{locked_used:.0f} + ₹{capital_used:.0f} exceeds "
                       f"₹{cfg.capital:.0f}",
                       advisory_used=advisory_used, locked_used=locked_used,
                       amount=capital_used, capital=cfg.capital)
        result = await insert(db)
        await db.commit()                         # releases the advisory lock
        return GateDecision(allowed=True, stage=Stage.ENTRY, code=None, reason="", result=result)
    except BaseException:
        try:
            await db.rollback()
        except Exception:                         # the original error is the one that matters
            pass
        raise
    finally:
        if sqlite_lock is not None and sqlite_lock.locked():
            sqlite_lock.release()


def _lock_timeout(started: float, exc: Optional[BaseException]) -> GateDecision:
    waited = asyncio.get_running_loop().time() - started
    logger.warning("[RiskGate] trade-cap lock not acquired within %.1fs (waited %.2fs): %s",
                   LOCK_TIMEOUT_S, waited, exc)
    return _no(Stage.ENTRY, ReasonCode.DAILY_TRADE_CAP_RACE,
               "trade-cap lock not acquired in time", waited_s=round(waited, 3))
