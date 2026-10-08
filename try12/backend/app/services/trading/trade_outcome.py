"""
TradeOutcome (design §7): exactly one per position, built at the final close
from the quantity-weighted exit across the partial and final legs, and handed
to every aggregate writer so they all see the same numbers.

Two classifications, never mixed:
  * net P&L (params.flat_epsilon, ₹1): is_win / is_loss / is_flat — win rate,
    expectancy_inr, flat_trades, streaks;
  * R sign (params.r_flat_epsilon, 0.01R) over R-valid trades only
    (r_source 'ENTRY', initial_risk > 0, r_multiple set): r_bucket — every
    R bucket and the Kelly inputs.

``accumulate`` maintains the exact sums shared by strategy_perf,
regime_performance and daily_pnl_summary, and derives the means from them,
using the same formulas as reconcile.py so an incremental row and a rebuild
compare equal within reconcile's tolerances.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Optional


@dataclass(frozen=True)
class TradeOutcome:
    position_id: int
    run_id: str
    trading_day: date
    strategy_name: str
    underlying: str
    entry_regime: Optional[str]
    mode: Any
    net_pnl: float
    gross_pnl: float
    brokerage: float
    statutory_charges: float
    r_multiple: Optional[float]       # None when the trade is not R-valid
    initial_risk: Optional[float]
    exit_value_weighted: float
    hold_minutes: Optional[float]
    lots: int                         # original lots (partial + final are one result)
    exit_time: datetime
    is_win: bool                      # net_pnl >  flat_epsilon
    is_loss: bool                     # net_pnl < -flat_epsilon
    is_flat: bool
    r_bucket: Optional[str]           # R_WIN / R_LOSS / R_FLAT; None when r_multiple is None


def build_outcome(pos, *, run_id: str, trading_day: date, params) -> TradeOutcome:
    """From a closed position whose final values are set on *pos*."""
    net = float(pos.net_pnl or 0.0)
    r_valid = (pos.r_source == "ENTRY" and pos.initial_risk is not None
               and pos.initial_risk > 0 and pos.r_multiple is not None)
    r = float(pos.r_multiple) if r_valid else None
    bucket = None
    if r is not None:
        eps = params.r_flat_epsilon
        bucket = "R_WIN" if r > eps else "R_LOSS" if r < -eps else "R_FLAT"
    flat_eps = params.flat_epsilon
    return TradeOutcome(
        position_id=pos.id, run_id=run_id, trading_day=trading_day,
        strategy_name=pos.strategy_name or "UNKNOWN", underlying=pos.underlying,
        entry_regime=pos.entry_regime, mode=pos.mode,
        net_pnl=net, gross_pnl=float(pos.gross_pnl or 0.0),
        brokerage=float(pos.brokerage or 0.0),
        statutory_charges=float(pos.statutory_charges or 0.0),
        r_multiple=r, initial_risk=pos.initial_risk,
        exit_value_weighted=float(pos.exit_value_weighted or 0.0),
        hold_minutes=pos.hold_minutes, lots=int(pos.original_lots or pos.lots or 0),
        exit_time=pos.exit_time,
        is_win=net > flat_eps, is_loss=net < -flat_eps,
        is_flat=-flat_eps <= net <= flat_eps, r_bucket=bucket,
    )


def accumulate(row, o: TradeOutcome) -> None:
    """Add *o* to the sum columns every aggregate table shares, then re-derive
    win_rate, expectancy_r and expectancy_inr from the sums."""
    row.total_trades = (row.total_trades or 0) + 1
    row.winning_trades = (row.winning_trades or 0) + int(o.is_win)
    row.losing_trades = (row.losing_trades or 0) + int(o.is_loss)
    row.flat_trades = (row.flat_trades or 0) + int(o.is_flat)
    row.gross_profit = float(row.gross_profit or 0.0) + max(o.net_pnl, 0.0)
    row.gross_loss = float(row.gross_loss or 0.0) + max(-o.net_pnl, 0.0)
    row.sum_r = float(row.sum_r or 0.0)
    row.sum_r_win = float(row.sum_r_win or 0.0)
    row.sum_r_loss = float(row.sum_r_loss or 0.0)
    row.r_sample_size = row.r_sample_size or 0
    if o.r_multiple is not None:
        row.sum_r += o.r_multiple
        row.r_sample_size += 1
        if o.r_bucket == "R_WIN":
            row.sum_r_win += o.r_multiple
        elif o.r_bucket == "R_LOSS":
            row.sum_r_loss += o.r_multiple
    row.win_rate = row.winning_trades / row.total_trades * 100.0
    row.expectancy_r = (row.sum_r / row.r_sample_size) if row.r_sample_size else None
    # gross_profit − gross_loss is exactly the sum of net P&L
    row.expectancy_inr = (row.gross_profit - row.gross_loss) / row.total_trades
