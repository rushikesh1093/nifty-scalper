"""
Spot exit plans end to end through paper_engine: the pure tick_action order,
then T1 partial + stop-to-entry, spot stop, time stop, premium-point target,
the generic ladder being off for plan positions, opposite-signal exits,
candle-close exits and the strategy close hook.
"""
from datetime import time as dtime, timedelta

import pytest

from app.services.trading import exit_plan as xp
from app.services.trading.exit_plan import ExitPlan
from app.services.trading.paper_engine import paper_engine
from app.strategies.base import Direction
from tests.helpers import add_position, get_position, make_signal, run, seed_config

SPOT = "26000"          # settings.NIFTY_TOKEN


def plan(**kw) -> dict:
    base = dict(strategy="VWAP_EMA_PULLBACK", side="BUY", spot_entry=22000.0,
                spot_stop=21990.0, spot_t1=22015.0)
    base.update(kw)
    return ExitPlan(**base).to_dict()


def _open(Session, runtime_ctx, *, lots=1, **kw):
    fields = dict(strategy_name="VWAP_EMA_PULLBACK", exit_plan=plan(),
                  entry_time=runtime_ctx.clock.now_utc(), initial_risk=15.0, r_source="ENTRY")
    if lots == 2:
        fields.update(lots=2, quantity=130, original_quantity=130, original_lots=2,
                      trade_amount_used=13000.0, brokerage=40.0)
    fields.update(kw)
    return run(add_position(Session, **fields))


def _tick(fake_redis, Session, pid, ltp, spot, *, at=dtime(11, 0)):
    fake_redis.put_tick("OPT1", ltp, age_s=1.0)
    if spot is not None:
        fake_redis.put_tick(SPOT, spot, age_s=1.0)
    pos = run(get_position(Session, pid))
    if pos.is_open:
        run(paper_engine._check_position(pos, at, dtime(15, 15)))
    return run(get_position(Session, pid))


# ── pure ─────────────────────────────────────────────────────────────────────

def test_tick_action_priority_and_validation():
    p = ExitPlan.from_dict(plan(spot_target=22030.0, time_stop_minutes=10))
    assert xp.tick_action(p, 21989.0, 50) == xp.SPOT_SL          # stop beats time
    assert xp.tick_action(p, 22031.0, 1) == xp.TARGET
    assert xp.tick_action(p, 22016.0, 1) == xp.T1_PARTIAL
    assert xp.tick_action(p, 22005.0, 11) == xp.TIME_EXIT
    assert xp.tick_action(p, None, 1) is None                    # no spot: premium stop only
    assert ExitPlan.from_dict(plan(spot_stop=22010.0)).validate()  # stop on the wrong side
    short = ExitPlan.from_dict(plan(side="SELL", spot_stop=22010.0, spot_t1=21985.0))
    assert short.validate() == [] and xp.tick_action(short, 22011.0, 0) == xp.SPOT_SL


# ── engine ───────────────────────────────────────────────────────────────────

def test_t1_books_half_and_moves_both_stops_to_entry(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=13000.0))
    pid = _open(sqlite_db, runtime_ctx, lots=2)
    pos = _tick(fake_redis, sqlite_db, pid, 112.0, 22016.0)
    assert pos.is_open and pos.partial_closed and pos.lots == 1
    assert pos.stop_loss == 100.0                                # premium stop → entry
    p = ExitPlan.from_dict(pos.exit_plan)
    assert p.t1_done and p.spot_stop == 22000.0                  # spot stop → entry
    pos = _tick(fake_redis, sqlite_db, pid, 104.0, 21999.0)      # back through entry
    assert not pos.is_open and pos.exit_reason == xp.SPOT_SL


def test_single_lot_t1_skips_the_partial_but_still_moves_the_stop(runtime_ctx, sqlite_db,
                                                                  fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx)
    pos = _tick(fake_redis, sqlite_db, pid, 112.0, 22016.0)
    assert pos.is_open and pos.partial_skipped and not pos.partial_closed
    assert pos.stop_loss == 100.0 and ExitPlan.from_dict(pos.exit_plan).t1_done


def test_spot_stop_closes_before_the_premium_stop(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx)
    pos = _tick(fake_redis, sqlite_db, pid, 95.0, 21990.0)       # premium still above 85
    assert not pos.is_open and pos.exit_reason == xp.SPOT_SL


def test_premium_stop_still_applies(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx)
    pos = _tick(fake_redis, sqlite_db, pid, 84.0, 21995.0)       # spot fine, premium through 85
    assert not pos.is_open and pos.exit_reason == "SL"


def test_time_stop(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx,
                exit_plan=plan(strategy="ORB_BREAKOUT", spot_t1=None, spot_target=22060.0,
                               time_stop_minutes=45),
                strategy_name="ORB_BREAKOUT",
                entry_time=runtime_ctx.clock.now_utc() - timedelta(minutes=46))
    pos = _tick(fake_redis, sqlite_db, pid, 108.0, 22010.0)
    assert not pos.is_open and pos.exit_reason == xp.TIME_EXIT


def test_premium_point_target(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx, strategy_name="SUPERTREND_EMA", target=120.0,
                exit_plan=plan(strategy="SUPERTREND_EMA", spot_t1=None,
                               premium_target_pts=20.0, time_stop_minutes=10))
    pos = _tick(fake_redis, sqlite_db, pid, 120.5, 22004.0)
    assert not pos.is_open and pos.exit_reason == xp.TARGET


