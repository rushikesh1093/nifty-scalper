"""
Per-position exit plans (spot-based exits). Pure, no I/O.

A strategy attaches an exit plan to its signal; paper_engine stores it on the
position (``paper_positions.exit_plan``) and evaluates it every monitor pass,
in place of the generic premium ladder / partial / timeout. The premium stop
(``sl_pct`` of the fill) stays live alongside it — "whichever hits first".

Levels are on the UNDERLYING spot. ``side`` is the spot direction of the
trade: "BUY" for a CE (profits when spot rises), "SELL" for a PE.

Fields
------
spot_entry          spot at the signal (entry is the next bar's open ≈ this close)
spot_stop           exit when spot trades at/through it (moves to entry on T1 / BE)
spot_target         full exit when spot reaches it (ORB)
spot_t1             book half and move the stop to entry (pullback)
breakeven_trigger   move the stop to entry, nothing booked (ORB at 50% of target)
premium_target_pts  full exit when the option gains this many rupees (Supertrend)
time_stop_minutes   full exit after this hold time, whatever the price
t1_done / breakeven_done   one-shot flags, written back by paper_engine

Candle-close exits (EMA 9 trail, VWAP hard exit, Supertrend flip) need bars,
so they live on the strategy (``BaseStrategy.candle_exit``) and are run by the
strategy runner on every candle close.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, fields
from typing import Optional

# Exit reasons (paper_positions.exit_reason is String(20); "_STALE" may be appended)
SPOT_SL         = "SPOT_SL"
TARGET          = "TARGET"
TIME_EXIT       = "TIME_EXIT"
VWAP_EXIT       = "VWAP_EXIT"
EMA_TRAIL       = "EMA9_TRAIL"
ST_FLIP         = "ST_FLIP"
OPPOSITE_SIGNAL = "OPPOSITE_SIGNAL"

# One-shot (non-closing) actions
T1_PARTIAL      = "T1_PARTIAL"
BREAKEVEN       = "BREAKEVEN"

# Reasons that count as "the trade was stopped out" (ORB reverse-trade rule)
STOP_REASONS = frozenset({"SL", "SL_STALE", SPOT_SL, SPOT_SL + "_STALE"})


@dataclass
class ExitPlan:
    strategy:            str
    side:                str                    # "BUY" (CE) / "SELL" (PE)
    spot_entry:          float
    spot_stop:           float
    spot_target:         Optional[float] = None
    spot_t1:             Optional[float] = None
    breakeven_trigger:   Optional[float] = None
    premium_target_pts:  Optional[float] = None
    time_stop_minutes:   Optional[float] = None
    t1_done:             bool = False
    breakeven_done:      bool = False

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: Optional[dict]) -> Optional["ExitPlan"]:
        if not d:
            return None
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in d.items() if k in known})

    @property
    def sign(self) -> int:
        return 1 if self.side == "BUY" else -1

    def validate(self) -> list[str]:
        """Violations; empty means usable. The stop must be on the losing side
        of entry and every target on the winning side."""
        out: list[str] = []
        s = self.sign
        if self.side not in ("BUY", "SELL"):
            out.append(f"side={self.side!r} must be BUY or SELL")
        if not (s * (self.spot_entry - self.spot_stop) > 0):
            out.append(f"spot_stop={self.spot_stop} is not beyond entry={self.spot_entry} "
                       f"against a {self.side}")
        for name in ("spot_target", "spot_t1", "breakeven_trigger"):
            lvl = getattr(self, name)
            if lvl is not None and not (s * (lvl - self.spot_entry) > 0):
                out.append(f"{name}={lvl} is not beyond entry={self.spot_entry} "
                           f"in favour of a {self.side}")
        if self.premium_target_pts is not None and self.premium_target_pts <= 0:
            out.append(f"premium_target_pts={self.premium_target_pts} must be > 0")
        if self.time_stop_minutes is not None and self.time_stop_minutes <= 0:
            out.append(f"time_stop_minutes={self.time_stop_minutes} must be > 0")
        return out


def reached(side: str, price: float, level: Optional[float]) -> bool:
    """Price is at/through *level* in the trade's favour."""
    if level is None:
        return False
    return price >= level if side == "BUY" else price <= level


def breached(side: str, price: float, level: Optional[float]) -> bool:
    """Price is at/through *level* against the trade."""
    if level is None:
        return False
    return price <= level if side == "BUY" else price >= level


def tick_action(plan: ExitPlan, spot: Optional[float],
                hold_minutes: Optional[float]) -> Optional[str]:
    """The first spot/time action due on this tick, in priority order:
    SPOT_SL → TARGET (spot) → T1_PARTIAL → BREAKEVEN → TIME_EXIT.
    A missing spot price skips the spot checks (the premium stop still runs)."""
    if spot is not None and spot > 0:
        if breached(plan.side, spot, plan.spot_stop):
            return SPOT_SL
        if reached(plan.side, spot, plan.spot_target):
            return TARGET
        if not plan.t1_done and reached(plan.side, spot, plan.spot_t1):
            return T1_PARTIAL
        if not plan.breakeven_done and reached(plan.side, spot, plan.breakeven_trigger):
            return BREAKEVEN
    if (plan.time_stop_minutes is not None and hold_minutes is not None
            and hold_minutes >= plan.time_stop_minutes):
        return TIME_EXIT
    return None


def move_stop_to_entry(plan: ExitPlan) -> ExitPlan:
    """Stop to the spot entry, never loosening it."""
    if breached(plan.side, plan.spot_entry, plan.spot_stop):
        return plan          # stop is already past entry in our favour
    plan.spot_stop = plan.spot_entry
    return plan


def tighten_stop(plan: ExitPlan, level: Optional[float]) -> bool:
    """Trail the spot stop to *level* only if that tightens it. True if moved."""
    if level is None:
        return False
    tighter = level > plan.spot_stop if plan.side == "BUY" else level < plan.spot_stop
    if tighter:
        plan.spot_stop = level
    return tighter
