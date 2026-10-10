"""trade_metrics.compute on a fixed 10-trade fixture, hand-computed (design §16.1, §24)."""
import math
from datetime import date, datetime, timedelta, timezone

import pytest

from app.core.trading_params import TradingParams
from app.services.metrics import trade_metrics as tm

P = TradingParams()          # flat_epsilon ₹1, r_flat_epsilon 0.01R, min_display_sample 20
CAPITAL = 200_000.0

#        day  net      r       source                 r_net  hold strategy
FIXTURE = [
    (1, 1000.0,  2.0,   "ENTRY",               1.8,   10, "ORB_VWAP"),
    (1, -500.0,  -1.0,  "ENTRY",               -1.1,  20, "ORB_VWAP"),
    (1, -520.0,  -1.0,  "ENTRY",               -1.12, 30, "RSI_VWAP"),
    (2, 0.5,     0.0,   "ENTRY",               -0.05, 5,  "RSI_VWAP"),    # flat by ₹, R_FLAT
    (2, 1500.0,  2.5,   "ENTRY",               2.3,   15, "ORB_VWAP"),
    (2, -300.0,  None,  "BACKFILL_UNVERIFIED", None,  None, "RSI_VWAP"),  # r_unavailable
    (3, 200.0,   0.4,   "ENTRY",               0.3,   25, "ORB_VWAP"),
    (3, -40.0,   0.005, "ENTRY",               -0.05, 12, "RSI_VWAP"),    # breakeven: loss, R_FLAT
    (3, -600.0,  -1.2,  "ENTRY",               -1.3,  8,  "ORB_VWAP"),    # gapped stop
    (4, 900.0,   1.8,   "ENTRY",               1.7,   40, "RSI_VWAP"),
]


def _rows():
    out = []
    for i, (d, net, r, src, r_net, hold, strat) in enumerate(FIXTURE, 1):
        day = date(2026, 9, d)
        exit_t = datetime(2026, 9, d, 5, 0, tzinfo=timezone.utc) + timedelta(minutes=i)
        out.append(tm.TradeRow(
            position_id=i, run_id="RPL-TEST", trading_day=day, underlying="NIFTY",
            strategy_name=strat, entry_regime="BULL_TREND_NORMAL_VOL", direction="BUY",
            strike=22600.0, option_type="CE", expiry=date(2026, 9, 8), dte_at_entry=5,
            lots=1, quantity=65, actual_entry=100.0, initial_stop_loss=85.0,
            initial_risk=15.0 if src == "ENTRY" else None, target=130.0, actual_exit=None,
            exit_reason="X", exit_value_weighted=None, gross_pnl=net + 40, brokerage=40.0,
            statutory_charges=0.0, net_pnl=net, r_multiple=r, r_multiple_net=r_net,
            r_source=src, entry_time=exit_t - timedelta(minutes=5), exit_time=exit_t,
            hold_minutes=hold))
    return out


def test_ten_trade_fixture():
    m = tm.compute(_rows(), CAPITAL, P)
    assert (m.trades, m.wins, m.losses, m.flats) == (10, 4, 5, 1)
    assert m.win_rate == pytest.approx(0.4)
    assert m.avg_win_inr == pytest.approx(3600 / 4)                    # 900
    assert m.avg_loss_inr == pytest.approx(1960 / 5)                   # 392
    assert m.win_loss_ratio_inr == pytest.approx(900 / 392)

    # R buckets over the 9 R-valid trades: wins 2.0, 2.5, 0.4, 1.8; losses 1.0, 1.0, 1.2
    assert m.avg_win_r == pytest.approx(6.7 / 4)
    assert m.avg_loss_r == pytest.approx(3.2 / 3)
    assert m.win_loss_ratio_r == pytest.approx((6.7 / 4) / (3.2 / 3))
    assert (m.r_flat_trades, m.decided_r, m.r_sample_size, m.r_unavailable) == (2, 7, 9, 1)
    assert m.expectancy_r == pytest.approx(3.505 / 9)                  # GROSS
    assert m.expectancy_r_net == pytest.approx(2.48 / 9)
    assert m.expectancy_inr == pytest.approx(1640.5 / 10)              # every trade
    assert m.gross_profit == pytest.approx(3600.5) and m.gross_loss == pytest.approx(1960)
    assert m.profit_factor == pytest.approx(3600.5 / 1960)
    assert m.median_r == pytest.approx(0.005)
    rs = [2.0, -1.0, -1.0, 0.0, 2.5, 0.4, 0.005, -1.2, 1.8]
    mu = sum(rs) / 9
    assert m.stdev_r == pytest.approx(math.sqrt(sum((r - mu) ** 2 for r in rs) / 8))

    # cum: 1000, 500, -20 (dd 1020), -19.5, 1480.5, 1180.5, 1380.5, 1340.5, 740.5, 1640.5
    assert m.max_equity_drawdown_inr == pytest.approx(1020)
    assert m.max_equity_drawdown_pct == pytest.approx(1020 / CAPITAL * 100)
    assert m.worst_trade_loss == pytest.approx(600)
    assert m.longest_losing_streak == 2
    assert m.avg_hold_minutes == pytest.approx(165 / 9)
    assert m.low_sample is True and m.sample_size == 10


