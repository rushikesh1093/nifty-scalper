"""A hole in the recent candles (feed outage / restart without history) is
NO TRADE (STALE_DATA) until the engine has a clean contiguous window."""
from datetime import datetime, timedelta

from app.strategies.trend_engine import STALE_DATA, TrendFollowingEngine, _largest_gap

START = datetime(2026, 9, 1, 8, 50)      # naive IST; runtime_ctx clock = 10:00 IST


def _c(m, px):
    ts = START + timedelta(minutes=m)
    return {"timestamp": ts.strftime("%Y-%m-%d %H:%M:%S"), "open": px, "high": px + 3,
            "low": px - 3, "close": px + 1, "volume": 100}


def test_largest_gap():
    assert _largest_gap([_c(0, 1), _c(1, 1), _c(5, 1)]) == (4.0, "08:55")
    assert _largest_gap([_c(0, 1)]) is None


def test_hole_in_the_window_is_no_trade(runtime_ctx):
    minutes = [m for m in range(70) if not 45 <= m < 60]        # 09:35-09:49 missing
    candles = [_c(m, 22600 + m) for m in minutes]
    d = TrendFollowingEngine("NIFTY").decide(candles, {"atr": 5.0})
    assert d.decision == "NO TRADE" and d.reason_code == STALE_DATA
    assert "hole" in d.reason and "09:50" in d.reason


def test_contiguous_window_is_not_blocked_by_an_older_hole(runtime_ctx):
    minutes = [m for m in range(70) if not 20 <= m < 30]        # hole before the last 30
    candles = [_c(m, 22600 + m) for m in minutes]
    d = TrendFollowingEngine("NIFTY").decide(candles, {"atr": 5.0})
    assert d.reason_code != STALE_DATA
