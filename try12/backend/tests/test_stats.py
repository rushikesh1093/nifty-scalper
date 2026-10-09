"""app/services/metrics/stats.py against hand-computed values (design §16.1, §23)."""
import math
from datetime import date, timedelta
from types import SimpleNamespace

import pytest

from app.services.metrics import stats


def test_wilson_interval_six_of_ten():
    # p = 0.6, z = 1.96: centre = (0.6 + 1.96²/20) / (1 + 1.96²/10)
    z2 = 1.96 ** 2
    denom = 1 + z2 / 10
    centre = (0.6 + z2 / 20) / denom
    half = 1.96 * math.sqrt(0.6 * 0.4 / 10 + z2 / 400) / denom
    lo, hi = stats.wilson_interval(6, 10)
    assert lo == pytest.approx(centre - half) and hi == pytest.approx(centre + half)
    assert lo == pytest.approx(0.3127, abs=1e-4) and hi == pytest.approx(0.8318, abs=1e-4)


def test_wilson_edges():
    assert stats.wilson_interval(0, 0) == (None, None)
    lo, hi = stats.wilson_interval(0, 5)
    assert lo == 0.0 and 0 < hi < 1
    lo, hi = stats.wilson_interval(5, 5)
    assert 0 < lo < 1 and hi == 1.0


def test_equity_drawdown_is_peak_to_trough_of_the_cumulative_curve():
    # cum: 100, 50, -30, 170, -130 → worst fall from the 170 peak = 300
    assert stats.equity_drawdown([100, -50, -80, 200, -300]) == 300
    assert stats.equity_drawdown([-50, -50]) == 100           # from the 0 start
    assert stats.equity_drawdown([10, 20]) == 0
    assert stats.equity_drawdown([]) == 0


def test_longest_losing_streak_flat_neither_extends_nor_resets():
    # L, L, flat, L (=3), win resets, L
    assert stats.longest_losing_streak([-10, -20, 0.5, -30, 50, -10], flat_epsilon=1.0) == 3
    assert stats.longest_losing_streak([5, 5]) == 0
    assert stats.longest_losing_streak([]) == 0


def test_sharpe_and_sortino_like():
    daily = [100.0, -50.0, 150.0]
    m = 200.0 / 3
    sd = math.sqrt(((100 - m) ** 2 + (-50 - m) ** 2 + (150 - m) ** 2) / 2)
    assert stats.sharpe_like(daily) == pytest.approx(m / sd * math.sqrt(252))
    downside = math.sqrt((0 + 50 ** 2 + 0) / 3)                  # over ALL days
    assert stats.sortino_like(daily) == pytest.approx(m / downside * math.sqrt(252))
    assert stats.sortino_like([10.0, 20.0]) is None              # no day below target
    assert stats.sharpe_like([10.0]) is None                     # one day: no stdev
    assert stats.sharpe_like([10.0, 10.0]) is None               # zero dispersion


def test_mean_median_stdev_helpers():
    assert stats.mean([]) is None and stats.median([]) is None and stats.stdev([1.0]) is None
    assert stats.mean([1.0, 2.0, 6.0]) == 3.0
    assert stats.median([3.0, 1.0, 2.0]) == 2.0
    assert stats.stdev([1.0, 3.0]) == pytest.approx(math.sqrt(2))


# ── bootstrap ────────────────────────────────────────────────────────────────

def _trades(per_day):
    """per_day: list of lists of R values; one SimpleNamespace trade per value."""
    out = []
    for i, values in enumerate(per_day):
        day = date(2026, 9, 1) + timedelta(days=i)
        out += [SimpleNamespace(trading_day=day, r=v) for v in values]
    return out


def _mean_r(ts):
    return sum(t.r for t in ts) / len(ts)


def test_day_block_is_wider_than_iid_on_clustered_data():
    # Strong within-day clustering: whole days win or lose together.
    per_day = [[1.0] * 5 if i % 2 == 0 else [-1.0] * 5 for i in range(12)]
    trades = _trades(per_day)
    iid = stats.bootstrap_ci(trades, _mean_r, 2000, seed=7)
    block = stats.bootstrap_ci_by_day(trades, _mean_r, 2000, seed=7)
    assert (block[1] - block[0]) > (iid[1] - iid[0])
    assert block[0] < iid[0] and block[1] > iid[1]


def test_day_block_equals_iid_at_one_trade_per_day():
    trades = _trades([[v] for v in (1.2, -1.0, 0.4, 2.0, -1.0, -0.6, 1.5, -1.0, 0.9, 2.1)])
    iid = stats.bootstrap_ci(trades, _mean_r, 1000, seed=11)
    block = stats.bootstrap_ci_by_day(trades, _mean_r, 1000, seed=11)
    assert iid == block


def test_bootstrap_is_seeded_and_reproducible():
    trades = _trades([[1.0, -1.0], [0.5], [-0.2, 2.0, -1.0], [1.1]])
    a = stats.bootstrap_ci_by_day(trades, _mean_r, 500, seed=20260901)
    b = stats.bootstrap_ci_by_day(trades, _mean_r, 500, seed=20260901)
    c = stats.bootstrap_ci(trades, _mean_r, 500, seed=20260901)
    d = stats.bootstrap_ci(trades, _mean_r, 500, seed=20260901)
    assert a == b and c == d
    assert stats.bootstrap_ci_by_day(trades, _mean_r, 500, seed=1) != a


def test_bootstrap_empty_input():
    assert stats.bootstrap_ci([], _mean_r, 100, seed=1) == (None, None)
    assert stats.bootstrap_ci_by_day([], _mean_r, 100, seed=1) == (None, None)


def test_bootstrap_percentile_indices():
    """With a constant statistic sample the bounds are that constant."""
    trades = _trades([[0.5], [0.5], [0.5]])
    assert stats.bootstrap_ci_by_day(trades, _mean_r, 200, seed=3) == (0.5, 0.5)
