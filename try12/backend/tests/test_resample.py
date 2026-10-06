"""resample_candles — OHLC correctness, IST bucket alignment, identity fields (finding 2)."""
from datetime import datetime, timedelta, timezone

import pytest

from app.schemas.market import CandleResponse
from app.services.market_data.levels import IST, resample_candles


def _one_min(start: datetime, n: int, *, with_oi: bool = True) -> list[dict]:
    out = []
    for i in range(n):
        ts = start + timedelta(minutes=i)
        c = {"token": "26000", "symbol": "NIFTY", "interval": "ONE_MINUTE", "timestamp": ts.isoformat(),
             "open": 100.0 + i, "high": 101.0 + i + (5 if i == 2 else 0), "low": 99.0 + i - (3 if i == 3 else 0),
             "close": 100.5 + i, "volume": 10 + i}
        if with_oi:
            c["oi"] = 1000 + i
        out.append(c)
    return out


S0915 = datetime(2026, 10, 6, 9, 15, tzinfo=IST)


def test_one_to_five_minute_ohlc_volume_against_hand_computed_bars():
    bars = resample_candles(_one_min(S0915, 10), 5, token="26000", interval="FIVE_MINUTE")
    assert len(bars) == 2
    b0, b1 = bars
    assert b0["timestamp"] == "2026-10-06T09:15:00+05:30"
    assert b1["timestamp"] == "2026-10-06T09:20:00+05:30"
    # bar 0 = minutes 0..4
    assert b0["open"] == 100.0
    assert b0["high"] == 101.0 + 2 + 5          # spike at i=2
    assert b0["low"] == 99.0 + 3 - 3            # dip at i=3
    assert b0["close"] == 100.5 + 4
    assert b0["volume"] == sum(10 + i for i in range(5))
    assert b0["oi"] == 1004
    # bar 1 = minutes 5..9
    assert b1["open"] == 105.0 and b1["close"] == 109.5
    assert b1["high"] == 110.0 and b1["low"] == 104.0
    assert b1["volume"] == sum(10 + i for i in range(5, 10))


@pytest.mark.parametrize("minutes,interval,expected", [
    (3, "THREE_MINUTE", ["09:15", "09:18", "09:21"]),
    (15, "FIFTEEN_MINUTE", ["09:15"]),
])
def test_three_and_fifteen_minute_buckets(minutes, interval, expected):
    bars = resample_candles(_one_min(S0915, 9), minutes, token="26000", interval=interval)
    assert [b["timestamp"][11:16] for b in bars] == expected
    assert all(b["interval"] == interval for b in bars)


@pytest.mark.parametrize("minutes", [3, 5, 15])
def test_0915_ist_starts_a_bucket_even_when_first_candle_is_later(minutes):
    bars = resample_candles(_one_min(S0915 + timedelta(minutes=1), 3), minutes, token="26000", interval="X")
    assert bars[0]["timestamp"] == "2026-10-06T09:15:00+05:30"


def test_partial_trailing_bucket_is_retained():
    bars = resample_candles(_one_min(S0915, 7), 5, token="26000", interval="FIVE_MINUTE")
    assert len(bars) == 2
    assert bars[-1]["open"] == 105.0 and bars[-1]["close"] == 106.5
    assert bars[-1]["volume"] == 15 + 16


def test_empty_input_gives_empty_output():
    assert resample_candles([], 5, token="26000", interval="FIVE_MINUTE") == []


def test_single_candle_gives_single_bar():
    bars = resample_candles(_one_min(S0915 + timedelta(minutes=7), 1), 5, token="26000", interval="FIVE_MINUTE")
    assert len(bars) == 1 and bars[0]["timestamp"] == "2026-10-06T09:20:00+05:30"


def test_every_bucket_carries_token_symbol_interval_and_validates_as_candle_response():
    bars = resample_candles(_one_min(S0915, 12), 5, token="26000", interval="FIVE_MINUTE", symbol="NIFTY")
    for b in bars:
        assert b["token"] == "26000" and b["symbol"] == "NIFTY" and b["interval"] == "FIVE_MINUTE"
        CandleResponse(**b)


def test_symbol_defaults_from_rows_when_not_given():
    bars = resample_candles(_one_min(S0915, 5), 5, token="26000", interval="FIVE_MINUTE")
    assert bars[0]["symbol"] == "NIFTY"


def test_input_without_oi_or_volume_keys_does_not_raise():
    rows = _one_min(S0915, 6, with_oi=False)
    for r in rows:
        r.pop("volume")
        r.pop("symbol")
    bars = resample_candles(rows, 5, token="26000", interval="FIVE_MINUTE")
    assert bars[0]["oi"] is None and bars[0]["volume"] == 0 and bars[0]["symbol"] is None
    CandleResponse(**bars[0])


def test_utc_offset_rows_bucket_identically_to_ist_rows():
    utc_rows = _one_min(S0915.astimezone(timezone.utc), 10)
    ist_rows = _one_min(S0915, 10)
    a = resample_candles(utc_rows, 5, token="26000", interval="FIVE_MINUTE")
    b = resample_candles(ist_rows, 5, token="26000", interval="FIVE_MINUTE")
    assert [x["timestamp"] for x in a] == [x["timestamp"] for x in b]
    assert [x["close"] for x in a] == [x["close"] for x in b]
