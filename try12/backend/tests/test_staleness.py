"""Fix 6 (B7): price-staleness guard and EOD at last known price."""
import logging
from datetime import datetime, time as dtime, timezone

import pytest

from app.services.trading.paper_engine import paper_engine
from app.services.trading.strategy_runner import strategy_runner
from tests.helpers import add_position, get_position, run, seed_config

SQ = dtime(15, 15)


@pytest.fixture
def closes(monkeypatch):
    calls = []

    async def _spy(pos_id, price, reason):
        calls.append((pos_id, price, reason))

    monkeypatch.setattr(paper_engine, "_close_position", _spy)
    return calls


def _setup(sqlite_db, **pos_kw):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db, **pos_kw))
    return pid, run(get_position(sqlite_db, pid))


def test_missing_ltp_mid_session_warns_and_does_not_close(sqlite_db, fake_redis, closes, caplog):
    pid, pos = _setup(sqlite_db)
    with caplog.at_level(logging.WARNING, logger="app.services.trading.paper_engine"):
        run(paper_engine._check_position(pos, dtime(11, 0), SQ))
    assert closes == []
    assert any("No LTP for pos" in r.getMessage() for r in caplog.records)


def test_missing_ltp_after_square_off_closes_at_last_known(sqlite_db, fake_redis, closes):
    pid, pos = _setup(sqlite_db)
    paper_engine._last_known[pid] = (92.0, datetime.now(timezone.utc))
    run(paper_engine._check_position(pos, dtime(15, 16), SQ))
    assert closes == [(pid, 92.0, "EOD_STALE")]


def test_stale_tick_below_sl_closes_sl_stale(sqlite_db, fake_redis, closes, caplog):
    pid, pos = _setup(sqlite_db)
    fake_redis.put_tick("OPT1", 80.0, age_s=120)
    with caplog.at_level(logging.WARNING, logger="app.services.trading.paper_engine"):
        run(paper_engine._check_position(pos, dtime(11, 0), SQ))
    assert closes == [(pid, 80.0, "SL_STALE")]
    assert any("Stale price" in r.getMessage() for r in caplog.records)


def test_stale_tick_above_target_does_not_book_profit(sqlite_db, fake_redis, closes):
    pid, pos = _setup(sqlite_db)
    fake_redis.put_tick("OPT1", 135.0, age_s=120)
    run(paper_engine._check_position(pos, dtime(11, 0), SQ))
    assert closes == []
    assert run(get_position(sqlite_db, pid)).trailing_sl is None   # no trailing on stale


def test_fresh_tick_above_target_closes_target(sqlite_db, fake_redis, closes):
    pid, pos = _setup(sqlite_db)
    fake_redis.put_tick("OPT1", 131.0, age_s=1)
    run(paper_engine._check_position(pos, dtime(11, 0), SQ))
    assert closes == [(pid, 130.0, "TARGET")]


def test_fresh_tick_persists_last_known(sqlite_db, fake_redis):
    pid, pos = _setup(sqlite_db)
    fake_redis.put_tick("OPT1", 104.0, age_s=1)
    run(paper_engine._check_position(pos, dtime(11, 0), SQ))
    assert paper_engine._last_known[pid][0] == 104.0
    assert fake_redis.kv[f"pos:{pid}"]["last_ltp"] == 104.0


def test_square_off_all_price_resolution(sqlite_db, fake_redis, caplog):
    run(seed_config(sqlite_db, used_capital=19500.0, capital=30000.0))
    fresh = run(add_position(sqlite_db, token="T_FRESH"))
    remembered = run(add_position(sqlite_db, token="T_MEM"))
    nothing = run(add_position(sqlite_db, token="T_NONE"))
    fake_redis.put_tick("T_FRESH", 105.0, age_s=1)
    paper_engine._last_known[remembered] = (88.0, datetime.now(timezone.utc))

    with caplog.at_level(logging.ERROR, logger="app.services.trading.paper_engine"):
        run(strategy_runner._eod_square_off())

    p_fresh = run(get_position(sqlite_db, fresh))
    p_mem = run(get_position(sqlite_db, remembered))
    p_none = run(get_position(sqlite_db, nothing))
    assert (p_fresh.exit_reason, p_fresh.exit_price) == ("EOD", 105.0)
    assert (p_mem.exit_reason, p_mem.exit_price) == ("EOD_STALE", 88.0)   # not entry
    assert (p_none.exit_reason, p_none.exit_price) == ("EOD_STALE", 100.0)
    assert any("closing at ENTRY" in r.getMessage() for r in caplog.records)
    assert not any(p.is_open for p in (p_fresh, p_mem, p_none))


def test_square_off_all_uses_redis_last_ltp_after_restart(sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = run(add_position(sqlite_db))
    fake_redis.kv[f"pos:{pid}"] = {"id": pid, "last_ltp": 91.5}
    result = run(paper_engine.square_off_all("EOD"))
    assert result == {"closed": 1, "stale": [pid], "failed": []}
    pos = run(get_position(sqlite_db, pid))
    assert (pos.exit_reason, pos.exit_price) == ("EOD_STALE", 91.5)
