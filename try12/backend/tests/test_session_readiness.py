"""
Session readiness (Part 4): the startup / 09:00 check of the candle history
(previous session vs 375 candles, today's gaps, out-of-session candles in the
500-candle cache) and of the previous-day levels, its log summary, and where it
surfaces (diagnostics blockers, /health). Also: the per-minute RegimeConfig
"no DB session" WARNING is logged once.
"""
import logging
from datetime import date, datetime, timedelta

from app.services.market_data import session_readiness as rd
from app.services.market_data.timestamps import IST
from app.services.trading.signal_diagnostics import derive_blockers
from tests.helpers import run
from tests.test_prev_session import TODAY, YESTERDAY, _session, pivot_env  # noqa: F401 (fixture)


def _at(h, m, day=TODAY):
    return day.replace(hour=h, minute=m).replace(tzinfo=IST)


def _codes(rep):
    return [w["code"] for w in rep.warnings]


def _assess(cache, now, stored=(), levels=None, problem=None, prev_day=None):
    prev = rd.check_prev_session(now.date(), [*stored, *cache], cache, prev_day)
    return rd.build("NIFTY", now, prev, rd.check_today(now, cache), levels, problem)


LEVELS = {"day": "2026-08-31", "high": 22100.0, "low": 21900.0, "close": 22000.0,
          "source": "minute_1m"}


# ── the pure checks ──────────────────────────────────────────────────────────

def test_a_complete_history_before_the_open_is_ready():
    rep = _assess(_session(YESTERDAY), _at(9, 0), levels=LEVELS)
    assert rep.ok and rep.warnings == []
    assert (rep.prev.day, rep.prev.candles, rep.prev.in_buffer) == (YESTERDAY.date(), 375, 375)
    assert (rep.today.expected, rep.today.missing) == (0, 0)
    s = rep.summary()
    assert "previous session 2026-08-31 375/375 candles, 09:15-15:29" in s
    assert "levels 2026-08-31 H=22100.00" in s and s.endswith("-> OK")


def test_a_partial_previous_session_is_a_warning():
    partial = _session(YESTERDAY.replace(hour=12, minute=38), n=172)
    rep = _assess(partial, _at(9, 0), problem="previous session 2026-08-31 is incomplete")
    assert _codes(rep) == [rd.PREV_SESSION_INCOMPLETE, rd.PREV_SESSION_LEVELS_UNAVAILABLE]
    msg = rep.warnings[0]["message"]
    assert "172/375" in msg and "12:38-15:29" in msg and "203 missing" in msg
    assert "PIVOT_BOUNCE and CPR_VWAP are off today" in rep.warnings[1]["message"]
    assert "levels UNAVAILABLE" in rep.summary() and "2 warning(s)" in rep.summary()


def test_the_ohlc_table_completes_the_previous_session_the_cache_lacks():
    yday = _session(YESTERDAY)
    rep = _assess(yday[-100:], _at(9, 0), stored=yday[:300], levels=LEVELS)
    assert rep.ok and (rep.prev.candles, rep.prev.in_buffer) == (375, 100)


def test_the_levels_day_wins_over_the_latest_minute_day():
    """The broker knows the real previous session: a day with no candles is 0/375."""
    older = _session(datetime(2026, 8, 28, 9, 15))
    rep = _assess(older, _at(9, 0), levels=LEVELS, prev_day=YESTERDAY.date())
    assert rep.prev.day == YESTERDAY.date() and rep.prev.candles == 0
    assert _codes(rep) == [rd.PREV_SESSION_INCOMPLETE]


def test_no_previous_session_candles_at_all():
    rep = _assess([], _at(9, 0))
    assert _codes(rep) == [rd.PREV_SESSION_NO_CANDLES] and "unknown" in rep.summary()


