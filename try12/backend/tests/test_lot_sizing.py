"""Fix 3: no forced 1-lot; unaffordable signals are SKIPPED with a clear reason."""
import pytest

from app.db.models.paper_trade import SignalStatus
from app.services.trading import paper_engine as pe_mod
from app.services.trading.mode_manager import mode_manager
from app.services.trading.option_selector import option_selector
from app.services.trading.paper_engine import _calc_lots, paper_engine
from app.services.trading.strategy_orchestrator import strategy_orchestrator
from tests.helpers import fake_candidate, make_cfg, make_signal, run


@pytest.mark.parametrize("amount, price, lot, expected", [
    (6000, 40, 65, (2, 5200.0)),     # affordable: 2 lots
    (2600, 40, 65, (1, 2600.0)),     # exactly one lot
    (3000, 70, 65, (0, 0.0)),        # one lot (₹4,550) > budget → 0, not forced to 1
    (3000, 0, 65, (0, 0.0)),         # invalid price
    (3000, 40, 0, (0, 0.0)),         # invalid lot size
])
def test_calc_lots(amount, price, lot, expected):
    assert _calc_lots(amount, price, lot) == expected


class _Spy:
    def __init__(self, ret):
        self.ret = ret
        self.calls = []

    async def __call__(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        return self.ret


@pytest.fixture
def wired(monkeypatch, fake_redis):
    """handle_signal with every external dependency stubbed (no DB)."""
    cfg = make_cfg()
    saved = []

    async def _get_config():
        return cfg

    async def _save_signal(signal, status, selected=None, sl=None, tgt=None, rr=None,
                           reason="", db=None):
        saved.append((status, reason))
        return 1

    async def _no_regime(underlying):
        return None

    class _NoDB:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

    monkeypatch.setattr(pe_mod, "_ist_time", lambda: __import__("datetime").time(10, 30))
    monkeypatch.setattr(pe_mod, "AsyncSessionLocal", lambda: _NoDB())
    monkeypatch.setattr(mode_manager, "get_config", _get_config)
    monkeypatch.setattr(mode_manager, "pre_trade_check", _Spy((True, "")))
    monkeypatch.setattr(mode_manager, "kelly_trade_amount", _Spy(3000.0))
    reserve = _Spy(False)
    monkeypatch.setattr(mode_manager, "reserve_capital", reserve)
    monkeypatch.setattr(strategy_orchestrator, "get_current_regime", _no_regime)
    monkeypatch.setattr(strategy_orchestrator, "get_position_multiplier", lambda u: 1.0)
    monkeypatch.setattr(paper_engine, "_save_signal", _save_signal)
    return {"cfg": cfg, "saved": saved, "reserve": reserve, "redis": fake_redis}


def _select_returns(monkeypatch, cand):
    async def _select(**kwargs):
        return cand
    monkeypatch.setattr(option_selector, "select", _select)


def test_unaffordable_lot_is_skipped(wired, monkeypatch):
    _select_returns(monkeypatch, fake_candidate(ltp=70.0, lot_size=65))   # ₹4,550+ per lot
    assert run(paper_engine.handle_signal(make_signal())) is False
    status, reason = wired["saved"][-1]
    assert status == SignalStatus.SKIPPED
    assert "Lot unaffordable" in reason
    assert wired["reserve"].calls == []
    assert wired["cfg"].capital == 10000.0   # capital untouched


def test_lot_above_available_capital_is_skipped(wired, monkeypatch):
    wired["cfg"].used_capital = 8000.0      # only ₹2,000 free
    _select_returns(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))   # ₹2,605 per lot
    assert run(paper_engine.handle_signal(make_signal())) is False
    assert "Lot unaffordable" in wired["saved"][-1][1]
    assert wired["reserve"].calls == []


def test_affordable_lot_reaches_reserve(wired, monkeypatch):
    _select_returns(monkeypatch, fake_candidate(ltp=40.0, lot_size=65))   # ₹2,605 ≤ ₹3,000
    assert run(paper_engine.handle_signal(make_signal())) is False   # reserve spy returns False
    assert len(wired["reserve"].calls) == 1
    reserved = wired["reserve"].calls[0][0][0]
    assert reserved == pytest.approx(40.08 * 65, abs=0.01)   # sized on the fill price
