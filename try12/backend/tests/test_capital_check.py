"""
Item 6 — capital-sufficiency startup WARNING (blockers-plan item 6).

Informational only: at capital ₹15,000 × 10 % the NIFTY affordability ceiling is
₹23.08 per contract, so only deep-OTM tickets pass. Startup still succeeds and no
default changes; the capital decision is the operator's.
"""
import logging

from app.core.trading_params import TradingParams
from app.services.trading.capital_check import (
    REFERENCE_ATM_PREMIUM, REFERENCE_LOT_SIZE, capital_required,
    capital_sufficiency_warnings, premium_ceiling,
)
from tests.helpers import run, seed_config

LOTS = {"NIFTY": 65, "BANKNIFTY": 30}


def _for(warnings, und):
    return [w for w in warnings if f"; {und} lot" in w]


def test_ceiling_at_15000_is_23_08():
    assert round(premium_ceiling(15000, TradingParams(), 65), 2) == 23.08
    assert round(premium_ceiling(15000, TradingParams(), 30), 2) == 50.0
    assert round(premium_ceiling(15000, TradingParams(), 65, regime_multiplier=0.5), 2) == 11.54


def test_capital_required():
    assert capital_required(1, 100.0, 65, 10.0) == 65000
    assert capital_required(2, 100.0, 65, 10.0) == 130000
    assert capital_required(1, 100.0, 65, 20.0) == 32500
    # the base is floored to ₹500: ₹50 × 65 = 3,250 needs a ₹3,500 base
    assert capital_required(1, 50.0, 65, 10.0) == 35000


def test_15000_warns_with_the_numbers():
    w = capital_sufficiency_warnings(15000, TradingParams(), LOTS)
    nifty = _for(w, "NIFTY")
    assert len(nifty) == 1
    assert "23.08" in nifty[0] and "65,000" in nifty[0] and "130,000" in nifty[0]
    assert "15,000" in nifty[0] and "1,500" in nifty[0]
    assert len(_for(w, "BANKNIFTY")) == 1


def test_200000_has_no_nifty_warning():
    assert _for(capital_sufficiency_warnings(200000, TradingParams(), LOTS), "NIFTY") == []


def test_5000_is_below_the_band_floor():
    nifty = _for(capital_sufficiency_warnings(5000, TradingParams(), LOTS), "NIFTY")
    assert len(nifty) == 1 and "premium band floor ₹10" in nifty[0]


def test_reference_values_are_the_measured_ones():
    assert REFERENCE_LOT_SIZE == {"NIFTY": 65, "BANKNIFTY": 30}
    assert REFERENCE_ATM_PREMIUM == {"NIFTY": 100.0, "BANKNIFTY": 300.0}
    w = capital_sufficiency_warnings(15000, TradingParams(), LOTS, lot_source="reference")
    assert all("reference" in x for x in w)


def test_lifespan_warns_and_still_starts(sqlite_db, lifespan_spies, caplog):
    from app.main import app, lifespan
    run(seed_config(sqlite_db, capital=15000.0, max_lots_per_trade=2, max_daily_loss=500.0,
                    trade_amount=None))
    caplog.set_level(logging.WARNING)

    async def _enter():
        async with lifespan(app):
            return True

    assert run(_enter()) is True
    assert any(r.levelno == logging.WARNING and "23.08" in r.getMessage()
               for r in caplog.records)
    assert "runner.start" in lifespan_spies