def test_todays_gaps_after_a_late_start_and_a_feed_outage():
    today = _session(TODAY, n=60) + _session(TODAY.replace(hour=10, minute=30), n=130)
    rep = _assess(_session(YESTERDAY)[-310:] + today, _at(12, 40), stored=_session(YESTERDAY),
                  levels=LEVELS)
    # 09:15-12:39 closed = 205 minutes; 10:15-10:29 missing; 12:40 is still forming
    assert (rep.today.expected, rep.today.candles, rep.today.missing) == (205, 190, 15)
    assert rep.today.gaps == ["10:15-10:29"]
    assert _codes(rep) == [rd.TODAY_CANDLE_GAPS]
    assert "15 of 205 minutes since 09:15 have no candle (10:15-10:29)" in rep.warnings[0]["message"]


def test_a_mid_session_start_without_backfill_lists_the_whole_morning():
    rep = _assess(_session(YESTERDAY), _at(12, 38), levels=LEVELS)
    assert rep.today.gaps == ["09:15-12:37"] and rep.today.missing == 203


def test_only_closed_minutes_are_expected():
    assert rd.check_today(_at(9, 16), _session(TODAY, n=1)).expected == 1
    assert rd.check_today(_at(9, 15), []).expected == 0
    late = rd.check_today(_at(16, 0), _session(TODAY))
    assert (late.expected, late.missing) == (375, 0)
    saturday = datetime(2026, 9, 5, 11, 0, tzinfo=IST)
    assert rd.check_today(saturday, []).expected == 0


def test_gap_ranges_are_capped_in_the_message():
    cache = [c for k, c in enumerate(_session(TODAY, n=40)) if k % 4]      # 10 single-minute holes
    rep = _assess(_session(YESTERDAY) + cache, _at(9, 55), levels=LEVELS)
    assert rep.today.missing == 10 and len(rep.today.gaps) == 10
    assert "and 5 more range(s)" in rep.warnings[0]["message"]


def test_pre_open_candles_occupying_the_buffer():
    pre = _session(TODAY.replace(hour=8, minute=48), n=27)                # 08:48-09:14
    rep = _assess(_session(YESTERDAY)[-200:] + pre, _at(9, 15), stored=_session(YESTERDAY),
                  levels=LEVELS)
    assert (rep.today.out_of_session_in_buffer, rep.today.pre_open_today) == (27, 27)
    assert rep.today.buffer_size == 227 and rep.prev.in_buffer == 200
    assert _codes(rep) == [rd.OUT_OF_SESSION_CANDLES_IN_BUFFER]
    assert "27 of 227 cached candles are outside 09:15-15:30 (27 from before today's open)" \
        in rep.warnings[0]["message"]


def test_refresh_redoes_today_from_the_cache_and_keeps_the_prev_count():
    yday = _session(YESTERDAY)
    rep = _assess(yday, _at(9, 0), levels=LEVELS)
    cache = (yday + _session(TODAY, n=200))[-500:]
    later = rd.refresh(rep, _at(12, 35), cache, LEVELS, None)
    assert later.prev.candles == 375 and later.prev.in_buffer == 300
    assert later.today.expected == 200 and later.ok
    assert later.as_dict()["today_candles"] == 200


# ── through the runner ───────────────────────────────────────────────────────

def test_the_runner_logs_a_summary_and_keeps_the_result(pivot_env, caplog):
    runner, strat, cache, _, _ = pivot_env
    cache(_session(YESTERDAY) + _session(TODAY.replace(hour=8, minute=50), n=25),
          TODAY.replace(hour=9, minute=0))
    run(runner._pre_market_setup())
    with caplog.at_level(logging.INFO, logger="app.services.trading.strategy_runner"):
        out = run(runner.check_session_readiness())
    rep = out["NIFTY"]
    assert rep.levels["high"] == 22100.0 and rep.levels_problem is None
    assert _codes(rep) == [rd.OUT_OF_SESSION_CANDLES_IN_BUFFER]
    msgs = [r.getMessage() for r in caplog.records]
    assert any("[NIFTY] session readiness 2026-09-01 at 09:00" in m and "1 warning(s)" in m
               for m in msgs)
    assert any("readiness OUT_OF_SESSION_CANDLES_IN_BUFFER" in m for m in msgs)
    assert runner.session_readiness("NIFTY", _at(9, 5)) is rep
    assert runner.session_readiness("NIFTY", _at(9, 5, TODAY + timedelta(days=1))) is None
    assert runner.session_readiness("BANKNIFTY", _at(9, 5)) is None


