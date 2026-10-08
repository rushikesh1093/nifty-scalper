"""
Trailing-stop ladder definitions and validation (design §8.1).

Pure, no I/O. Two distances, two names:

  REWARD = target - entry         distance to target. NOT the R of R-multiple.
  RISK   = entry - initial_stop   this IS R, per the requirements glossary.

Tiers are expressed as fractions of REWARD, so every trigger < 1.0 is
reachable before the target by construction.

There is NO breakeven-buffer constant: a stop below entry is a tier with a
negative ``lock_reward_frac``, and ``TradingParams.validate()`` rejects a
``breakeven_buffer_reward`` key outright.

This module currently holds the ladder's data and its validator, which
``TradingParams.validate()`` calls. ``next_stop`` and the paper_engine wiring
arrive with the exit-ladder change (plan item 4.2).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class TrailTier:
    trigger_reward_frac: float   # fires when (ltp - entry) / REWARD >= this
    lock_reward_frac:    float   # SL = entry + lock_reward_frac * REWARD
    label:               str


DEFAULT_TRAIL_TIERS: tuple[TrailTier, ...] = (
    TrailTier(0.90, 0.60, "LOCK_60"),
    TrailTier(0.75, 0.40, "LOCK_40"),
    TrailTier(0.50, 0.00, "BREAKEVEN"),
)
PARTIAL_EXIT_REWARD_FRAC = 0.75
PARTIAL_EXIT_FRACTION    = 0.50


def validate_ladder(
    tiers: Sequence[TrailTier],
    partial_reward_frac: float,
    partial_fraction: float,
    sl_pct: float,
    target_pct: float,
) -> list[str]:
    """Return a list of violations; empty means valid.

    Checks: every trigger < 1.0; triggers strictly decreasing in declaration
    order; locks strictly decreasing with trigger; lock < trigger; lock above
    the initial stop (lock > -(sl_pct / target_pct)); partial trigger < 1.0;
    partial fraction in (0, 1).
    """
    out: list[str] = []
    if target_pct <= 0 or sl_pct <= 0:
        out.append(f"sl_pct={sl_pct} and target_pct={target_pct} must both be > 0")
        floor = None
    else:
        floor = -(sl_pct / target_pct)

    for i, t in enumerate(tiers):
        if t.trigger_reward_frac >= 1.0:
            out.append(
                f"trail_tiers[{i}] ({t.label}): trigger_reward_frac={t.trigger_reward_frac} "
                f"must be < 1.0 (unreachable before target)"
            )
        if t.lock_reward_frac >= t.trigger_reward_frac:
            out.append(
                f"trail_tiers[{i}] ({t.label}): lock_reward_frac={t.lock_reward_frac} "
                f"must be < trigger_reward_frac={t.trigger_reward_frac}"
            )
        if floor is not None and t.lock_reward_frac <= floor:
            out.append(
                f"trail_tiers[{i}] ({t.label}): lock_reward_frac={t.lock_reward_frac} "
                f"must be > {floor:.4f} (stop at or below initial_stop_loss)"
            )
        if i > 0:
            prev = tiers[i - 1]
            if t.trigger_reward_frac >= prev.trigger_reward_frac:
                out.append(
                    f"trail_tiers[{i}] ({t.label}): triggers must be strictly decreasing "
                    f"({prev.trigger_reward_frac} then {t.trigger_reward_frac})"
                )
            if t.lock_reward_frac >= prev.lock_reward_frac:
                out.append(
                    f"trail_tiers[{i}] ({t.label}): locks must be strictly decreasing "
                    f"with trigger ({prev.lock_reward_frac} then {t.lock_reward_frac})"
                )

    if partial_reward_frac >= 1.0 or partial_reward_frac <= 0.0:
        out.append(f"partial_exit_reward_frac={partial_reward_frac} must be in (0, 1)")
    if not (0.0 < partial_fraction < 1.0):
        out.append(f"partial_exit_fraction={partial_fraction} must be in (0, 1)")
    return out
