"""Fix 3: no forced 1-lot; unaffordable signals are SKIPPED with a clear reason.

Capital in use is DERIVED from open positions (design §9.2), so "only ₹2,000
free" is modelled by seeding an open position, not by writing a counter."""
from datetime import time as dtime

import pytest
from sqlalchemy import select

from app.db.models.paper_trade import PaperPosition, SignalStatus, StrategySignal
from app.db.models.risk import NoTradeDecision
from app.runtime.context import using
from app.services.trading.paper_engine import _calc_lots, paper_engine
from tests.helpers import (
    add_position, allow_regime, fake_candidate, pin_ist_time, run, seed_config, stub_select,
    make_signal, with_cfg,
)


@pytest.mark.parametrize("amount, price, lot, expected", [
    (6000, 40, 65, (2, 5200.0)),     # affordable: 2 lots
    (2600, 40, 65, (1, 2600.0)),     # exactly one lot
    (3000, 70, 65, (0, 0.0)),        # one lot (₹4,550) > budget → 0, not forced to 1
    (3000, 0, 65, (0, 0.0)),         # invalid price
    (3000, 40, 0, (0, 0.0)),         # invalid lot size
])
def test_calc_lots(amount, price, lot, expected):
    assert _calc_lots(amount, price, lot) == expected


@pytest.fixture
def wired(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """handle_signal over the real gate and a SQLite DB: ₹200,000 capital, a ₹20,000
    base budget (10%), regime multiplier whatever the regime rule gives."""
    run(seed_config(sqlite_db))
    pin_ist_time(monkeypatch, dtime(10, 30))
    allow_regime(monkeypatch)
    return sqlite_db


def _rows(Session, model):
    async def _all():
        async with Session() as db:
            return (await db.execute(select(model))).scalars().all()
    return run(_all())


def test_unaffordable_lot_is_skipped(wired, monkeypatch):
    stub_select(monkeypatch, fake_candidate(ltp=700.0, lot_size=65))   # ₹45,500 per lot
    assert run(paper_engine.handle_signal(make_signal())) is False
    sig = _rows(wired, StrategySignal)[-1]
    assert sig.status == SignalStatus.SKIPPED
    (decision,) = _rows(wired, NoTradeDecision)
    assert decision.code == "LOT_UNAFFORDABLE" and decision.stage == "SIZING"
    assert _rows(wired, PaperPosition) == []


def test_lot_above_available_capital_is_skipped(wired, runtime_ctx, monkeypatch):
    # ₹198,000 already deployed by an open position → only ₹2,000 free
    run(add_position(wired, trade_amount_used=198_000.0))
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))   # ₹2,605 per lot
    wide = with_cfg(runtime_ctx, open_exposure_cap_pct=100.0, max_open_positions=10)
    with using(wide):
        assert run(paper_engine.handle_signal(make_signal())) is False
    (decision,) = [d for d in _rows(wired, NoTradeDecision) if d.stage != "DEGRADATION"]
    assert decision.code == "INSUFFICIENT_CAPITAL"
    assert decision.context["binding_clamp"] == "available_capital"
    assert len(_rows(wired, PaperPosition)) == 1          # only the seeded one


def test_affordable_lot_opens_a_position_sized_on_the_fill(wired, monkeypatch):
    stub_select(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))   # ₹2,605 per lot
    assert run(paper_engine.handle_signal(make_signal())) is True
    (pos,) = _rows(wired, PaperPosition)
    assert pos.actual_entry == pytest.approx(40.08)                  # LTP + 0.2% fallback
    assert pos.trade_amount_used == pytest.approx(pos.lots * 40.08 * 65, abs=0.01)
    assert pos.lots == min(3, int(pos.sizing_detail["trade_amount"] // (40.08 * 65)))
