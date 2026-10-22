"""paper_engine._save_signal — detached SIGNAL_RECORDED publish (design §5.1, findings 7, 11).
No DB / Redis: a fake session and a stubbed publish."""
import asyncio
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from app.db.models.paper_trade import SignalStatus, TradingMode
from app.services.trading import paper_engine as pe_mod
from app.strategies.base import Direction, SignalEvent


class _Session:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    def add(self, obj):
        self.obj = obj

    async def commit(self):
        pass

    async def refresh(self, obj):
        obj.id = 4242


def _signal():
    return SignalEvent(strategy_name="VWAP_EMA_PULLBACK", underlying="BANKNIFTY", direction=Direction.SELL,
                       spot_price=55099.45, signal_time=datetime(2026, 10, 6, 8, 0, 51, tzinfo=timezone.utc),
                       vwap=55102.4, indicators={})


@pytest.fixture
def wired(monkeypatch):
    calls = []
    state = {"mode": TradingMode.PAPER, "publish": None}

    async def _get_config():
        return SimpleNamespace(mode=state["mode"])

    async def _publish(channel, payload):
        calls.append((channel, payload))
        if state["publish"]:
            await state["publish"]()

    from app.services.trading import mode_manager as mm
    monkeypatch.setattr(mm.mode_manager, "get_config", _get_config)
    import app.db.base as db_base     # ctx().sessions resolves this at call time
    from app.services.cache.redis_client import redis_cache
    monkeypatch.setattr(db_base, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(redis_cache, "publish_signal", _publish)
    return calls, state


async def _drain():
    for _ in range(5):
        await asyncio.sleep(0)


async def test_publishes_eight_field_payload_on_paper_channel(wired):
    calls, _ = wired
    sid = await pe_mod.paper_engine._save_signal(_signal(), SignalStatus.SKIPPED, reason="momentum failed")
    assert sid == 4242
    await _drain()
    assert len(calls) == 1
    channel, payload = calls[0]
    assert channel == "paper_trade"
    assert payload == {
        "event": "SIGNAL_RECORDED", "signal_id": 4242, "strategy": "VWAP_EMA_PULLBACK", "underlying": "BANKNIFTY",
        "direction": "SELL", "status": "SKIPPED", "time": "2026-10-06T08:00:51+00:00",
        "skip_reason": "momentum failed",
    }


async def test_live_mode_publishes_on_live_channel(wired):
    calls, state = wired
    state["mode"] = TradingMode.LIVE
    await pe_mod.paper_engine._save_signal(_signal(), SignalStatus.GENERATED)
    await _drain()
    assert calls[0][0] == "live_trade"


async def test_hung_publish_never_blocks_save_and_task_is_retained(wired):
    calls, state = wired
    gate = asyncio.Event()
    state["publish"] = gate.wait                      # publish hangs until released
    sid = await asyncio.wait_for(pe_mod.paper_engine._save_signal(_signal(), SignalStatus.SELECTED), timeout=1.0)
    assert sid == 4242
    await _drain()
    assert len(pe_mod._bg_tasks) >= 1                 # strong reference held while pending
    gate.set()
    await _drain()
    assert not pe_mod._bg_tasks                       # discarded on completion


async def test_failing_publish_is_swallowed(wired):
    _, state = wired

    async def _boom():
        raise ConnectionError("redis down")

    state["publish"] = _boom
    sid = await pe_mod.paper_engine._save_signal(_signal(), SignalStatus.SKIPPED)
    await _drain()
    assert sid == 4242 and not pe_mod._bg_tasks
