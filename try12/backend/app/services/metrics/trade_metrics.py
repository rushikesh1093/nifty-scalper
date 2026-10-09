"""
The only place trade metrics are computed (design §16.1, R15.6). The analytics
API, the replay validation report and the tests all call ``compute`` /
``compute_daily`` / ``group_by`` here.

Two classifications, never mixed (design §9.5, §24):
  * net P&L (``flat_epsilon``, ₹1): win / loss / flat — win rate, avg_win_inr,
    avg_loss_inr, expectancy_inr, the losing streak;
  * R sign (``r_flat_epsilon``, 0.01R) over R-valid trades only — avg_win_r,
    avg_loss_r, win_loss_ratio_r, the Kelly inputs.

Three denominators, each named where it is used (§24):
  * ``decided_r``     = R_WIN + R_LOSS trades   — every Kelly term
  * ``r_sample_size`` = R-valid trades           — expectancy_r, median_r, stdev_r
  * ``trades``        = every closed trade        — win_rate, expectancy_inr

A trade is R-valid only with ``r_source == 'ENTRY'``, ``initial_risk > 0`` and
``r_multiple`` set; anything else counts in ``r_unavailable`` and is excluded
from every R statistic (R15.7). Nothing is defaulted to zero; an empty set has
every statistic ``None`` and ``low_sample = True`` (§17.3).

``expectancy_r`` is GROSS of costs (§8.4); ``expectancy_inr`` and the mean
``r_multiple_net`` are the net figures.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass
from datetime import date, datetime, timezone
from typing import Any, Iterable, Optional

from app.services.metrics import stats
from app.services.trading.session_times import trading_day as ist_day, trading_day_bounds_utc

DIMENSIONS = ("strategy", "regime", "strategy_regime", "underlying", "day")

SHARPE_ASSUMPTION = (
    "Sharpe-like = mean(daily net P&L) / stdev(daily net P&L) × √252, risk-free rate "
    "assumed 0, days with no trades excluded. Computed on rupee P&L; the ratio is "
    "scale-invariant, so dividing both terms by capital would not change it."
)
SORTINO_ASSUMPTION = (
    "Sortino-like = mean(daily net P&L) / downside deviation × √252, where downside "
    "deviation is the root-mean-square of min(0, daily net P&L − target) over all included "
    "days (not only losing days), target return assumed 0, risk-free rate assumed 0, days "
    "with no trades excluded. Reported as null when no day falls below the target."
)


@dataclass(frozen=True)
class TradeFilter:
    run_id: str = "PAPER"
    date_from: Optional[date] = None
    date_to: Optional[date] = None
    underlying: Optional[str] = None
    strategy: Optional[str] = None
    regime: Optional[str] = None
    outcome: Optional[str] = None         # 'WIN' | 'LOSS' | 'FLAT'
    mode: str = "PAPER"


@dataclass(frozen=True)
class TradeRow:
    """One closed position, as every metric sees it (R15.1 fields)."""
    position_id: int
    run_id: str
    trading_day: date                     # IST day of the exit (daily_pnl_summary's day)
    underlying: str
    strategy_name: str
    entry_regime: Optional[str]
    direction: Optional[str]
    strike: Optional[float]
    option_type: Optional[str]
    expiry: Optional[date]
    dte_at_entry: Optional[int]
    lots: int
    quantity: int
    actual_entry: float
    initial_stop_loss: Optional[float]
    initial_risk: Optional[float]
    target: Optional[float]
    actual_exit: Optional[float]
    exit_reason: Optional[str]
    exit_value_weighted: Optional[float]
    gross_pnl: float
    brokerage: float
    statutory_charges: float
    net_pnl: float
    r_multiple: Optional[float]
    r_multiple_net: Optional[float]
    r_source: Optional[str]
    entry_time: Optional[datetime]
    exit_time: Optional[datetime]
    hold_minutes: Optional[float] = None
    entry_fill_source: Optional[str] = None
    exit_fill_source: Optional[str] = None
    sizing_method: Optional[str] = None
    target_stretched: bool = False
    partial_closed: bool = False
    mode: str = "PAPER"

    @property
    def r_valid(self) -> bool:
        return (self.r_source == "ENTRY" and self.initial_risk is not None
                and self.initial_risk > 0 and self.r_multiple is not None)


@dataclass(frozen=True)
class MetricSet:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    flats: int = 0
    win_rate: Optional[float] = None              # fraction of `trades`, 0..1
    avg_win_inr: Optional[float] = None
    avg_loss_inr: Optional[float] = None          # magnitude
    win_loss_ratio_inr: Optional[float] = None
    avg_win_r: Optional[float] = None             # R_WIN bucket
    avg_loss_r: Optional[float] = None            # magnitude of the R_LOSS bucket
    win_loss_ratio_r: Optional[float] = None      # the Kelly b
    r_flat_trades: int = 0
    expectancy_r: Optional[float] = None          # GROSS of costs
    expectancy_r_net: Optional[float] = None      # mean r_multiple_net
    expectancy_inr: Optional[float] = None        # net
    profit_factor: Optional[float] = None
    gross_profit: Optional[float] = None
    gross_loss: Optional[float] = None
    median_r: Optional[float] = None
    stdev_r: Optional[float] = None
    max_equity_drawdown_inr: Optional[float] = None
    max_equity_drawdown_pct: Optional[float] = None
    worst_trade_loss: Optional[float] = None
    longest_losing_streak: Optional[int] = None
    avg_hold_minutes: Optional[float] = None
    r_unavailable: int = 0
    decided_r: int = 0
    r_sample_size: int = 0
    low_sample: bool = True
    sample_size: int = 0


@dataclass(frozen=True)
class DailyMetricSet:
    mean_daily_pnl: Optional[float] = None
    stdev_daily_pnl: Optional[float] = None
    sharpe_like: Optional[float] = None
    sortino_like: Optional[float] = None
    trading_days: int = 0
    positive_days: int = 0
    assumptions: str = SHARPE_ASSUMPTION + " " + SORTINO_ASSUMPTION
    sharpe_assumption: str = SHARPE_ASSUMPTION
    sortino_assumption: str = SORTINO_ASSUMPTION


def as_dict(obj) -> dict:
    return dataclasses.asdict(obj)


# ── loading ──────────────────────────────────────────────────────────────────

def _v(x) -> Any:
    return getattr(x, "value", x)


def _aware(ts: Optional[datetime]) -> Optional[datetime]:
    if ts is None:
        return None
    return ts if ts.tzinfo is not None else ts.replace(tzinfo=timezone.utc)


def row_from_position(pos) -> TradeRow:
    exit_t, entry_t = _aware(pos.exit_time), _aware(pos.entry_time)
    sizing = pos.sizing_detail if isinstance(pos.sizing_detail, dict) else {}
    frac = sizing.get("fraction") if isinstance(sizing.get("fraction"), dict) else {}
    return TradeRow(
        position_id=pos.id, run_id=pos.run_id,
        trading_day=ist_day(exit_t or entry_t),
        underlying=pos.underlying, strategy_name=pos.strategy_name or "UNSPECIFIED",
        entry_regime=pos.entry_regime, direction=_v(pos.direction),
        strike=pos.strike, option_type=pos.option_type, expiry=pos.expiry,
        dte_at_entry=pos.dte_at_entry,
        lots=int(pos.original_lots or pos.lots or 0),
        quantity=int(pos.original_quantity or pos.quantity or 0),
        actual_entry=float(pos.actual_entry or 0.0),
        initial_stop_loss=pos.initial_stop_loss, initial_risk=pos.initial_risk,
        target=pos.target, actual_exit=pos.actual_exit, exit_reason=pos.exit_reason,
        exit_value_weighted=pos.exit_value_weighted,
        gross_pnl=float(pos.gross_pnl or 0.0), brokerage=float(pos.brokerage or 0.0),
        statutory_charges=float(pos.statutory_charges or 0.0),
        net_pnl=float(pos.net_pnl or 0.0),
        r_multiple=pos.r_multiple, r_multiple_net=pos.r_multiple_net, r_source=pos.r_source,
        entry_time=entry_t, exit_time=exit_t, hold_minutes=pos.hold_minutes,
        entry_fill_source=pos.entry_fill_source, exit_fill_source=pos.exit_fill_source,
        sizing_method=frac.get("method"), target_stretched=bool(pos.target_stretched),
        partial_closed=bool(pos.partial_closed), mode=str(_v(pos.mode)),
    )


def outcome_of(t: TradeRow, flat_epsilon: float) -> str:
    if t.net_pnl > flat_epsilon:
        return "WIN"
    if t.net_pnl < -flat_epsilon:
        return "LOSS"
    return "FLAT"


async def load_trades(db, f: TradeFilter, *, flat_epsilon: float = 1.0) -> list[TradeRow]:
    """Closed positions of ONE run (``f.run_id``), filtered, ordered by exit."""
    from sqlalchemy import select

    from app.db.models.paper_trade import PaperPosition, TradingMode

    q = select(PaperPosition).where(PaperPosition.run_id == f.run_id,
                                    PaperPosition.is_open == False)  # noqa: E712
    if f.mode:
        q = q.where(PaperPosition.mode == TradingMode(f.mode))
    if f.underlying:
        q = q.where(PaperPosition.underlying == f.underlying.upper())
    if f.strategy:
        q = q.where(PaperPosition.strategy_name == f.strategy)
    if f.regime:
        q = q.where(PaperPosition.entry_regime == f.regime)
    if f.date_from:
        q = q.where(PaperPosition.exit_time >= trading_day_bounds_utc(f.date_from)[0])
    if f.date_to:
        q = q.where(PaperPosition.exit_time < trading_day_bounds_utc(f.date_to)[1])
    q = q.order_by(PaperPosition.exit_time, PaperPosition.id)
    rows = [row_from_position(p) for p in (await db.execute(q)).scalars().all()]
    if f.outcome:
        rows = [t for t in rows if outcome_of(t, flat_epsilon) == f.outcome.upper()]
    return rows


# ── computation ──────────────────────────────────────────────────────────────

def _ordered(trades: Iterable[TradeRow]) -> list[TradeRow]:
    far = datetime.max.replace(tzinfo=timezone.utc)
    return sorted(trades, key=lambda t: (t.exit_time or far, t.position_id))


def _ratio(a: Optional[float], b: Optional[float]) -> Optional[float]:
    if a is None or b is None or b == 0:
        return None
    return a / b


def expectancy_r(trades: list[TradeRow]) -> float:
    """Statistic for the bootstrap: mean gross R over the R-valid trades (0.0 on none)."""
    rs = [t.r_multiple for t in trades if t.r_valid]
    return stats.mean(rs) if rs else 0.0


def expectancy_inr(trades: list[TradeRow]) -> float:
    """Statistic for the bootstrap: mean net ₹ per trade (0.0 on none)."""
    return stats.mean([t.net_pnl for t in trades]) if trades else 0.0


def compute(trades: list[TradeRow], capital: float, params) -> MetricSet:
    n = len(trades)
    if n == 0:
        return MetricSet()
    flat_eps, r_eps = params.flat_epsilon, params.r_flat_epsilon
    ordered = _ordered(trades)
    nets = [t.net_pnl for t in ordered]
    wins = [x for x in nets if x > flat_eps]
    losses = [x for x in nets if x < -flat_eps]
    flats = n - len(wins) - len(losses)

    r_rows = [t for t in trades if t.r_valid]
    rs = [float(t.r_multiple) for t in r_rows]
    r_win = [r for r in rs if r > r_eps]
    r_loss = [abs(r) for r in rs if r < -r_eps]
    r_net = [float(t.r_multiple_net) for t in r_rows if t.r_multiple_net is not None]

    gross_profit = sum(x for x in nets if x > 0)
    gross_loss = sum(-x for x in nets if x < 0)
    avg_win_inr = stats.mean(wins)
    avg_loss_inr = stats.mean([abs(x) for x in losses])
    avg_win_r = stats.mean(r_win)
    avg_loss_r = stats.mean(r_loss)
    dd = stats.equity_drawdown(nets)
    holds = [t.hold_minutes for t in trades if t.hold_minutes is not None]
    return MetricSet(
        trades=n, wins=len(wins), losses=len(losses), flats=flats,
        win_rate=len(wins) / n,
        avg_win_inr=avg_win_inr, avg_loss_inr=avg_loss_inr,
        win_loss_ratio_inr=_ratio(avg_win_inr, avg_loss_inr),
        avg_win_r=avg_win_r, avg_loss_r=avg_loss_r,
        win_loss_ratio_r=_ratio(avg_win_r, avg_loss_r),
        r_flat_trades=len(rs) - len(r_win) - len(r_loss),
        expectancy_r=stats.mean(rs), expectancy_r_net=stats.mean(r_net),
        expectancy_inr=sum(nets) / n,
        profit_factor=(gross_profit / gross_loss) if gross_profit > 0 and gross_loss > 0 else None,
        gross_profit=gross_profit, gross_loss=gross_loss,
        median_r=stats.median(rs), stdev_r=stats.stdev(rs),
        max_equity_drawdown_inr=dd,
        max_equity_drawdown_pct=(dd / capital * 100.0) if capital else None,
        worst_trade_loss=max((abs(x) for x in nets if x < 0), default=None),
        longest_losing_streak=stats.longest_losing_streak(nets, flat_eps),
        avg_hold_minutes=stats.mean(holds),
        r_unavailable=n - len(r_rows), decided_r=len(r_win) + len(r_loss),
        r_sample_size=len(r_rows),
        low_sample=n < params.min_display_sample, sample_size=n,
    )


def daily_pnl(trades: list[TradeRow]) -> dict[date, float]:
    out: dict[date, float] = {}
    for t in trades:
        out[t.trading_day] = out.get(t.trading_day, 0.0) + t.net_pnl
    return dict(sorted(out.items()))


def compute_daily(trades: list[TradeRow], capital: float) -> DailyMetricSet:
    daily = list(daily_pnl(trades).values())
    if not daily:
        return DailyMetricSet()
    return DailyMetricSet(
        mean_daily_pnl=stats.mean(daily), stdev_daily_pnl=stats.stdev(daily),
        sharpe_like=stats.sharpe_like(daily), sortino_like=stats.sortino_like(daily),
        trading_days=len(daily), positive_days=sum(1 for x in daily if x > 0),
    )


def _key(t: TradeRow, dimension: str) -> str:
    if dimension == "strategy":
        return t.strategy_name
    if dimension == "regime":
        return t.entry_regime or "UNRECORDED"
    if dimension == "strategy_regime":
        return f"{t.strategy_name}|{t.entry_regime or 'UNRECORDED'}"
    if dimension == "underlying":
        return t.underlying
    if dimension == "day":
        return t.trading_day.isoformat()
    raise ValueError(f"unknown dimension {dimension!r}; expected one of {DIMENSIONS}")


def group_by(trades: list[TradeRow], key: str, capital: float, params) -> dict[str, MetricSet]:
    groups: dict[str, list[TradeRow]] = {}
    for t in trades:
        groups.setdefault(_key(t, key), []).append(t)
    return {k: compute(v, capital, params) for k, v in sorted(groups.items())}


# ── determinism projection (design §14.5) ────────────────────────────────────

DETERMINISM_FIELDS = (
    "trading_day", "underlying", "strategy_name", "entry_regime", "direction",
    "strike", "option_type", "expiry", "dte_at_entry", "lots", "quantity",
    "actual_entry", "initial_stop_loss", "initial_risk", "target",
    "actual_exit", "exit_reason", "exit_value_weighted",
    "gross_pnl", "brokerage", "statutory_charges", "net_pnl",
    "r_multiple", "r_multiple_net",
)
DETERMINISM_ORDER = ("trading_day", "entry_time", "strategy_name", "strike")
METRIC_FIELDS = (
    "trades", "wins", "losses", "flats", "win_rate", "expectancy_r", "expectancy_inr",
    "profit_factor", "gross_profit", "gross_loss", "max_equity_drawdown_inr",
    "longest_losing_streak", "r_unavailable",
)


def determinism_hash(trades: list[TradeRow], metrics: MetricSet) -> str:
    """sha256 over an allowlisted projection: no run_id, surrogate id, created_at,
    code_version, wall-clock duration or sizing_detail can reach it."""
    import hashlib

    from app.runtime.serialize import canonical_json

    def _order(t: TradeRow):
        return tuple("" if getattr(t, k) is None else str(getattr(t, k))
                     for k in DETERMINISM_ORDER)

    payload = {
        "trades": [{k: getattr(t, k) for k in DETERMINISM_FIELDS}
                   for t in sorted(trades, key=_order)],
        "metrics": {k: getattr(metrics, k) for k in METRIC_FIELDS},
    }
    return hashlib.sha256(canonical_json(payload).encode("utf-8")).hexdigest()
