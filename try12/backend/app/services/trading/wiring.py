"""
Signal-handler wiring (design §14.1a).

paper_engine.handle_signal is reached only through SignalBus._handlers. Both
entry points — StrategyRunner.start() (live) and ReplayEngine.run() — call
this, so replay cannot silently drop every signal, and a second start() over
a process lifetime cannot double-enter every signal.
"""
from app.strategies.base import SignalBus


def wire_signal_handlers() -> None:
    """Idempotent. The only place handle_signal is subscribed."""
    from app.services.trading.paper_engine import paper_engine

    SignalBus.reset_handlers()
    SignalBus.subscribe(paper_engine.handle_signal)
