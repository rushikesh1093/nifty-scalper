"""Indicator correctness against independent reference implementations."""
from datetime import datetime, timedelta, timezone

import numpy as np
import pytest

from app.strategies import indicators as ind

IST = timezone(timedelta(hours=5, minutes=30))
rng = np.random.default_rng(7)
CLOSE = 23000 + np.cumsum(rng.normal(0, 4, 300))
HIGH = CLOSE + rng.uniform(0.5, 6, 300)
LOW = CLOSE - rng.uniform(0.5, 6, 300)


def _wilder_ref(x, n):
    """Plain-Python Wilder smoothing: SMA seed, then avg = (avg*(n-1) + x) / n."""
    out = [float("nan")] * len(x)
    avg = sum(x[:n]) / n
    out[n - 1] = avg
    for i in range(n, len(x)):
        avg = (avg * (n - 1) + x[i]) / n
        out[i] = avg
    return out


def _rsi_ref(c, n=14):
    ch = [c[i] - c[i - 1] for i in range(1, len(c))]
    g = _wilder_ref([max(d, 0.0) for d in ch], n)
    l = _wilder_ref([max(-d, 0.0) for d in ch], n)
    out = [float("nan")]
    for a, b in zip(g, l):
        out.append(float("nan") if a != a else (100.0 if b == 0 else 100 - 100 / (1 + a / b)))
    return np.array(out)


def test_rsi_is_wilders():
    np.testing.assert_allclose(ind.rsi(CLOSE, 14), _rsi_ref(list(CLOSE)), rtol=1e-12, equal_nan=True)
    assert np.isnan(ind.rsi(CLOSE, 14)[13]) and np.isfinite(ind.rsi(CLOSE, 14)[14])


def test_rsi_edge_cases():
    assert ind.rsi(np.arange(40.0), 14)[-1] == 100.0
    assert ind.rsi(np.arange(40.0)[::-1], 14)[-1] == 0.0
    assert ind.rsi(np.full(40, 5.0), 14)[-1] == 50.0
    alt = np.array([100.0 + (i % 2) for i in range(201)])
    assert abs(ind.rsi(alt, 14)[-1] - 50.0) < 3.0


def test_atr_is_wilders():
    pc = np.concatenate([[CLOSE[0]], CLOSE[:-1]])
    tr = np.maximum(HIGH - LOW, np.maximum(abs(HIGH - pc), abs(LOW - pc)))
    np.testing.assert_allclose(ind.atr(HIGH, LOW, CLOSE, 14), _wilder_ref(list(tr), 14),
                               rtol=1e-12, equal_nan=True)


def test_bollinger_uses_population_std():
    bb = ind.bollinger(CLOSE, 20, 2.0)
    w = CLOSE[-20:]
    assert bb.mid[-1] == pytest.approx(w.mean())
    assert bb.upper[-1] == pytest.approx(w.mean() + 2 * w.std(ddof=0))


def test_ema_reference_and_leading_nans():
    e = ind.ema(CLOSE, 9)
    ref = CLOSE[:9].mean()
    for x in CLOSE[9:]:
        ref = x * 0.2 + ref * 0.8
    assert e[-1] == pytest.approx(ref)
    m = ind.macd(CLOSE)                          # the signal line is an EMA of a NaN-led series
    assert np.isfinite(m.signal[-1]) and np.isfinite(m.hist[-1])


def test_supertrend_direction_is_unknown_during_warm_up():
    st = ind.supertrend(HIGH[:8], LOW[:8], CLOSE[:8], 10, 3.0)
    assert set(st.direction.tolist()) == {0}
    full = ind.supertrend(HIGH, LOW, CLOSE, 10, 3.0)
    assert set(full.direction[20:].tolist()) <= {1, -1}
    cands = [{"open": c, "high": h, "low": l, "close": c, "volume": 0}
             for h, l, c in zip(HIGH[:8], LOW[:8], CLOSE[:8])]
    assert ind.compute_all(cands)["supertrend_dir"] is None


def _session(start_hm, n, base=23000.0):
    t0 = datetime(2026, 10, 21, start_hm[0], start_hm[1], tzinfo=IST)
    out = []
    for i in range(n):
        px = base + i
        out.append({"timestamp": (t0 + timedelta(minutes=i)).isoformat(), "open": px,
                    "high": px + 1, "low": px - 1, "close": px, "volume": 0})
    return out


def test_orb_is_the_0915_0929_range():
    cands = _session((9, 15), 40)
    out = ind.compute_all(cands)
    assert out["orb_high"] == 23000 + 14 + 1 and out["orb_low"] == 23000 - 1


def test_orb_unknown_before_0930_or_after_a_late_start():
    assert ind.compute_all(_session((9, 15), 12))["orb_high"] is None     # range not complete
    late = ind.compute_all(_session((9, 27), 40))                           # 09:15 candle missing
    assert late["orb_high"] is None and late["orb_broken"] == "NONE"
