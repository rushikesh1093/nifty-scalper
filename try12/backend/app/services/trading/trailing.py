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

``validate_ladder`` is called by ``TradingParams.validate()``; ``next_stop``
is called by paper_engine._check_position after the exits and the partial
(§8.2 evaluation order). There is no default-target fallback: a missing or
non-advancing target is a broken invariant and ``next_stop`` raises.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Sequence


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


def reward_progress(entry: float, target: Optional[float], ltp: float) -> float:
    """(ltp - entry) / REWARD. Raises ValueError unless target > entry."""
    if target is None or entry is None or not target > entry:
        raise ValueError(f"target must be > entry for the ladder (entry={entry}, target={target})")
    return (ltp - entry) / (target - entry)


def next_stop(entry: float, target: Optional[float], ltp: float, current_sl: Optional[float],
              tiers: Sequence[TrailTier] = DEFAULT_TRAIL_TIERS) -> Optional[tuple[float, str]]:
    """Highest-trigger-first. Returns (new_sl, tier_label) only when new_sl >
    current_sl — monotonicity is enforced here, in one place, not by the caller.
    `target` is REQUIRED and must be > entry; it raises ValueError otherwise."""
    progress = reward_progress(entry, target, ltp)
    reward = target - entry
    for tier in tiers:
        if progress >= tier.trigger_reward_frac:
            new_sl = round(entry + tier.lock_reward_frac * reward, 2)
            if current_sl is None or new_sl > current_sl:
                return new_sl, tier.label
            return None
    return None
