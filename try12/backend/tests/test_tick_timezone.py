"""TickData.received_at is timezone-aware UTC (blockers-plan item 2).

All DB timestamp columns are timestamptz, CandleBuilder buckets in UTC and the
staleness readers compare against now_utc(); a naive received_at is rejected
so a local-time value can never be mistaken for UTC.
"""
from datetime import datetime, timedelta, timezone

import pytest

from app.services.market_data.feed import TickData
from app.services.market_data.tick_writer import CandleBuilder
from app.services.trading import fills, paper_engine
from tests.conftest import FROZEN_START

UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))


def test_default_received_at_is_aware_utc():
    tick = TickData(token="99926000", exchange="NSE", ltp=22600.0)
    assert tick.received_at.tzinfo is not None
    assert tick.received_at.utcoffset() == timedelta(0)
    assert abs((datetime.now(UTC) - tick.received_at).total_seconds()) < 5


def test_naive_received_at_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        TickData(token="99926000", exchange="NSE", ltp=22600.0,
                 received_at=datetime(2026, 9, 1, 4, 30))


def test_ist_aware_received_at_is_normalised_to_utc():
    ist = datetime(2026, 9, 1, 10, 0, 30, tzinfo=IST)
    tick = TickData(token="99926000", exchange="NSE", ltp=22600.0, received_at=ist)
    assert tick.received_at == ist                      # same instant
    assert tick.received_at.utcoffset() == timedelta(0)
    assert tick.received_at == datetime(2026, 9, 1, 4, 30, 30, tzinfo=UTC)
    assert tick.received_at.isoformat() == "2026-09-01T04:30:30+00:00"


def test_candle_bucket_for_ist_aware_tick_is_correct_utc_minute():
    builder = CandleBuilder()
    t0 = TickData(token="T1", exchange="NFO", ltp=100.0,
                  received_at=datetime(2026, 9, 1, 10, 0, 30, tzinfo=IST))
    assert builder.update(t0) is None
    t1 = TickData(token="T1", exchange="NFO", ltp=101.0,
                  received_at=datetime(2026, 9, 1, 10, 1, 5, tzinfo=IST))
    completed = builder.update(t1)
    assert completed is not None
    assert completed["bucket_start"] == datetime(2026, 9, 1, 4, 30, tzinfo=UTC)
    assert completed["timestamp"] == datetime(2026, 9, 1, 10, 0, tzinfo=IST).isoformat()
    assert completed["timestamp"] == "2026-09-01T10:00:00+05:30"


def test_candle_bucket_survives_ist_object_bypassing_post_init():
    """Even if an IST-aware value is assigned after construction, the bucket
    is computed from the instant, not from the wall-clock fields."""
    builder = CandleBuilder()
    tick = TickData(token="T2", exchange="NFO", ltp=100.0)
    tick.received_at = datetime(2026, 9, 1, 10, 0, 30, tzinfo=IST)
    builder.update(tick)
    assert builder._candles["T2"]["bucket_start"] == datetime(2026, 9, 1, 4, 30, tzinfo=UTC)


def test_staleness_aware_and_legacy_naive_ts_are_identical(runtime_ctx):
    received = FROZEN_START - timedelta(seconds=90)
    tick = TickData(token="T3", exchange="NFO", ltp=50.0, received_at=received)
    aware = {"ltp": 50.0, "ts": tick.received_at.isoformat()}     # market_manager._on_tick shape
    legacy = {"ltp": 50.0, "ts": received.replace(tzinfo=None).isoformat()}
    assert aware["ts"].endswith("+00:00")

    assert fills.tick_age_seconds(aware, FROZEN_START) == 90
    assert fills.tick_age_seconds(legacy, FROZEN_START) == 90
    assert paper_engine._tick_age_seconds(aware) == 90
    assert paper_engine._tick_age_seconds(legacy) == 90
