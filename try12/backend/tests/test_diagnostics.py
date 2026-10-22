"""signal_diagnostics — session window predicate + blocker derivation (findings 8, 12, 13)."""
from datetime import datetime, time

from app.core.config import settings
from app.services.trading.signal_diagnostics import (
    derive_blockers, format_session_window, session_window_open, vix_gate,
)
from app.services.market_data.levels import IST

START, END = settings.SESSION_START_IST, settings.SESSION_END_IST
MON = (2026, 10, 5)     # Monday


def at(h: int, m: int, ymd=MON) -> datetime:
    return datetime(*ymd, h, m, tzinfo=IST)


def healthy(now: datetime) -> dict:
    return {
        "now_ist": now, "session_start": START, "session_end": END,
        "db_available": True, "runner_running": True, "study_phase": False,
        "feed": {"connected": True},
        "vix": {"ltp": 14.0, "min": 10.0, "max": 25.0},
        "capital": {"available": 100000.0, "daily_trades": 0, "max_trades_per_day": 10,
                    "open_positions": 0, "max_open_positions": 2, "circuit_breaker": False, "circuit_reason": ""},
        "strategies": {"total": 12, "enabled": 12},
        "underlyings": [
            {"underlying": "NIFTY", "candles_today": 86, "candles_required": 25, "atr": 7.2, "atr_min": 1.5,
             "regime": "TREND_LOW_VOL", "regime_allows_trading": True, "in_cooldown": False,
             "prev_day_snapshot": "2026-10-02", "lot_cost": 5000.0, "atm_premium": 100.0, "lot_size": 50},
        ],
        "probe_errors": [],
    }


def codes(blockers):
    return [b["code"] for b in blockers]


def test_session_constants_equal_the_legacy_runner_literals():
    assert (START.hour * 60 + START.minute, END.hour * 60 + END.minute) == (9 * 60 + 15, 15 * 60 + 25)
    assert format_session_window(START, END) == "09:15-15:25 IST"


def test_monday_1527_ist_is_closed_with_market_closed_detail_naming_1525_and_1530():
    now = at(15, 27)
    assert session_window_open(now, START, END) is False
    b = derive_blockers(healthy(now))
    mc = [x for x in b if x["code"] == "MARKET_CLOSED"]
    assert len(mc) == 1 and mc[0]["severity"] == "blocking"
    assert "15:25" in mc[0]["detail"] and "15:30" in mc[0]["detail"]


def test_boundaries_1524_and_1525_open_0914_closed():
    assert session_window_open(at(15, 24), START, END) is True
    assert session_window_open(at(15, 25), START, END) is True        # inclusive, like the runner's <=
    assert session_window_open(at(15, 26), START, END) is False
    assert session_window_open(at(9, 14), START, END) is False
    assert session_window_open(at(9, 15), START, END) is True


def test_saturday_is_closed():
    sat = at(11, 0, ymd=(2026, 10, 10))
    assert sat.weekday() == 5
    assert session_window_open(sat, START, END) is False
    assert "MARKET_CLOSED" in codes(derive_blockers(healthy(sat)))


def test_healthy_state_in_window_has_no_blockers():
    assert derive_blockers(healthy(at(10, 56))) == []


def test_weekday_in_window_with_zero_candles_emits_no_candles_today_warning():
    s = healthy(at(11, 0))
    s["underlyings"][0]["candles_today"] = 0
    b = derive_blockers(s)
    nct = [x for x in b if x["code"] == "NO_CANDLES_TODAY"]
    assert len(nct) == 1 and nct[0]["severity"] == "warning" and nct[0]["underlying"] == "NIFTY"
    assert "INSUFFICIENT_CANDLES" in codes(b)


