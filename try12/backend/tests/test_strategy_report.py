"""Trade log and per-strategy report (app.services.metrics.strategy_report), and the
spot / IV / delta fields the paper engine writes for it."""
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from app.services.market_data import chain_snapshots as cs
from app.services.metrics import strategy_report as sr

UTC = timezone.utc


def pos(**kw):
    base = dict(
        id=1, strategy_name="ORB_BREAKOUT", option_type="CE", strike=24500.0,
        expiry=date(2026, 11, 3), entry_time=datetime(2026, 10, 30, 4, 12, tzinfo=UTC),   # 09:42 IST
        exit_time=datetime(2026, 10, 30, 5, 1, tzinfo=UTC), spot_entry=24512.4, spot_exit=24561.0,
        actual_entry=118.5, actual_exit=141.0, delta_at_entry=0.52, iv_entry=13.1, iv_exit=12.4,
        initial_stop_loss=88.9, exit_reason="TARGET",
        exit_plan={"spot_entry": 24512.4, "spot_stop": 24470.0, "spot_target": 24610.0},
        gross_pnl=1462.5, brokerage=40.0, statutory_charges=12.5, net_pnl=1410.0,
        entry_slippage=0.5, exit_slippage=0.25, original_quantity=65, quantity=65,
    )
    base.update(kw)
    return SimpleNamespace(**base)


def test_trade_log_row_has_every_phase0_field():
    row = sr.trade_log_row(pos(), seq=1)
    assert row["trade_id"] == "2026-10-30-ORB_BREAKOUT-1"
    assert (row["direction"], row["strike"], row["expiry"]) == ("CE", 24500.0, "2026-11-03")
    assert (row["entry_time"], row["exit_time"], row["is_expiry_day"]) == ("09:42", "10:31", False)
    assert (row["spot_entry"], row["spot_exit"], row["opt_entry"], row["opt_exit"]) == (24512.4, 24561.0, 118.5, 141.0)
    assert (row["delta_at_entry"], row["iv_entry"], row["iv_exit"]) == (0.52, 13.1, 12.4)
    assert (row["spot_stop"], row["spot_target"], row["premium_stop"]) == (24470.0, 24610.0, 88.9)
    assert row["charges"] == 52.5 and row["slippage"] == pytest.approx(0.75 * 65)
    assert row["exit_reason"] == "TARGET"


@pytest.mark.parametrize("reason,plan,expected", [
    ("SL", {}, "PREMIUM_STOP"),
    ("SL_STALE", {}, "PREMIUM_STOP"),
    ("SPOT_SL", {}, "SPOT_STOP"),
    ("SPOT_SL", {"breakeven_done": True}, "BREAKEVEN"),
    ("SPOT_SL", {"t1_done": True}, "BREAKEVEN"),
    ("SESSION_EXIT", {}, "TIME_EXIT"),
    ("ST_FLIP", {}, "FLIP"),
    ("EMA9_TRAIL", {}, "TRAIL"),
    ("EOD", {}, "EOD"),
    (None, {}, "UNKNOWN"),
])
def test_exit_category(reason, plan, expected):
    assert sr.exit_category(reason, plan) == expected


def test_is_expiry_day_compares_the_ist_entry_date():
    row = sr.trade_log_row(pos(expiry=date(2026, 10, 30)))
    assert row["is_expiry_day"] is True


def test_report_blocks_exits_expiry_split_hours_and_flags():
    log = [sr.trade_log_row(p) for p in (
        pos(id=1, net_pnl=1410.0, gross_pnl=1462.5),
        pos(id=2, exit_reason="SL", net_pnl=-900.0, gross_pnl=-850.0, expiry=date(2026, 10, 30)),
        pos(id=3, exit_reason="SL", net_pnl=-700.0, gross_pnl=-650.0,
            entry_time=datetime(2026, 10, 30, 5, 30, tzinfo=UTC)),              # 11:00 IST
        pos(id=4, strategy_name="SUPERTREND_EMA", exit_reason="TARGET", net_pnl=-20.0, gross_pnl=30.0),
    )]
    rep = sr.build_report(log)
    orb = rep["ORB_BREAKOUT"]
    assert (orb["trades"], orb["wins"], orb["win_rate"]) == (3, 1, pytest.approx(1 / 3, abs=1e-4))
    assert orb["avg_win"] == 1410.0 and orb["avg_loss"] == -800.0
    assert orb["expectancy_net"] == pytest.approx((1410 - 900 - 700) / 3, abs=0.01)
    assert orb["exit_reasons"] == {"PREMIUM_STOP": 2, "TARGET": 1}
    assert orb["expiry_day"]["trades"] == 1 and orb["other_days"]["trades"] == 2
    assert orb["by_entry_hour"] == {"09": {"trades": 2, "net_pnl": 510.0}, "11": {"trades": 1, "net_pnl": -700.0}}
    assert any("PREMIUM_STOP" in f for f in orb["flags"])
    assert any("too small" in f for f in rep["SUPERTREND_EMA"]["flags"])
    assert rep["ALL"]["trades"] == 4


def test_option_greeks_round_trip_and_signed_delta():
    now = datetime(2026, 10, 30, 4, 30, tzinfo=UTC)          # 10:00 IST, expiry 4 days later
    exp = date(2026, 11, 3)
    years = (datetime(2026, 11, 3, 10, 0, tzinfo=UTC) - now).total_seconds() / (365 * 86400)
    ce = cs._bs_price(24500, 24500, years, 0.14, True)
    pe = cs._bs_price(24500, 24500, years, 0.14, False)
    iv_c, d_c = cs.option_greeks(ce, 24500, 24500, exp, now, True)
    iv_p, d_p = cs.option_greeks(pe, 24500, 24500, exp, now, False)
    assert iv_c == pytest.approx(14.0, abs=0.05) and iv_p == pytest.approx(14.0, abs=0.05)
    assert 0.45 < d_c < 0.6 and -0.55 < d_p < -0.4
    assert cs.option_greeks(0.0, 24500, 24500, exp, now, True) == (None, None)


def test_the_engine_records_spot_iv_and_delta_at_entry_and_exit(runtime_ctx, sqlite_db, fake_redis, monkeypatch):
    """A real paper entry and close write the trade log's market fields."""
    from app.core.config import settings
    from app.services.trading.paper_engine import paper_engine
    from tests.helpers import (allow_regime, fake_candidate, get_position, make_signal, run,
                               seed_config, stub_select)

    run(seed_config(sqlite_db))
    stub_select(monkeypatch, fake_candidate(ltp=120.0, lot_size=65))     # ATM 22600 CE, expiry 13 Oct
    allow_regime(monkeypatch)
    fake_redis.put_tick("OPT1", 120.0, bid=119.5, ask=120.5, age_s=1)
    assert run(paper_engine.handle_signal(make_signal(sl_pct=0.15))) is True

    p = run(get_position(sqlite_db, 1))
    assert p.spot_entry == 22600.0
    assert p.iv_entry is not None and 1 < p.iv_entry < 100
    assert p.delta_at_entry is not None and 0.4 < p.delta_at_entry < 0.65

    fake_redis.put_tick(settings.NIFTY_TOKEN, 22650.0, age_s=1)
    fake_redis.put_tick("OPT1", 140.0, bid=139.5, ask=140.5, age_s=1)
    run(paper_engine._close_position(1, 140.0, "TARGET"))
    p = run(get_position(sqlite_db, 1))
    assert p.spot_exit == 22650.0 and p.iv_exit is not None
