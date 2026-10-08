"""regime_states collision handling (design §11, R2 finding 4).

A replay of the same window writes the same (underlying, timestamp) pairs.
With run_id in the unique key, a different run never collides; a true
duplicate inside one run is caught at a savepoint, recorded as a DATA_GAP
degradation, and leaves the caller's transaction usable."""
from datetime import datetime, timezone

import pytest
from sqlalchemy import func, select

from app.db.models.regime import RegimeStateModel
from app.services.trading.regime_persistence import RegimeState, regime_persistence
from tests.helpers import make_test_context, run
from tests.test_regime_state_machine import _ind

TS = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)


def _state(ts=TS):
    return RegimeState(regime="BULL_TREND_NORMAL_VOL", underlying="NIFTY", timestamp=ts,
                       indicators=_ind())


async def _count(Session, model, **where):
    async with Session() as db:
        q = select(func.count()).select_from(model)
        for k, v in where.items():
            q = q.where(getattr(model, k) == v)
        return (await db.execute(q)).scalar()


def test_duplicate_in_one_run_is_caught_and_transaction_stays_usable(runtime_ctx, sqlite_db):
    from app.db.models.risk import NoTradeDecision

    async def _go():
        async with sqlite_db() as db:
            await regime_persistence.persist_to_db(_state(), db)
            await db.commit()
        async with sqlite_db() as db:
            await regime_persistence.persist_to_db(_state(), db)      # duplicate key
            # the same transaction still accepts work and commits
            await regime_persistence.persist_to_db(
                _state(TS.replace(minute=31)), db)
            await db.commit()

    run(_go())
    assert run(_count(sqlite_db, RegimeStateModel)) == 2
    assert run(_count(sqlite_db, RegimeStateModel, run_id="PAPER")) == 2
    gaps = run(_count(sqlite_db, NoTradeDecision, code="DATA_GAP", stage="DEGRADATION"))
    assert gaps == 1


def test_same_key_in_another_run_does_not_collide(runtime_ctx, sqlite_db):
    from app.runtime.context import using

    async def _go(rid):
        async with sqlite_db() as db:
            await regime_persistence.persist_to_db(_state(), db)
            await db.commit()

    run(_go("PAPER"))
    with using(make_test_context(clock=runtime_ctx.clock, market=runtime_ctx.market,
                                 sessions=sqlite_db, run_id="REPLAY_A")):
        run(_go("REPLAY_A"))
    assert run(_count(sqlite_db, RegimeStateModel)) == 2
    assert run(_count(sqlite_db, RegimeStateModel, run_id="REPLAY_A")) == 1


def test_non_integrity_errors_propagate(runtime_ctx):
    class _Boom:
        def begin_nested(self):
            raise RuntimeError("connection lost")

        def add(self, obj):
            pass

    with pytest.raises(RuntimeError, match="connection lost"):
        run(regime_persistence.persist_to_db(_state(), _Boom()))


def test_runner_persist_still_isolates_other_errors(runtime_ctx, sqlite_db, caplog, monkeypatch):
    """_persist_regime_state keeps its non-fatal contract for re-raised errors."""
    import logging
    from app.services.trading.strategy_runner import StrategyRunner

    async def _raise(state, db):
        raise RuntimeError("other failure")

    caplog.set_level(logging.WARNING, logger="app.services.trading.strategy_runner")
    monkeypatch.setattr(regime_persistence, "persist_to_db", _raise)
    run(StrategyRunner()._persist_regime_state("NIFTY", _state()))
    assert any("regime persist failed" in r.message for r in caplog.records)
