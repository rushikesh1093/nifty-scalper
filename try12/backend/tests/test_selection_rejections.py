"""
Item 7 — per-ReasonCode selection-rejection visibility (blockers-plan item 7).

No threshold changes: an in-process counter per (trading_day, underlying) logs a
structured ``SELECTION_REJECTIONS`` line at most once per 300 s per underlying,
and a read-only route sums the tallies the rows already store.
"""
import json
import logging
from datetime import timedelta

import httpx
from sqlalchemy import event

from app.core.trading_params import TradingParams
from app.db.models.paper_trade import StrategySignal
from app.db.models.risk import NoTradeDecision
from app.runtime.context import using
from app.services.trading import option_selector as option_selector_mod
from app.services.trading.option_selector import option_selector
from app.strategies.base import Direction
from tests.helpers import option_row, run, seed_config, with_instruments

SPOT = 22600.0
KW = dict(sl_pct=0.15, target_pct=0.30, params=TradingParams(max_otm_strikes=20),  # wide ladder
          base_amount=20_000.0,
          regime_multiplier=1.0)
GOOD = dict(ltp=100.0, bid=99.5, ask=100.5, oi=200_000, volume=5_000)
BAD = {                                   # strike -> the one field that fails
    22600: dict(bid=90.0, ask=110.0),     # SPREAD_TOO_WIDE
    22650: dict(oi=10_000),               # LIQUIDITY_TOO_LOW
    22700: dict(volume=10),               # VOLUME_INSUFFICIENT
}
RPL = "RPL-01TESTRUN0000000000000000"[:30]


def _universe(rc):
    exp = rc.clock.trading_day() + timedelta(days=7)
    return [option_row(k, exp) for k in BAD]


def _put_ticks(fake_redis):
    for strike, over in BAD.items():
        q = dict(GOOD, **over)
        fake_redis.put_tick(f"NIFTY-CE-{strike}", q["ltp"], bid=q["bid"], ask=q["ask"],
                            age_s=1.0, oi=q["oi"], volume=q["volume"])


def _lines(caplog):
    out = []
    for r in caplog.records:
        msg = r.getMessage()
        if msg.startswith("SELECTION_REJECTIONS "):
            assert r.levelno == logging.INFO
            out.append(json.loads(msg[len("SELECTION_REJECTIONS "):]))
    return out


def _select(rc):
    with using(with_instruments(rc, _universe(rc))):
        return run(option_selector.select(None, Direction.BUY, "NIFTY", SPOT, **KW))


def test_counters_and_rate_limited_structured_line(runtime_ctx, fake_redis, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.option_selector")
    _put_ticks(fake_redis)
    res = _select(runtime_ctx)
    assert res.candidate is None
    day = runtime_ctx.clock.trading_day()
    c = option_selector_mod.rejection_counters().get(day, "NIFTY")
    assert c["by_code"] == {"SPREAD_TOO_WIDE": 1, "LIQUIDITY_TOO_LOW": 1, "VOLUME_INSUFFICIENT": 1}
    assert c["evaluated"] == 3 and c["selected"] == 0

    lines = _lines(caplog)
    assert len(lines) == 1
    assert set(lines[0]) == {"run_id", "trading_day", "underlying", "evaluated", "selected",
                             "by_code"}
    assert lines[0]["run_id"] == "PAPER" and lines[0]["underlying"] == "NIFTY"
    assert lines[0]["trading_day"] == day.isoformat()
    assert lines[0]["by_code"]["SPREAD_TOO_WIDE"] == 1

    runtime_ctx.clock.advance(120)
    _put_ticks(fake_redis)
    _select(runtime_ctx)
    assert len(_lines(caplog)) == 1                        # inside 300 s: no new line
    assert option_selector_mod.rejection_counters().get(day, "NIFTY")["evaluated"] == 6

    runtime_ctx.clock.advance(181)                         # 301 s after the first line
    _put_ticks(fake_redis)
    _select(runtime_ctx)
    lines = _lines(caplog)
    assert len(lines) == 2
    assert lines[1]["evaluated"] == 9 and lines[1]["by_code"]["VOLUME_INSUFFICIENT"] == 3


def test_select_top_n_does_not_count(runtime_ctx, fake_redis, caplog):
    caplog.set_level(logging.INFO, logger="app.services.trading.option_selector")
    _put_ticks(fake_redis)
    with using(with_instruments(runtime_ctx, _universe(runtime_ctx))):
        run(option_selector.select_top_n(None, Direction.BUY, "NIFTY", SPOT, n=3, **KW))
    day = runtime_ctx.clock.trading_day()
    assert option_selector_mod.rejection_counters().get(day, "NIFTY") is None
    assert _lines(caplog) == []


def test_a_day_roll_drops_the_stale_day(runtime_ctx, fake_redis):
    _put_ticks(fake_redis)
    _select(runtime_ctx)
    day1 = runtime_ctx.clock.trading_day()
    runtime_ctx.clock.advance(days=1)
    _put_ticks(fake_redis)
    _select(runtime_ctx)
    counters = option_selector_mod.rejection_counters()
    assert counters.get(day1, "NIFTY") is None
    assert counters.get(runtime_ctx.clock.trading_day(), "NIFTY")["evaluated"] == 3


# ── the summary route ────────────────────────────────────────────────────────

def _get(path, **params):
    from app.main import app

    async def _go():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                     base_url="http://test") as c:
            return await c.get(path, params=params)
    return run(_go())


