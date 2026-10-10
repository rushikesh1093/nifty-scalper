"""
Capital sufficiency check (blockers-plan item 6) — a startup WARNING, never a gate.

The affordability ceiling is the same arithmetic as ``risk_gate.fraction_decision``
(base = round_to_500(fixed_fraction_pct × capital)) and
``option_selector._build_candidates`` (ceiling = base × regime_multiplier / lot).
When that ceiling is below the premium-band floor or a typical ATM premium, the
system can only buy deep-OTM contracts. This module says so with the numbers;
it changes no default. Raising capital or fixed_fraction_pct is the operator's
decision.
"""
from __future__ import annotations

import math
from typing import Mapping, Union

from app.core.trading_params import TradingParams
from app.services.trading.risk_gate import round_to_500

# Operator reference for this warning only — an ASSUMPTION about a typical ATM
# premium, not a gate and not read by any trading decision.
REFERENCE_ATM_PREMIUM: dict[str, float] = {"NIFTY": 100.0, "BANKNIFTY": 300.0}

# Fallback when the instrument table has no lot size (measured in the
# acceptance-test report). The instrument table wins when it has a value.
REFERENCE_LOT_SIZE: dict[str, int] = {"NIFTY": 65, "BANKNIFTY": 30}


def premium_ceiling(capital: float, tp: TradingParams, lot_size: int,
                    regime_multiplier: float = 1.0) -> float:
    """Highest per-contract premium one lot can cost under fixed-fraction sizing."""
    base = round_to_500(tp.fixed_fraction_pct / 100.0 * capital)
    return base * regime_multiplier / lot_size


def capital_required(lots: int, premium: float, lot_size: int, fraction_pct: float) -> int:
    """Smallest multiple of ₹500 whose round_to_500 base buys *lots* lots at *premium*."""
    cost = lots * premium * lot_size
    base_needed = math.ceil(cost / 500.0 - 1e-9) * 500.0
    capital = math.ceil(base_needed * 100.0 / fraction_pct / 500.0 - 1e-9) * 500
    while round_to_500(fraction_pct / 100.0 * capital) < base_needed - 1e-9:
        capital += 500
    return int(capital)


def _rs(x: float) -> str:
    return f"₹{x:,.0f}" if float(x).is_integer() else f"₹{x:,.2f}"


def capital_sufficiency_warnings(capital: float, tp: TradingParams,
                                 lot_sizes: Mapping[str, int], *,
                                 lot_source: Union[str, Mapping[str, str]] = "instruments"
                                 ) -> list[str]:
    """One warning per underlying whose ceiling is below the premium-band floor or
    the reference ATM premium. Empty = capital fits."""
    out: list[str] = []
    frac = tp.fixed_fraction_pct
    base = round_to_500(frac / 100.0 * capital)
    for und, lot in lot_sizes.items():
        if not lot:
            continue
        source = lot_source.get(und, "instruments") if isinstance(lot_source, Mapping) \
            else lot_source
        ceiling = premium_ceiling(capital, tp, lot)
        lo, hi = tp.premium_band.get(und, tp.premium_band["NIFTY"])
        atm = REFERENCE_ATM_PREMIUM.get(und)
        lot_txt = f"{und} lot {lot} ({'reference fallback' if source == 'reference' else source})"
        head = (f"capital {_rs(capital)} × fixed_fraction_pct {frac:g}% → base {_rs(base)}; "
                f"{lot_txt} → premium ceiling {_rs(round(ceiling, 2))}/contract")
        if ceiling < lo:
            what = (f" < premium band floor {_rs(lo)}: no contract in the band "
                    f"({_rs(lo)}–{_rs(hi)}) can pass")
        elif atm is not None and ceiling < atm:
            what = (f" < ATM reference {_rs(atm)} (assumption): only "
                    f"{_rs(lo)}–{_rs(round(ceiling, 2))} contracts can pass "
                    f"(band {_rs(lo)}–{_rs(hi)})")
        else:
            continue
        need = ""
        if atm is not None:
            need = (f". 1 ATM lot needs ≈ {_rs(capital_required(1, atm, lot, frac))}, "
                    f"2 lots (partial exit) ≈ {_rs(capital_required(2, atm, lot, frac))}")
        out.append(head + what + need + ". No default changed: capital sizing is the "
                   "operator's decision.")
    return out
