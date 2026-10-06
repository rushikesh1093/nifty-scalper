"""compute_levels — pure S/R computation (design §5.3 steps 0-9)."""
from datetime import date, datetime, timedelta, timezone

import pytest

from app.services.market_data.levels import IST, compute_levels

DAY1 = date(2026, 10, 5)
DAY2 = date(2026, 10, 6)
OTHER = date(2026, 1, 1)          # a session_date absent from the data → no ORB/DAY levels
SPOT = 10000.0                    # tol = max(10000 * 0.0006, 0.05) = 6.0
TOL = 6.0


def series(n: int, *, day: date = DAY1, base: float = 10000.0, spikes_high: dict | None = None,
           spikes_low: dict | None = None, start_min: int = 0) -> list[dict]:
    """Flat candles (high=base+2, low=base-2) with optional strict spikes at given indices."""
    spikes_high = spikes_high or {}
    spikes_low = spikes_low or {}
    t0 = datetime(day.year, day.month, day.day, 9, 15, tzinfo=IST) + timedelta(minutes=start_min)
    out = []
    for i in range(n):
        h = spikes_high.get(i, base + 2)
        l = spikes_low.get(i, base - 2)
        out.append({"timestamp": (t0 + timedelta(minutes=i)).isoformat(),
                    "open": base, "high": h, "low": l, "close": base, "volume": 0})
    return out


def swings(levels):
    return [l for l in levels if l.source == "SWING"]


def test_double_top_yields_one_resistance_cluster_with_two_touches():
    c = series(60, spikes_high={15: 10020.0, 40: 10021.0})
    levels, meta = compute_levels(c, SPOT, None, session_date=OTHER)
    sw = swings(levels)
    assert len(sw) == 1
    assert sw[0].kind == "RESISTANCE" and sw[0].touches == 2 and sw[0].strength == 2
    assert sw[0].price == pytest.approx(10020.5)
    assert meta["pivot_count"] == 2


def test_double_bottom_yields_one_support_cluster():
    c = series(60, spikes_low={12: 9980.0, 45: 9981.0})
    levels, _ = compute_levels(c, SPOT, None, session_date=OTHER)
    sw = swings(levels)
    assert len(sw) == 1 and sw[0].kind == "SUPPORT" and sw[0].touches == 2


def test_flat_series_has_no_pivots_and_no_levels_without_structure():
    levels, meta = compute_levels(series(60), SPOT, None, session_date=OTHER)
    assert levels == []
    assert meta["pivot_count"] == 0
    assert "no_prev_day_snapshot" in meta["warnings"] and "no_today_candles" in meta["warnings"]
    assert meta["sources_available"] == {"swing": False, "cpr": False, "orb": False, "prev_day": False}


def test_39_candles_hits_min_candles_guard_and_40_does_not():
    levels, meta = compute_levels(series(39), SPOT, None, session_date=DAY1)
    assert levels == [] and "insufficient_candles" in meta["warnings"]
    _, meta40 = compute_levels(series(40), SPOT, None, session_date=DAY1)
    assert "insufficient_candles" not in meta40["warnings"]


@pytest.mark.parametrize("delta,expected_clusters", [(TOL - 0.01, 1), (TOL + 0.01, 2)])
def test_clustering_tolerance_boundary(delta, expected_clusters):
    c = series(60, spikes_high={10: 10020.0, 30: 10020.0 + delta})
    levels, meta = compute_levels(c, SPOT, None, session_date=OTHER)
    assert meta["cluster_tol"] == pytest.approx(TOL)
    assert len(swings(levels)) == expected_clusters


def test_cross_source_merge_produces_one_confluent_level():
    c = series(60, spikes_high={10: 10020.0, 30: 10020.0})
    prev = {"high": 10020.0, "low": 9900.0, "close": 9950.0, "date": "2026-10-04"}
    levels, meta = compute_levels(c, SPOT, prev, session_date=OTHER, max_levels=30)
    near = [l for l in levels if abs(l.price - 10020.0) <= TOL]
    assert len(near) == 1
    assert near[0].source == "PDH" and near[0].touches == 2
    assert near[0].strength == 5               # min(5, max(4, 2) + 1)
    assert meta["prev_day_date"] == "2026-10-04"


def test_cpr_levels_present_with_valid_prev_day():
    prev = {"high": 10100.0, "low": 9900.0, "close": 10050.0}
    levels, meta = compute_levels(series(60), SPOT, prev, session_date=OTHER, max_levels=30)
    sources = {l.source for l in levels}
    assert {"PDH", "PDL", "PDC", "CPR_PIVOT", "CPR_BC", "CPR_TC", "CPR_R1", "CPR_R2", "CPR_S1", "CPR_S2"} <= sources
    assert meta["sources_available"]["cpr"] is True