def _seed_rows(Session, rc):
    day = rc.clock.trading_day()
    at = rc.clock.now_utc()

    async def _go():
        async with Session() as db:
            for run_id in ("PAPER", RPL):
                db.add(NoTradeDecision(
                    run_id=run_id, trading_day=day, decided_at=at, stage="SELECTION",
                    code="NO_OPTION_AVAILABLE", underlying="NIFTY", reason="No option available",
                    context={"rejections": {"SPREAD_TOO_WIDE": 2, "LIQUIDITY_TOO_LOW": 1},
                             "evaluated": 3}))
                db.add(NoTradeDecision(                       # another code: not a tally
                    run_id=run_id, trading_day=day, decided_at=at, stage="RISK",
                    code="DAILY_LOSS_HALT", underlying="NIFTY", reason="x",
                    context={"rejections": {"SPREAD_TOO_WIDE": 50}}))
                # the SKIPPED twin of the NO_OPTION_AVAILABLE row: same tally, not re-counted
                db.add(StrategySignal(strategy_name="ORB_BREAKOUT", underlying="NIFTY",
                                      direction="BUY", spot_price=SPOT, signal_time=at,
                                      run_id=run_id,
                                      selection_rejections={"SPREAD_TOO_WIDE": 2,
                                                            "LIQUIDITY_TOO_LOW": 1}))
                # a signal whose option WAS selected: the other strikes' rejections
                db.add(StrategySignal(strategy_name="ORB_BREAKOUT", underlying="NIFTY",
                                      direction="BUY", spot_price=SPOT, signal_time=at,
                                      run_id=run_id, suggested_token="NIFTY-CE-22600",
                                      selection_rejections={"VOLUME_INSUFFICIENT": 4}))
                db.add(StrategySignal(strategy_name="ORB_BREAKOUT", underlying="BANKNIFTY",
                                      direction="BUY", spot_price=48000.0, signal_time=at,
                                      run_id=run_id, suggested_token="BN-1",
                                      selection_rejections={"QUOTE_STALE": 7}))
            await db.commit()
    run(_go())
    return day


def test_route_sums_both_sources_scoped_to_the_run(runtime_ctx, sqlite_db):
    run(seed_config(sqlite_db, capital=200_000.0, max_lots_per_trade=3))
    day = _seed_rows(sqlite_db, runtime_ctx)
    statements = []
    engine = sqlite_db.kw["bind"].sync_engine

    def _record(conn, cursor, statement, parameters, context, executemany):
        statements.append(statement)

    event.listen(engine, "before_cursor_execute", _record)
    try:
        r = _get("/api/v1/analytics/selection-rejections", run_id="PAPER")
    finally:
        event.remove(engine, "before_cursor_execute", _record)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["run_id"] == "PAPER"
    assert body["by_code"] == {"SPREAD_TOO_WIDE": 2, "LIQUIDITY_TOO_LOW": 1,
                               "VOLUME_INSUFFICIENT": 4, "QUOTE_STALE": 7}
    assert body["by_day"] == {day.isoformat(): body["by_code"]}
    assert body["sources"] == {"no_option_available": 1, "selected_signals": 2}
    assert statements
    assert [s for s in statements
            if not s.lstrip().upper().startswith(("SELECT", "PRAGMA"))] == []

    nifty = _get("/api/v1/analytics/selection-rejections", underlying="nifty").json()
    assert nifty["by_code"] == {"SPREAD_TOO_WIDE": 2, "LIQUIDITY_TOO_LOW": 1,
                                "VOLUME_INSUFFICIENT": 4}
    later = _get("/api/v1/analytics/selection-rejections",
                 date_from=(day + timedelta(days=1)).isoformat()).json()
    assert later["by_code"] == {} and later["empty_reason"] == "no_data_for_filter"
    assert _get("/api/v1/analytics/selection-rejections", run_id="bad").status_code == 400
