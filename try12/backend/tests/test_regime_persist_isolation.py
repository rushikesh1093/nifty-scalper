"""StrategyRunner._persist_regime_state — a DB outage must never escape (design §5.5 item 2)."""
import logging
from datetime import datetime, timezone

import pytest

import app.db.base as db_base
from app.services.trading.strategy_runner import StrategyRunner


class _State:
    regime = "EXPIRY_DAY"
    underlying = "NIFTY"
    timestamp = datetime(2026, 10, 6, 5, 0, tzinfo=timezone.utc)


async def test_session_acquisition_failure_is_swallowed_and_logged(monkeypatch, caplog):
    def _boom():
        raise ConnectionRefusedError("postgres down")

    monkeypatch.setattr(db_base, "AsyncSessionLocal", _boom)
    caplog.set_level(logging.WARNING, logger="app.services.trading.strategy_runner")
    result = await StrategyRunner()._persist_regime_state("NIFTY", _State())
    assert result is None
    assert any("regime persist failed (non-fatal)" in r.getMessage() for r in caplog.records)


async def test_commit_failure_is_swallowed_and_logged(monkeypatch, caplog):
    calls = {"persist": 0}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            raise RuntimeError("commit failed")

    async def _persist(state, db):
        calls["persist"] += 1

    from app.services.trading import regime_persistence as rp
    monkeypatch.setattr(db_base, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(rp.regime_persistence, "persist_to_db", _persist)
    caplog.set_level(logging.WARNING, logger="app.services.trading.strategy_runner")
    await StrategyRunner()._persist_regime_state("NIFTY", _State())
    assert calls["persist"] == 1
    assert any("regime persist failed (non-fatal)" in r.getMessage() for r in caplog.records)


async def test_happy_path_persists_and_commits(monkeypatch):
    seen = {"commit": 0, "persist": 0}

    class _Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def commit(self):
            seen["commit"] += 1

    async def _persist(state, db):
        seen["persist"] += 1

    from app.services.trading import regime_persistence as rp
    monkeypatch.setattr(db_base, "AsyncSessionLocal", lambda: _Session())
    monkeypatch.setattr(rp.regime_persistence, "persist_to_db", _persist)
    await StrategyRunner()._persist_regime_state("NIFTY", _State())
    assert seen == {"commit": 1, "persist": 1}


def test_gating_block_does_not_reference_persistence():
    """The regime-gating path stays free of DB calls (structural guard).

    After the fix/live-system-p0-p1 merge the gating try body delegates to
    _detect_and_merge_regime, so both the try body and that helper's code
    (docstring excluded) are checked.
    """
    import ast
    import inspect
    import textwrap
    src = inspect.getsource(StrategyRunner._run_underlying)
    gate = src[src.index("await self._detect_and_merge_regime("):src.index("except Exception as _regime_err")]
    assert "AsyncSessionLocal" not in gate and "persist" not in gate and "db_session" not in gate

    fn = ast.parse(textwrap.dedent(inspect.getsource(StrategyRunner._detect_and_merge_regime))).body[0]
    body = fn.body[1:] if ast.get_docstring(fn) else fn.body
    helper = "\n".join(ast.unparse(node) for node in body)
    assert "AsyncSessionLocal" not in helper and "persist" not in helper
    assert "db_session=None" in helper

    # the detection-failure branch decides the flags (all disabled, design §6.4)
    # before, and independently of, the persistence call
    after = src[src.index("except Exception as _regime_err"):]
    assert after.index("mark_regime_unavailable") < after.index("_persist_regime_state")
    assert "final_flags = flags" not in src


@pytest.mark.parametrize("attr", ["is_running"])
def test_is_running_property(attr):
    r = StrategyRunner()
    assert getattr(r, attr) is False
    r._running = True
    assert getattr(r, attr) is True