def test_the_runner_reports_missing_levels(pivot_env):
    runner, strat, cache, _, _ = pivot_env
    cache(_session(YESTERDAY.replace(hour=12, minute=38), n=172), TODAY.replace(hour=9, minute=0))
    run(runner._pre_market_setup())
    rep = run(runner.check_session_readiness())["NIFTY"]
    assert _codes(rep) == [rd.PREV_SESSION_INCOMPLETE, rd.PREV_SESSION_LEVELS_UNAVAILABLE]
    assert "incomplete" in rep.levels_problem


def test_the_runner_check_never_raises(pivot_env, fake_redis, monkeypatch):
    runner, *_ = pivot_env

    async def _boom(*a, **k):
        raise RuntimeError("redis down")
    monkeypatch.setattr(fake_redis, "get_candles", _boom)
    assert run(runner.check_session_readiness()) == {}


def test_the_pre_market_setup_does_not_run_it():
    """Replay calls _pre_market_setup once per day; only start() and the live
    09:00 step run the readiness check."""
    import inspect

    from app.services.trading.strategy_runner import StrategyRunner
    assert "check_session_readiness" not in inspect.getsource(StrategyRunner._pre_market_setup)
    for fn in (StrategyRunner.start, StrategyRunner._eod_monitor):
        assert "self.check_session_readiness()" in inspect.getsource(fn)


# ── where it surfaces ────────────────────────────────────────────────────────

def test_readiness_warnings_become_diagnostics_blockers():
    rep = _assess(_session(YESTERDAY.replace(hour=12, minute=38), n=172), _at(9, 0))
    state = {"now_ist": _at(10, 0), "session_start": datetime.min.time().replace(hour=9, minute=15),
             "session_end": datetime.min.time().replace(hour=15, minute=25),
             "underlyings": [{"underlying": "NIFTY", "readiness_warnings": rep.warnings}]}
    b = [x for x in derive_blockers(state) if x["code"] == rd.PREV_SESSION_INCOMPLETE]
    assert len(b) == 1 and b[0]["severity"] == "warning" and b[0]["underlying"] == "NIFTY"
    assert "172/375" in b[0]["message"]


def test_the_diagnostics_schema_accepts_the_readiness_dict():
    from app.schemas.diagnostics import UnderlyingProbe
    rep = _assess(_session(YESTERDAY), _at(12, 38), levels=LEVELS)
    probe = UnderlyingProbe(underlying="NIFTY", candles_required=20, atr_min=1.5,
                            readiness=rep.as_dict())
    assert probe.readiness.today_gaps == ["09:15-12:37"]
    assert probe.readiness.warnings[0].code == rd.TODAY_CANDLE_GAPS


def test_health_reports_the_last_readiness_check(monkeypatch):
    from fastapi.testclient import TestClient

    from app.main import app
    from app.services.trading.strategy_runner import strategy_runner
    rep = _assess([], datetime.now(IST))
    monkeypatch.setitem(strategy_runner._readiness, "NIFTY", rep)
    body = TestClient(app).get("/health").json()
    assert body["readiness"]["NIFTY"] == {"ok": False, "checked_at": rep.checked_at.isoformat(),
                                          "warnings": _codes(rep)}
    assert rd.PREV_SESSION_NO_CANDLES in _codes(rep)


# ── RegimeConfig fallback WARNING: once, not every minute ────────────────────

def test_the_no_db_session_warning_is_logged_once(monkeypatch, caplog):
    from app.services.trading import regime_config as rc
    monkeypatch.setattr(rc, "_no_session_warned", set())
    with caplog.at_level(logging.DEBUG, logger=rc.__name__):
        for _ in range(3):
            rc.load_config("NIFTY")
        rc.load_config("BANKNIFTY")
    recs = [r for r in caplog.records if "No DB session provided" in r.getMessage()]
    assert [r.levelno for r in recs] == [logging.WARNING, logging.DEBUG, logging.DEBUG,
                                         logging.WARNING]