def test_section2_snapshot_state_yields_regime_blocks_all_and_capital_below_lot_cost():
    s = healthy(at(10, 56))
    s["feed"] = {"connected": False}
    s["capital"]["available"] = 10000.0
    s["underlyings"] = [
        {"underlying": "NIFTY", "candles_today": 86, "candles_required": 25, "atr": 7.26, "atr_min": 1.5,
         "regime": "EXPIRY_DAY", "regime_allows_trading": False, "in_cooldown": False,
         "prev_day_snapshot": "2026-10-05", "lot_cost": 4000.0, "atm_premium": 80.0, "lot_size": 50},
        {"underlying": "BANKNIFTY", "candles_today": 86, "candles_required": 25, "atr": 20.0, "atr_min": 5.0,
         "regime": "TREND_HIGH_VOL", "regime_allows_trading": True, "in_cooldown": False,
         "prev_day_snapshot": "2026-10-05", "lot_cost": 12071.25, "atm_premium": 804.75, "lot_size": 15},
    ]
    b = derive_blockers(s)
    c = codes(b)
    assert "REGIME_BLOCKS_ALL" in c and "CAPITAL_BELOW_LOT_COST" in c and "FEED_DOWN" in c
    rba = next(x for x in b if x["code"] == "REGIME_BLOCKS_ALL")
    assert rba["underlying"] == "NIFTY" and "EXPIRY_DAY" in rba["message"]
    cap = next(x for x in b if x["code"] == "CAPITAL_BELOW_LOT_COST")
    assert cap["underlying"] == "BANKNIFTY" and cap["severity"] == "blocking"
    assert "804.75" in cap["detail"] and "15" in cap["detail"]


def test_no_premium_makes_capital_blocker_a_warning():
    s = healthy(at(10, 0))
    u = s["underlyings"][0]
    u.pop("lot_cost")
    u["lot_cost_unavailable"] = True
    b = [x for x in derive_blockers(s) if x["code"] == "CAPITAL_BELOW_LOT_COST"]
    assert len(b) == 1 and b[0]["severity"] == "warning"
    assert b[0]["detail"] == "no live ATM premium available to compute lot cost"


def test_db_unavailable_runner_stopped_study_phase_caps_and_breaker():
    s = healthy(at(10, 0))
    s.update(db_available=False, runner_running=False, study_phase=True)
    s["capital"].update(daily_trades=10, open_positions=2, circuit_breaker=True, circuit_reason="loss limit")
    s["strategies"] = {"total": 12, "enabled": 0}
    c = codes(derive_blockers(s))
    for code in ("DB_UNAVAILABLE", "RUNNER_STOPPED", "STUDY_PHASE", "DAILY_TRADE_CAP_REACHED",
                 "MAX_OPEN_POSITIONS_REACHED", "CIRCUIT_BREAKER", "ALL_STRATEGIES_DISABLED"):
        assert code in c


def test_atr_vix_and_cooldown():
    s = healthy(at(10, 0))
    s["underlyings"][0].update(atr=1.0, prev_day_snapshot=None, in_cooldown=True)
    s["vix"]["ltp"] = 30.0
    c = codes(derive_blockers(s))
    assert {"ATR_BELOW_MIN", "VIX_OUT_OF_RANGE", "REGIME_TRANSITION_COOLDOWN"} <= set(c)
    assert "NO_PREV_DAY_SNAPSHOT" not in c        # no strategy needs previous-day levels


def test_probe_errors_become_warning_blockers():
    s = healthy(at(10, 0))
    s["probe_errors"] = [{"code": "VIX_PROBE", "detail": "Connection refused"}]
    b = derive_blockers(s)
    assert b == [{"code": "VIX_PROBE", "severity": "warning", "message": "probe unavailable",
                  "detail": "Connection refused"}]


def test_vix_gate():
    assert vix_gate(None, 10, 25) == "BYPASSED"
    assert vix_gate(14, 10, 25) == "PASS"
    assert vix_gate(9.9, 10, 25) == "BLOCKED"
    assert vix_gate(25.1, 10, 25) == "BLOCKED"


def test_before_open_detail():
    b = derive_blockers(healthy(at(9, 0)))
    assert next(x for x in b if x["code"] == "MARKET_CLOSED")["detail"] == "signal generation starts at 09:15 IST"


def test_session_constants_are_not_settings_fields():
    # ClassVar → not a pydantic field → cannot be overridden from the environment / .env
    assert "SESSION_START_IST" not in type(settings).model_fields
    assert "SESSION_END_IST" not in type(settings).model_fields
    assert isinstance(settings.SESSION_START_IST, time)