def test_generic_ladder_and_premium_target_are_off_for_plan_positions(runtime_ctx, sqlite_db,
                                                                      fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx, exit_plan=plan(spot_t1=22100.0))
    pos = _tick(fake_redis, sqlite_db, pid, 131.0, 22010.0)      # past the nominal 130 target
    assert pos.is_open and pos.stop_loss == 85.0                 # no LOCK_60, no TARGET


def test_opposite_signal_closes_the_other_side(runtime_ctx, sqlite_db, fake_redis):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx)                          # a VWAP_EMA_PULLBACK CE
    fake_redis.put_tick("OPT1", 103.0, age_s=1.0)
    assert run(paper_engine._close_opposite(make_signal(
        strategy="VWAP_EMA_PULLBACK", direction=Direction.BUY))) == 0     # same side
    assert run(paper_engine._close_opposite(make_signal(
        strategy="VWAP_EMA_PULLBACK", direction=Direction.SELL))) == 1
    pos = run(get_position(sqlite_db, pid))
    assert not pos.is_open and pos.exit_reason == xp.OPPOSITE_SIGNAL


@pytest.mark.parametrize("other", ["SUPERTREND_EMA", "ORB_BREAKOUT"])
def test_another_strategys_opposite_signal_leaves_the_position_open(runtime_ctx, sqlite_db,
                                                                    fake_redis, other):
    run(seed_config(sqlite_db, used_capital=6500.0))
    pid = _open(sqlite_db, runtime_ctx)                          # a VWAP_EMA_PULLBACK CE
    fake_redis.put_tick("OPT1", 103.0, age_s=1.0)
    assert run(paper_engine._close_opposite(make_signal(
        strategy=other, direction=Direction.SELL))) == 0
    assert run(get_position(sqlite_db, pid)).is_open


def test_opposite_signal_closes_only_its_own_strategys_position(runtime_ctx, sqlite_db,
                                                                fake_redis):
    run(seed_config(sqlite_db, used_capital=13000.0))
    mine = _open(sqlite_db, runtime_ctx, strategy_name="SUPERTREND_EMA",
                 exit_plan=plan(strategy="SUPERTREND_EMA", spot_t1=None,
                                premium_target_pts=20.0, time_stop_minutes=10))
    other = _open(sqlite_db, runtime_ctx)                        # VWAP_EMA_PULLBACK CE
    fake_redis.put_tick("OPT1", 103.0, age_s=1.0)
    assert run(paper_engine._close_opposite(make_signal(
        strategy="SUPERTREND_EMA", direction=Direction.SELL))) == 1
    assert not run(get_position(sqlite_db, mine)).is_open
    assert run(get_position(sqlite_db, other)).is_open


class _Stub:
    def __init__(self, name, reason=None, trail_to=None):
        self.name, self.reason, self.trail_to = name, reason, trail_to
        self.history, self.closed = [], []

    def candle_exit(self, p, candles, indicators):
        if self.trail_to is not None:
            xp.tighten_stop(p, self.trail_to)
        return self.reason

    def position_closed(self, direction, reason):
        self.closed.append((direction, reason))


def test_candle_exits_close_or_persist_the_trailed_stop(runtime_ctx, sqlite_db, fake_redis,
                                                         monkeypatch):
    from app.services.trading.strategy_runner import strategy_runner
    run(seed_config(sqlite_db, used_capital=13000.0))
    a = _open(sqlite_db, runtime_ctx)
    b = _open(sqlite_db, runtime_ctx, strategy_name="SUPERTREND_EMA",
              exit_plan=plan(strategy="SUPERTREND_EMA", spot_t1=None, premium_target_pts=20.0))
    fake_redis.put_tick("OPT1", 101.0, age_s=1.0)
    vwap = _Stub("VWAP_EMA_PULLBACK", reason=xp.VWAP_EXIT)
    st = _Stub("SUPERTREND_EMA", trail_to=21996.0)
    monkeypatch.setattr(strategy_runner, "_strategies", {"NIFTY": [vwap, st]})
    closed = run(paper_engine.run_candle_exits("NIFTY", [vwap, st], [], [], {}))
    assert closed == 1
    pa, pb = run(get_position(sqlite_db, a)), run(get_position(sqlite_db, b))
    assert not pa.is_open and pa.exit_reason == xp.VWAP_EXIT
    assert vwap.closed == [(Direction.BUY, xp.VWAP_EXIT)]        # close hook reached the strategy
    assert pb.is_open and ExitPlan.from_dict(pb.exit_plan).spot_stop == 21996.0


def test_replay_spot_index_has_a_timestamped_tick():
    """Replay stores spot candles, not ticks. Without a tick the plan's spot
    exits saw no fresh price and were silently skipped (missed ORB stops)."""
    from datetime import datetime, timezone
    from types import SimpleNamespace
    from app.services.replay.ports_impl import ReplayMarketData
    from tests.helpers import FrozenClock

    t0 = datetime(2026, 9, 1, 4, 30, tzinfo=timezone.utc)                 # 10:00 IST
    clock = FrozenClock(t0 + timedelta(minutes=1, seconds=20))
    m = ReplayMarketData(clock, spot_tokens={SPOT: "NIFTY"}, vix_token="VIX",
                         max_bracket_gap_s=300)
    m.load_day(SimpleNamespace(
        candles={SPOT: [dict(token=SPOT, timestamp=t0, open=1.0, high=2.0,
                             low=0.5, close=1.5, volume=0)]},
        ticks={}, snapshots={}, chains={}, vix=None))
    tick = run(m.get_tick(SPOT))
    assert tick["ltp"] == 1.5 == run(m.get_ltp(SPOT))
    assert tick["ts"] == (t0 + timedelta(minutes=1)).isoformat()           # the candle's close
