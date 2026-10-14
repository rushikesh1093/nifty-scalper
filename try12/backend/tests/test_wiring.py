"""Signal-handler wiring is idempotent and shared by both entry points (design §14.1a)."""
import pytest

from app.services.trading.paper_engine import paper_engine
from app.services.trading.wiring import wire_signal_handlers
from app.strategies.base import SignalBus


@pytest.fixture
def clean_bus(monkeypatch):
    monkeypatch.setattr(SignalBus, "_handlers", [])


def test_wire_twice_leaves_one_handler(clean_bus):
    wire_signal_handlers()
    wire_signal_handlers()
    assert SignalBus._handlers == [paper_engine.handle_signal]


def test_wire_replaces_a_stray_subscription(clean_bus):
    async def stray(signal):
        return False
    SignalBus.subscribe(stray)
    SignalBus.subscribe(paper_engine.handle_signal)
    wire_signal_handlers()
    assert SignalBus._handlers == [paper_engine.handle_signal]


def test_strategy_runner_start_uses_wiring():
    import inspect
    from app.services.trading.strategy_runner import StrategyRunner
    src = inspect.getsource(StrategyRunner.start)
    assert "wire_signal_handlers()" in src
    assert "SignalBus.subscribe" not in src


def test_step_functions_take_no_time_or_flags_parameter():
    import inspect
    from app.services.trading.strategy_runner import StrategyRunner
    from app.services.trading.paper_engine import PaperEngine
    assert list(inspect.signature(StrategyRunner.run_cycle).parameters) == ["self", "underlying"]
    assert list(inspect.signature(PaperEngine.monitor_once).parameters) == ["self"]


def test_candle_loop_uses_configured_underlyings():
    import inspect
    from app.services.trading.strategy_runner import StrategyRunner

    source = inspect.getsource(StrategyRunner._candle_loop)
    assert "for underlying in ctx().cfg.underlyings:" in source
    assert "run_cycle(underlying)" in source
    assert 'for underlying in ["NIFTY", "BANKNIFTY"]' not in source
