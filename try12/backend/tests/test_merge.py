"""merge_dedup_by_timestamp — finding 1: dedup on the UTC instant, not the string."""
from datetime import datetime, timedelta, timezone

from app.services.market_data.levels import IST, merge_dedup_by_timestamp, parse_candle_ts


def _row(ts: str, close: float, src: str) -> dict:
    return {"token": "26000", "symbol": "NIFTY", "interval": "ONE_MINUTE", "timestamp": ts,
            "open": close, "high": close + 1, "low": close - 1, "close": close, "volume": 0, "src": src}


def test_ist_redis_row_and_utc_ohlc_row_for_same_minute_collapse_to_one_ist_row():
    db_rows = [_row("2026-10-06T04:30:00+00:00", 100.0, "db")]
    cached = [_row("2026-10-06T10:00:00+05:30", 200.0, "redis")]
    out = merge_dedup_by_timestamp(db_rows, cached)
    assert len(out) == 1
    assert out[0]["timestamp"] == "2026-10-06T10:00:00+05:30"
    assert out[0]["src"] == "redis"            # cached wins the tie
    assert out[0]["close"] == 200.0


def test_db_row_converted_to_ist_offset_when_no_cached_twin():
    out = merge_dedup_by_timestamp([_row("2026-10-05T09:59:00+00:00", 1.0, "db")], [])
    assert out[0]["timestamp"] == "2026-10-05T15:29:00+05:30"


def test_output_strictly_ascending_and_unique_instants_for_interleaved_sources():
    base = datetime(2026, 10, 6, 3, 45, tzinfo=timezone.utc)
    db_rows = [_row((base + timedelta(minutes=m)).isoformat(), 10.0 + m, "db") for m in range(0, 20, 2)]
    cached = [_row((base + timedelta(minutes=m)).astimezone(IST).isoformat(), 50.0 + m, "redis")
              for m in range(0, 20, 3)]
    out = merge_dedup_by_timestamp(db_rows, list(reversed(cached)))
    instants = [parse_candle_ts(r["timestamp"]) for r in out]
    assert instants == sorted(instants)
    assert len(set(instants)) == len(instants)
    assert all(i < j for i, j in zip(instants, instants[1:]))
    assert all(r["timestamp"].endswith("+05:30") for r in out)
    expected = {m for m in range(0, 20, 2)} | {m for m in range(0, 20, 3)}
    assert len(out) == len(expected)
    for m in (0, 6, 12, 18):                     # overlaps → redis value
        row = next(r for r in out if parse_candle_ts(r["timestamp"]) == base + timedelta(minutes=m))
        assert row["src"] == "redis"


def test_naive_timestamps_are_treated_as_ist():
    out = merge_dedup_by_timestamp([_row("2026-10-06T04:30:00+00:00", 1.0, "db")],
                                   [_row("2026-10-06 10:00:00", 2.0, "redis")])
    assert len(out) == 1 and out[0]["src"] == "redis"


def test_unparseable_timestamp_rows_are_skipped_not_raised():
    out = merge_dedup_by_timestamp([_row("garbage", 1.0, "db")], [_row("2026-10-06T10:00:00+05:30", 2.0, "redis")])
    assert len(out) == 1