def test_daily_metrics():
    d = tm.compute_daily(_rows(), CAPITAL)
    daily = [-20.0, 1200.5, -440.0, 900.0]
    mu = sum(daily) / 4
    sd = math.sqrt(sum((x - mu) ** 2 for x in daily) / 3)
    assert (d.trading_days, d.positive_days) == (4, 2)
    assert d.mean_daily_pnl == pytest.approx(mu) and d.stdev_daily_pnl == pytest.approx(sd)
    assert d.sharpe_like == pytest.approx(mu / sd * math.sqrt(252))
    downside = math.sqrt((20 ** 2 + 440 ** 2) / 4)
    assert d.sortino_like == pytest.approx(mu / downside * math.sqrt(252))
    assert "risk-free rate assumed 0" in d.assumptions and "scale-invariant" in d.assumptions


def test_empty_set_is_all_null_and_low_sample():
    m = tm.compute([], CAPITAL, P)
    assert m.trades == 0 and m.low_sample is True
    assert m.win_rate is None and m.expectancy_r is None and m.profit_factor is None
    d = tm.compute_daily([], CAPITAL)
    assert d.trading_days == 0 and d.sharpe_like is None


def test_profit_factor_is_null_with_no_winners_or_no_losers():
    rows = _rows()
    losers = [r for r in rows if r.net_pnl < 0]
    winners = [r for r in rows if r.net_pnl > 0]
    assert tm.compute(losers, CAPITAL, P).profit_factor is None
    assert tm.compute(winners, CAPITAL, P).profit_factor is None


def test_group_by_strategy_and_day():
    g = tm.group_by(_rows(), "strategy", CAPITAL, P)
    assert set(g) == {"ORB_VWAP", "RSI_VWAP"}
    assert g["ORB_VWAP"].trades == 5 and g["RSI_VWAP"].trades == 5
    days = tm.group_by(_rows(), "day", CAPITAL, P)
    assert list(days) == ["2026-09-01", "2026-09-02", "2026-09-03", "2026-09-04"]
    with pytest.raises(ValueError):
        tm.group_by(_rows(), "colour", CAPITAL, P)


def test_determinism_hash_ignores_ids_and_wall_clock_fields():
    import dataclasses
    rows = _rows()
    m = tm.compute(rows, CAPITAL, P)
    h = tm.determinism_hash(rows, m)
    renumbered = [dataclasses.replace(r, position_id=r.position_id + 100, run_id="RPL-OTHER",
                                      hold_minutes=999.0, sizing_method="KELLY") for r in rows]
    assert tm.determinism_hash(renumbered, tm.compute(renumbered, CAPITAL, P)) != "" and \
        tm.determinism_hash(renumbered, m) == h
    changed = [dataclasses.replace(rows[0], net_pnl=1001.0)] + rows[1:]
    assert tm.determinism_hash(changed, m) != h
    assert "r_multiple_net" in tm.DETERMINISM_FIELDS