def test_max_levels_caps_and_result_is_price_descending():
    spikes = {8 + 8 * k: 10030.0 + 40 * k for k in range(6)}
    levels, _ = compute_levels(series(60, spikes_high=spikes), SPOT, None, session_date=OTHER, max_levels=3)
    assert len(levels) == 3
    assert [l.price for l in levels] == sorted([l.price for l in levels], reverse=True)


def test_missing_prev_day_path():
    levels, meta = compute_levels(series(60, spikes_high={20: 10030.0}), SPOT, None, session_date=OTHER)
    assert "no_prev_day_snapshot" in meta["warnings"]
    assert meta["prev_day_date"] is None
    assert not any(l.source.startswith("CPR") or l.source in ("PDH", "PDL", "PDC") for l in levels)


def test_invalid_prev_day_is_treated_as_absent():
    _, meta = compute_levels(series(60), SPOT, {"high": None, "low": 9900.0, "close": 9950.0}, session_date=OTHER)
    assert "no_prev_day_snapshot" in meta["warnings"] and meta["sources_available"]["prev_day"] is False


def test_malformed_rows_are_skipped_and_counted():
    c = series(45)
    c += [
        {"timestamp": "not-a-date", "open": 1, "high": 2, "low": 1, "close": 1},
        {"timestamp": "2026-10-05T12:00:00+05:30", "open": 10, "high": 5, "low": 9, "close": 10},   # high < low
        {"timestamp": "2026-10-05T12:01:00+05:30", "open": -1, "high": 5, "low": 1, "close": 2},    # <= 0
        {"timestamp": "2026-10-05T12:02:00+05:30", "open": float("nan"), "high": 5, "low": 1, "close": 2},
        {"timestamp": "2026-10-05T12:03:00+05:30", "high": 5, "low": 1, "close": 2},                 # missing open
    ]
    _, meta = compute_levels(c, SPOT, None, session_date=OTHER)
    assert meta["skipped_rows"] == 5
    assert "malformed_candles" in meta["warnings"]
    assert meta["candles_used"] == 45


def test_session_date_filters_orb_and_day_range_with_two_day_input():
    day1 = series(60, day=DAY1, spikes_high={30: 10100.0}, spikes_low={30: 9900.0})
    day2 = series(60, day=DAY2, spikes_high={5: 10010.0, 40: 10030.0}, spikes_low={7: 9990.0})
    levels, meta = compute_levels(day1 + day2, SPOT, None, session_date=DAY2, max_levels=30)
    by_src = {l.source: l for l in levels}
    assert by_src["DAY_HIGH"].price == 10030.0       # not day 1's 10100
    assert by_src["DAY_LOW"].price == 9990.0          # not day 1's 9900
    assert by_src["ORB_HIGH"].price == 10010.0        # first 15 bars of DAY2 only
    assert by_src["ORB_LOW"].price == 9990.0
    assert meta["span_days"] == 2
    assert meta["sources_available"]["orb"] is True


def test_cross_session_cluster_gets_strength_bonus():
    day1 = series(60, day=DAY1, spikes_high={20: 10050.0})
    day2 = series(60, day=DAY2, spikes_high={20: 10050.0})
    levels, _ = compute_levels(day1 + day2, SPOT, None, session_date=OTHER)
    sw = swings(levels)
    assert len(sw) == 1 and sw[0].touches == 2 and sw[0].strength == 3   # 2 → 2, +1 multi-day


def test_classification_relative_to_spot():
    c = series(60, spikes_high={10: 10040.0}, spikes_low={30: 9960.0, 50: 10000.0 - 1.0})
    levels, _ = compute_levels(c, SPOT, None, session_date=OTHER, max_levels=30)
    kinds = {round(l.price): l.kind for l in levels}
    assert kinds[10040] == "RESISTANCE"
    assert kinds[9960] == "SUPPORT"


def test_pivot_kind_when_within_tolerance_of_spot():
    c = series(60, base=9990.0, spikes_high={20: 10003.0})
    levels, _ = compute_levels(c, SPOT, None, session_date=OTHER)
    assert swings(levels)[0].kind == "PIVOT"


def test_duplicate_instants_are_deduplicated_on_entry():
    c = series(50)
    dup = dict(c[10])
    dup["timestamp"] = datetime.fromisoformat(dup["timestamp"]).astimezone(timezone.utc).isoformat()
    _, meta = compute_levels(c + [dup], SPOT, None, session_date=OTHER)
    assert meta["candles_used"] == 50


def test_session_date_is_required():
    with pytest.raises(TypeError):
        compute_levels(series(50), SPOT, None)   # type: ignore[call-arg]
